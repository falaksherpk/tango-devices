#!/usr/bin/env python3
"""Prometheus exporter for the lab's Tango devices (handbook Part 4, Ch36).

Prometheus on obs.beamline scrapes it; every scrape reads the devices now, so a value
is never older than the scrape, and a device that cannot be read is reported as down
rather than as its last good value.

Configuration is site data, from the environment (the tango_ds role writes it to
/etc/beamline-tango/tango-exporter-<instance>.env), checked at startup:

  TANGO_EXPORTER_LISTEN       host:port to serve /metrics on (the lab address)
  TANGO_EXPORTER_DEVICES      the devices whose State is exported, space-separated.
                              Listed, not discovered: a device that disappears from
                              the database turns into tango_device_up 0, not a gap.
  TANGO_EXPORTER_ATTRIBUTES   device/attribute entries (numeric or boolean scalars)
                              exported as tango_attribute_value; a ":counter" suffix
                              exports a count the device keeps itself (it may reset
                              at the device's Init) as tango_attribute_count_total.
  TANGO_EXPORTER_HDBPP        HDB++ EventSubscriber devices: their attribute counts and
                              the statistics of their last 60 s window (the numbers
                              beamline-scripts' 85-hdbpp check reads).
  TANGO_EXPORTER_TIMEOUT_MS   the client timeout for each device (default 1500).

How it reads: one tango.utils.PyTangoThread per device owns that device's DeviceProxy
(the thread holds EnsureOmniThread for its whole life, as cppTango needs, and no proxy
is shared between threads). A scrape asks every worker for one read_attributes call
(State plus the device's listed attributes, from the polling cache where they are
polled) and waits for all of them up to a deadline, so one hung device costs one
timeout, not one per device. Scrapes are serialized.

Metrics: tango_device_up and tango_device_state{state} (one-hot over DevState);
tango_attribute_value, tango_attribute_up and tango_attribute_count_total;
tango_read_errors_total{device,attribute}, the reads this exporter saw fail since it
started (attribute="State" for a device that could not be read at all); the hdbpp_es_*
families; tango_exporter_scrape_duration_seconds; the process_* metrics.
"""

import logging
import os
import queue
import re
import signal
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import Future
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass

import tango
from prometheus_client import CollectorRegistry, ProcessCollector, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector
from tango import DevState
from tango.utils import PyTangoThread

log = logging.getLogger("tango_exporter")

DEFAULT_TIMEOUT_MS = 1500
# Margin on top of the client timeout before a scrape stops waiting for a worker.
DEADLINE_MARGIN_S = 1.0
COUNTER_SUFFIX = ":counter"
STATE_NAMES = tuple(sorted(DevState.names))
HDBPP_ATTRIBUTES = (
    "AttributeOkNumber",
    "AttributeNokNumber",
    "AttributePendingNumber",
    "AttributeList",
    "AttributeFailureFreq",
    "AttributeFailureFreqList",
    "AttributeRecordFreqList",
    "StatisticsResetTime",
)


class ConfigError(ValueError):
    """A setting that the exporter refuses to start with."""


@dataclass(frozen=True)
class AttrSpec:
    device: str
    attribute: str
    counter: bool = False


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    devices: tuple[str, ...]
    attributes: tuple[AttrSpec, ...]
    hdbpp: tuple[str, ...]
    timeout_ms: int = DEFAULT_TIMEOUT_MS


def config_from_env(env: Mapping[str, str]) -> Config:
    """Build the configuration from TANGO_EXPORTER_* variables, or raise ConfigError."""
    listen = env.get("TANGO_EXPORTER_LISTEN", "").strip()
    if not listen:
        raise ConfigError("TANGO_EXPORTER_LISTEN is not set")
    m = re.fullmatch(r"(.+):(\d+)", listen)
    if not m:
        raise ConfigError(f"TANGO_EXPORTER_LISTEN must be host:port, got {listen!r}")
    host, port = m.group(1), int(m.group(2))
    if port > 65535:
        raise ConfigError(f"TANGO_EXPORTER_LISTEN: port {port} out of range")

    devices = tuple(env.get("TANGO_EXPORTER_DEVICES", "").split())
    if not devices:
        raise ConfigError("TANGO_EXPORTER_DEVICES lists no device")
    if len(set(devices)) != len(devices):
        raise ConfigError("TANGO_EXPORTER_DEVICES lists a device twice")
    known = set(devices)

    attributes = []
    for entry in env.get("TANGO_EXPORTER_ATTRIBUTES", "").split():
        name, counter = entry, False
        if entry.endswith(COUNTER_SUFFIX):
            name, counter = entry[: -len(COUNTER_SUFFIX)], True
        elif re.search(r":[a-z]+$", entry):
            raise ConfigError(f"TANGO_EXPORTER_ATTRIBUTES: unknown suffix in {entry!r}")
        device, sep, attr = name.rpartition("/")
        if not sep or not device or not attr:
            raise ConfigError(
                f"TANGO_EXPORTER_ATTRIBUTES: {entry!r} is not device/attribute"
            )
        if device not in known:
            raise ConfigError(
                f"TANGO_EXPORTER_ATTRIBUTES: device of {entry!r} is not in "
                "TANGO_EXPORTER_DEVICES"
            )
        attributes.append(AttrSpec(device, attr, counter))

    hdbpp = tuple(env.get("TANGO_EXPORTER_HDBPP", "").split())
    for es in hdbpp:
        if es not in known:
            raise ConfigError(
                f"TANGO_EXPORTER_HDBPP: {es!r} is not in TANGO_EXPORTER_DEVICES"
            )

    raw_timeout = env.get("TANGO_EXPORTER_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)).strip()
    try:
        timeout_ms = int(raw_timeout)
    except ValueError:
        raise ConfigError(
            f"TANGO_EXPORTER_TIMEOUT_MS must be an integer, got {raw_timeout!r}"
        ) from None
    if timeout_ms <= 0:
        raise ConfigError(
            f"TANGO_EXPORTER_TIMEOUT_MS must be positive, got {timeout_ms}"
        )

    return Config(host, port, devices, tuple(attributes), hdbpp, timeout_ms)


@dataclass
class DeviceReading:
    """One device's result in one scrape. ok is False when the device as a whole
    could not be read; values holds the attributes read, failed the ones that failed."""

    ok: bool
    state: DevState | None = None
    values: dict | None = None
    failed: frozenset = frozenset()


class _DeviceWorker:
    """Owns one device's proxy; reads it on request, in its own PyTangoThread."""

    _STOP = object()

    def __init__(self, name: str, attributes: tuple[str, ...], timeout_ms: int):
        self.name = name
        self.attributes = ("State", *attributes)
        self.timeout_ms = timeout_ms
        self._requests: queue.Queue = queue.Queue()
        self._proxy = None
        self._thread = PyTangoThread(
            target=self._run, name=f"tango-exporter {name}", daemon=True
        )
        self._thread.start()

    def request(self) -> Future:
        fut: Future = Future()
        self._requests.put(fut)
        return fut

    def stop(self, timeout: float) -> None:
        self._requests.put(self._STOP)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            fut = self._requests.get()
            if fut is self._STOP:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            fut.set_result(self._read())

    def _read(self) -> DeviceReading:
        try:
            if self._proxy is None:
                proxy = tango.DeviceProxy(self.name)
                proxy.set_timeout_millis(self.timeout_ms)
                self._proxy = proxy
            results = self._proxy.read_attributes(list(self.attributes))
        except tango.DevFailed as e:
            log.warning("%s: not readable: %s", self.name, _reason(e))
            return DeviceReading(ok=False)
        values, failed, state = {}, set(), None
        for name, da in zip(self.attributes, results, strict=True):
            if da.has_failed or da.value is None:
                failed.add(name)
            elif name == "State":
                state = da.value
            else:
                values[name] = da.value
        if state is None:
            # The device answered but its State read failed: count it as not readable.
            return DeviceReading(ok=False)
        return DeviceReading(True, state, values, frozenset(failed))


def _reason(e: tango.DevFailed) -> str:
    err = e.args[0]
    return f"{err.reason}: {err.desc.strip()[:160]}"


def _short(full_name: str) -> str:
    """domain/family/member/attribute, without tango://host:port/ (as 85-hdbpp)."""
    return (
        full_name.split("/", 3)[-1] if full_name.startswith("tango://") else full_name
    )


class TangoCollector(Collector):
    """A prometheus_client collector that reads the configured devices per scrape."""

    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._errors = {(d, "State"): 0 for d in config.devices}
        for a in config.attributes:
            self._errors[(a.device, a.attribute)] = 0
        per_device = {d: [] for d in config.devices}
        for a in config.attributes:
            per_device[a.device].append(a.attribute)
        for es in config.hdbpp:
            per_device[es] += [n for n in HDBPP_ATTRIBUTES if n not in per_device[es]]
        self._workers = {
            d: _DeviceWorker(d, tuple(attrs), config.timeout_ms)
            for d, attrs in per_device.items()
        }

    def close(self) -> None:
        for w in self._workers.values():
            w.stop(timeout=self.config.timeout_ms / 1000 + DEADLINE_MARGIN_S)

    def _read_all(self) -> dict[str, DeviceReading]:
        futures = {d: w.request() for d, w in self._workers.items()}
        deadline = self.config.timeout_ms / 1000 + DEADLINE_MARGIN_S
        wait_futures(futures.values(), timeout=deadline)
        readings = {}
        for d, fut in futures.items():
            if fut.done():
                readings[d] = fut.result()
            else:
                fut.cancel()  # its worker is still busy; the result is not waited for
                log.warning("%s: no answer within %.1f s", d, deadline)
                readings[d] = DeviceReading(ok=False)
        return readings

    def collect(self) -> Iterable:
        with self._lock:
            t0 = time.monotonic()
            readings = self._read_all()
            families = list(self._families(readings))
            duration = GaugeMetricFamily(
                "tango_exporter_scrape_duration_seconds",
                "Time this exporter took to read every device in this scrape",
            )
            duration.add_metric([], time.monotonic() - t0)
            families.append(duration)
        return families

    def _families(self, readings: dict[str, DeviceReading]):
        up = GaugeMetricFamily(
            "tango_device_up",
            "1 if the device's State could be read",
            labels=["device"],
        )
        state = GaugeMetricFamily(
            "tango_device_state",
            "The device's State, one-hot: 1 for its current state",
            labels=["device", "state"],
        )
        for d in self.config.devices:
            r = readings[d]
            up.add_metric([d], 1 if r.ok else 0)
            if r.ok:
                current = str(r.state)
                for s in STATE_NAMES:
                    state.add_metric([d, s], 1 if s == current else 0)
            else:
                self._errors[(d, "State")] += 1
        yield up
        yield state

        value = GaugeMetricFamily(
            "tango_attribute_value",
            "A numeric or boolean scalar attribute's value",
            labels=["device", "attribute"],
        )
        count = CounterMetricFamily(
            "tango_attribute_count",
            "A count the device keeps itself (resets at the device's Init)",
            labels=["device", "attribute"],
        )
        attr_up = GaugeMetricFamily(
            "tango_attribute_up",
            "1 if the attribute could be read in this scrape",
            labels=["device", "attribute"],
        )
        for a in self.config.attributes:
            r = readings[a.device]
            v = r.values.get(a.attribute) if r.ok else None
            if v is None:
                self._errors[(a.device, a.attribute)] += 1
                attr_up.add_metric([a.device, a.attribute], 0)
                continue
            attr_up.add_metric([a.device, a.attribute], 1)
            (count if a.counter else value).add_metric(
                [a.device, a.attribute], float(v)
            )
        yield value
        yield count
        yield attr_up

        errors = CounterMetricFamily(
            "tango_read_errors",
            "Reads this exporter saw fail since it started "
            '(attribute="State": the device could not be read at all)',
            labels=["device", "attribute"],
        )
        for (d, a), n in sorted(self._errors.items()):
            errors.add_metric([d, a], n)
        yield errors

        yield from self._hdbpp_families(readings)

    def _hdbpp_families(self, readings: dict[str, DeviceReading]):
        attrs = GaugeMetricFamily(
            "hdbpp_es_attributes",
            "Attributes the EventSubscriber archives, by status",
            labels=["device", "status"],
        )
        valid = GaugeMetricFamily(
            "hdbpp_es_window_valid",
            "1 if the EventSubscriber's last 60 s statistics window is complete",
            labels=["device"],
        )
        failures = GaugeMetricFamily(
            "hdbpp_es_window_failures",
            "Failed events or stores in the EventSubscriber's last 60 s window",
            labels=["device"],
        )
        per_failures = GaugeMetricFamily(
            "hdbpp_es_attribute_window_failures",
            "Failed events or stores of one archived attribute in the last 60 s window",
            labels=["device", "attribute"],
        )
        per_records = GaugeMetricFamily(
            "hdbpp_es_attribute_window_records",
            "Records stored for one archived attribute in the last 60 s window",
            labels=["device", "attribute"],
        )
        age = GaugeMetricFamily(
            "hdbpp_es_statistics_age_seconds",
            "Seconds since the EventSubscriber's statistics were reset",
            labels=["device"],
        )
        for es in self.config.hdbpp:
            r = readings[es]
            if not r.ok:
                continue
            v = r.values
            for status, name in (
                ("ok", "AttributeOkNumber"),
                ("nok", "AttributeNokNumber"),
                ("pending", "AttributePendingNumber"),
            ):
                if name in v:
                    attrs.add_metric([es, status], float(v[name]))
            if "StatisticsResetTime" in v:
                age.add_metric([es], float(v["StatisticsResetTime"]))
            names = [_short(n).lower() for n in (v.get("AttributeList") or [])]
            fail_list = v.get("AttributeFailureFreqList")
            rec_list = v.get("AttributeRecordFreqList")
            total = v.get("AttributeFailureFreq")
            complete = (
                total is not None
                and fail_list is not None
                and rec_list is not None
                and len(fail_list) == len(names) == len(rec_list)
            )
            valid.add_metric([es], 1 if complete else 0)
            if not complete:
                continue
            failures.add_metric([es], float(total))
            for n, f, rec in zip(names, fail_list, rec_list, strict=True):
                per_failures.add_metric([es, n], float(f))
                per_records.add_metric([es, n], float(rec))
        yield attrs
        yield valid
        yield failures
        yield per_failures
        yield per_records
        yield age


class Server:
    """The HTTP endpoint and the collector behind it; close() stops both."""

    def __init__(self, config: Config):
        self.collector = TangoCollector(config)
        self.registry = CollectorRegistry()
        self.registry.register(self.collector)
        ProcessCollector(registry=self.registry)
        self._httpd, self._thread = start_http_server(
            config.listen_port, addr=config.listen_host, registry=self.registry
        )
        self.port = self._httpd.server_port

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        self.collector.close()


def serve(config: Config) -> Server:
    return Server(config)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = config_from_env(os.environ)
    except ConfigError as e:
        log.error("refusing to start: %s", e)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    server = serve(config)
    log.info(
        "serving /metrics on %s:%d for %d device(s), %d attribute(s), %d HDB++ "
        "subscriber(s); timeout %d ms",
        config.listen_host,
        server.port,
        len(config.devices),
        len(config.attributes),
        len(config.hdbpp),
        config.timeout_ms,
    )
    stop.wait()
    log.info("stopping")
    server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
