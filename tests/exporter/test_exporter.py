"""Tests for beamline_tango.exporter, the Prometheus exporter (handbook Part 4, Ch36).

The exporter is tested against small devices defined here, not the lab's devices: what
is under test is how it reads, labels and counts, and what it does when a device fails,
hangs or is gone. MultiDeviceTestContext(process=True) runs them in one server
subprocess without a Tango database; the exporter reaches them by their full names
(tango://host:port/domain/family/member#dbase=no), as a client on another host would.
One server process serves every device, but Tango serializes calls per device, so a
device whose read hangs does not hold up its neighbours (which test_hung_device uses).
"""

import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import tango
from prometheus_client.parser import text_string_to_metric_families
from tango import DevState
from tango.server import Device, attribute, command
from tango.test_context import MultiDeviceTestContext

from beamline_tango import exporter

TIMEOUT_MS = 500


class ExporterTarget(Device):
    """A device whose State and reads the tests control through commands."""

    def init_device(self):
        super().init_device()
        self._value = 1.5
        self._count = 0
        self._fail = False
        self._delay = 0.0
        self._state_fails = False
        self.set_state(DevState.ON)

    def dev_state(self):
        if self._state_fails:
            raise RuntimeError("simulated State failure")
        return super().dev_state()

    @attribute(dtype=float)
    def value(self):
        if self._fail:
            raise RuntimeError("simulated read failure")
        if self._delay:
            time.sleep(self._delay)
        return self._value

    @attribute(dtype="int64")
    def count(self):
        self._count += 1
        return self._count

    @attribute(dtype=bool)
    def flag(self):
        return True

    @command(dtype_in=str)
    def SetStateName(self, name):
        self.set_state(DevState.names[name])

    @command(dtype_in=bool)
    def SetFail(self, fail):
        self._fail = fail

    @command(dtype_in=float)
    def SetDelay(self, seconds):
        self._delay = seconds

    @command(dtype_in=bool)
    def SetStateFails(self, fails):
        self._state_fails = fails


class FakeEventSubscriber(Device):
    """The attributes of an HDB++ EventSubscriber that the exporter reads."""

    def init_device(self):
        super().init_device()
        self._names = [
            "tango://tango-db.beamline:10000/linac/beam/faradaycup1/current",
            "tango://tango-db.beamline:10000/linac/magnet/q1/current",
        ]
        self._failures = [2.0, 0.0]
        self._window_valid = True
        self.set_state(DevState.ON)

    @attribute(dtype=int)
    def AttributeOkNumber(self):
        return 1

    @attribute(dtype=int)
    def AttributeNokNumber(self):
        return 1

    @attribute(dtype=int)
    def AttributePendingNumber(self):
        return 0

    @attribute(dtype=(str,), max_dim_x=1024)
    def AttributeList(self):
        return self._names

    @attribute(dtype=float)
    def AttributeFailureFreq(self):
        if not self._window_valid:
            raise RuntimeError("no complete statistics window yet")
        return sum(self._failures)

    @attribute(dtype=(float,), max_dim_x=1024)
    def AttributeFailureFreqList(self):
        return self._failures

    @attribute(dtype=(float,), max_dim_x=1024)
    def AttributeRecordFreqList(self):
        return [6.0, 6.0]

    @attribute(dtype=float)
    def StatisticsResetTime(self):
        return 75.0

    @command(dtype_in=(float,))
    def SetFailures(self, failures):
        self._failures = list(failures)

    @command(dtype_in=bool)
    def SetWindowValid(self, valid):
        self._window_valid = valid


DEVICES_INFO = (
    {
        "class": ExporterTarget,
        "devices": (
            {"name": "test/exp/1"},
            {"name": "test/exp/2"},
        ),
    },
    {"class": FakeEventSubscriber, "devices": ({"name": "test/hdbpp/es1"},)},
)


@pytest.fixture(scope="module")
def context():
    with MultiDeviceTestContext(DEVICES_INFO, process=True) as ctx:
        yield ctx


@pytest.fixture
def names(context):
    """Full names of the three devices, and a fourth that no server serves."""
    exp1 = context.get_device_access("test/exp/1")
    exp2 = context.get_device_access("test/exp/2")
    es1 = context.get_device_access("test/hdbpp/es1")
    host_port = exp1.split("/")[2]
    gone = f"tango://{host_port.split(':')[0]}:1/test/exp/gone#dbase=no"
    return {"exp1": exp1, "exp2": exp2, "es1": es1, "gone": gone}


@pytest.fixture(autouse=True)
def reset(names):
    """Put every test device back in its healthy state after each test."""
    yield
    for key in ("exp1", "exp2"):
        p = tango.DeviceProxy(names[key])
        p.SetStateName("ON")
        p.SetFail(False)
        p.SetDelay(0.0)
        p.SetStateFails(False)
    es = tango.DeviceProxy(names["es1"])
    es.SetFailures([2.0, 0.0])
    es.SetWindowValid(True)


def make_env(names, devices=("exp1", "exp2"), hdbpp=()):
    devs = [names[d] for d in devices] + [names[h] for h in hdbpp]
    attrs = []
    for d in devices:
        attrs += [f"{names[d]}/value", f"{names[d]}/flag", f"{names[d]}/count:counter"]
    return {
        "TANGO_EXPORTER_LISTEN": "127.0.0.1:0",
        "TANGO_EXPORTER_DEVICES": " ".join(devs),
        "TANGO_EXPORTER_ATTRIBUTES": " ".join(attrs),
        "TANGO_EXPORTER_HDBPP": " ".join(names[h] for h in hdbpp),
        "TANGO_EXPORTER_TIMEOUT_MS": str(TIMEOUT_MS),
    }


@pytest.fixture
def scrape():
    """scrape(env) -> {(metric_name, frozenset(labels.items())): value}; the
    collector (and its worker threads) is closed after the test."""
    collectors = []

    def _scrape(env, collector=None):
        if collector is None:
            collector = exporter.TangoCollector(exporter.config_from_env(env))
            collectors.append(collector)
        return samples(collector), collector

    yield _scrape
    for c in collectors:
        c.close()


def samples(collector):
    out = {}
    for family in collector.collect():
        for s in family.samples:
            out[(s.name, frozenset(s.labels.items()))] = s.value
    return out


def value(result, name, **labels):
    return result.get((name, frozenset(labels.items())))


# -- configuration ----------------------------------------------------------------


def test_config_from_env_parses_every_setting(names):
    cfg = exporter.config_from_env(make_env(names, hdbpp=("es1",)))
    assert (cfg.listen_host, cfg.listen_port) == ("127.0.0.1", 0)
    assert cfg.devices == (names["exp1"], names["exp2"], names["es1"])
    assert cfg.hdbpp == (names["es1"],)
    assert cfg.timeout_ms == TIMEOUT_MS
    count = [a for a in cfg.attributes if a.attribute == "count"]
    assert [a.counter for a in count] == [True, True]
    assert all(not a.counter for a in cfg.attributes if a.attribute != "count")


@pytest.mark.parametrize(
    ("key", "bad", "message"),
    [
        ("TANGO_EXPORTER_LISTEN", "", "TANGO_EXPORTER_LISTEN"),
        ("TANGO_EXPORTER_LISTEN", "10.10.10.32", "host:port"),
        ("TANGO_EXPORTER_LISTEN", "10.10.10.32:http", "host:port"),
        ("TANGO_EXPORTER_DEVICES", "  ", "TANGO_EXPORTER_DEVICES"),
        ("TANGO_EXPORTER_TIMEOUT_MS", "fast", "TANGO_EXPORTER_TIMEOUT_MS"),
        ("TANGO_EXPORTER_TIMEOUT_MS", "0", "TANGO_EXPORTER_TIMEOUT_MS"),
        ("TANGO_EXPORTER_ATTRIBUTES", "linac/beam/faradaycup1/current", "not in"),
        ("TANGO_EXPORTER_ATTRIBUTES", "noslash", "device/attribute"),
        ("TANGO_EXPORTER_HDBPP", "archiving/hdbpp/other", "not in"),
    ],
)
def test_config_refuses_bad_settings(names, key, bad, message):
    env = make_env(names)
    env[key] = bad
    with pytest.raises(exporter.ConfigError, match=message):
        exporter.config_from_env(env)


def test_config_refuses_an_unknown_attribute_suffix(names):
    env = make_env(names)
    env["TANGO_EXPORTER_ATTRIBUTES"] = f"{names['exp1']}/count:gauge"
    with pytest.raises(exporter.ConfigError, match="suffix"):
        exporter.config_from_env(env)


# -- healthy devices -----------------------------------------------------------------


def test_healthy_devices(names, scrape):
    r, _ = scrape(make_env(names))
    for d in ("exp1", "exp2"):
        dev = names[d]
        assert value(r, "tango_device_up", device=dev) == 1
        states = {
            dict(labels)["state"]: v
            for (n, labels), v in r.items()
            if n == "tango_device_state" and dict(labels)["device"] == dev
        }
        assert set(states) == set(DevState.names)
        assert states["ON"] == 1
        assert sum(states.values()) == 1
        assert value(r, "tango_attribute_value", device=dev, attribute="value") == 1.5
        assert value(r, "tango_attribute_value", device=dev, attribute="flag") == 1.0
        for attr in ("State", "value", "flag", "count"):
            assert value(r, "tango_read_errors_total", device=dev, attribute=attr) == 0
        for attr in ("value", "flag", "count"):
            assert value(r, "tango_attribute_up", device=dev, attribute=attr) == 1
        # A counter is exported as a counter only, never also as a gauge.
        assert value(r, "tango_attribute_value", device=dev, attribute="count") is None


def test_counter_attribute_follows_the_device(names, scrape):
    r1, collector = scrape(make_env(names))
    r2, _ = scrape(None, collector)
    dev = names["exp1"]
    c1 = value(r1, "tango_attribute_count_total", device=dev, attribute="count")
    c2 = value(r2, "tango_attribute_count_total", device=dev, attribute="count")
    assert c1 >= 1
    assert c2 == c1 + 1


def test_state_change_is_reported(names, scrape):
    tango.DeviceProxy(names["exp1"]).SetStateName("FAULT")
    r, _ = scrape(make_env(names))
    dev = names["exp1"]
    assert value(r, "tango_device_up", device=dev) == 1
    assert value(r, "tango_device_state", device=dev, state="FAULT") == 1
    assert value(r, "tango_device_state", device=dev, state="ON") == 0


# -- failures ----------------------------------------------------------------------


def test_failed_attribute_counts_an_error_and_spares_the_rest(names, scrape):
    tango.DeviceProxy(names["exp1"]).SetFail(True)
    r1, collector = scrape(make_env(names))
    r2, _ = scrape(None, collector)
    dev = names["exp1"]
    for r, errors in ((r1, 1), (r2, 2)):
        assert value(r, "tango_device_up", device=dev) == 1
        assert value(r, "tango_device_state", device=dev, state="ON") == 1
        assert value(r, "tango_attribute_up", device=dev, attribute="value") == 0
        assert value(r, "tango_attribute_value", device=dev, attribute="value") is None
        assert (
            value(r, "tango_read_errors_total", device=dev, attribute="value") == errors
        )
        assert value(r, "tango_attribute_value", device=dev, attribute="flag") == 1.0
        assert value(r, "tango_read_errors_total", device=dev, attribute="flag") == 0
    assert (
        value(r2, "tango_attribute_value", device=names["exp2"], attribute="value")
        == 1.5
    )


def test_device_whose_state_cannot_be_read_is_down(names, scrape):
    # The device answers, but its State read fails: down, not some guessed state.
    tango.DeviceProxy(names["exp1"]).SetStateFails(True)
    r, _ = scrape(make_env(names))
    dev = names["exp1"]
    assert value(r, "tango_device_up", device=dev) == 0
    assert not [
        k for k in r if k[0] == "tango_device_state" and dict(k[1])["device"] == dev
    ]
    assert value(r, "tango_read_errors_total", device=dev, attribute="State") == 1
    assert value(r, "tango_device_up", device=names["exp2"]) == 1


def test_device_that_no_server_serves(names, scrape):
    env = make_env(names, devices=("exp1", "gone"))
    r, _ = scrape(env)
    gone = names["gone"]
    assert value(r, "tango_device_up", device=gone) == 0
    assert not [
        k for k in r if k[0] == "tango_device_state" and dict(k[1])["device"] == gone
    ]
    assert value(r, "tango_read_errors_total", device=gone, attribute="State") == 1
    assert value(r, "tango_attribute_up", device=gone, attribute="value") == 0
    assert value(r, "tango_read_errors_total", device=gone, attribute="value") == 1
    assert value(r, "tango_device_up", device=names["exp1"]) == 1


def test_hung_device_costs_one_timeout_not_the_scrape(names, scrape):
    tango.DeviceProxy(names["exp1"]).SetDelay(3.0)
    t0 = time.monotonic()
    r, _ = scrape(make_env(names))
    elapsed = time.monotonic() - t0
    assert value(r, "tango_device_up", device=names["exp1"]) == 0
    assert value(r, "tango_device_up", device=names["exp2"]) == 1
    assert (
        value(r, "tango_attribute_value", device=names["exp2"], attribute="value")
        == 1.5
    )
    # One timeout (0.5 s) plus margin, not a sum over devices, not the device's 3 s.
    assert elapsed < 2.0, elapsed
    # Let the hung read finish before the next test reuses the device.
    time.sleep(3.0)


def test_hung_devices_are_read_in_parallel(names, scrape):
    # With one hung device, reading the devices one after another would pass the test
    # above too. Two hung devices with a 1 s timeout take about 1 s when read in
    # parallel, about 2 s when read in turn.
    for d in ("exp1", "exp2"):
        tango.DeviceProxy(names[d]).SetDelay(3.0)
    env = make_env(names)
    env["TANGO_EXPORTER_TIMEOUT_MS"] = "1000"
    t0 = time.monotonic()
    r, _ = scrape(env)
    elapsed = time.monotonic() - t0
    assert value(r, "tango_device_up", device=names["exp1"]) == 0
    assert value(r, "tango_device_up", device=names["exp2"]) == 0
    assert elapsed < 1.6, elapsed
    time.sleep(3.0)


# -- HDB++ ---------------------------------------------------------------------------


def test_hdbpp_event_subscriber(names, scrape):
    r, _ = scrape(make_env(names, hdbpp=("es1",)))
    es = names["es1"]
    assert value(r, "tango_device_up", device=es) == 1
    assert value(r, "hdbpp_es_attributes", device=es, status="ok") == 1
    assert value(r, "hdbpp_es_attributes", device=es, status="nok") == 1
    assert value(r, "hdbpp_es_attributes", device=es, status="pending") == 0
    assert value(r, "hdbpp_es_window_valid", device=es) == 1
    assert value(r, "hdbpp_es_window_failures", device=es) == 2.0
    cup = "linac/beam/faradaycup1/current"
    magnet = "linac/magnet/q1/current"
    assert (
        value(r, "hdbpp_es_attribute_window_failures", device=es, attribute=cup) == 2.0
    )
    assert (
        value(r, "hdbpp_es_attribute_window_failures", device=es, attribute=magnet) == 0
    )
    assert (
        value(r, "hdbpp_es_attribute_window_records", device=es, attribute=cup) == 6.0
    )
    assert value(r, "hdbpp_es_statistics_age_seconds", device=es) == 75.0


def test_hdbpp_without_a_complete_window(names, scrape):
    tango.DeviceProxy(names["es1"]).SetWindowValid(False)
    r, _ = scrape(make_env(names, hdbpp=("es1",)))
    es = names["es1"]
    assert value(r, "hdbpp_es_window_valid", device=es) == 0
    assert value(r, "hdbpp_es_window_failures", device=es) is None
    assert not [k for k in r if k[0] == "hdbpp_es_attribute_window_failures"]
    # The attribute counts don't depend on the window.
    assert value(r, "hdbpp_es_attributes", device=es, status="ok") == 1


def test_hdbpp_misaligned_lists_name_nothing(names, scrape):
    tango.DeviceProxy(names["es1"]).SetFailures([1.0])
    r, _ = scrape(make_env(names, hdbpp=("es1",)))
    es = names["es1"]
    assert value(r, "hdbpp_es_window_valid", device=es) == 0
    assert not [k for k in r if k[0] == "hdbpp_es_attribute_window_failures"]


# -- the HTTP endpoint ---------------------------------------------------------------


def test_http_endpoint_serves_the_text_format(names):
    server = exporter.serve(exporter.config_from_env(make_env(names)))
    try:
        port = server.port
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/metrics", timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/plain")
            body = resp.read().decode()
    finally:
        server.close()
    families = {f.name: f for f in text_string_to_metric_families(body)}
    assert families["tango_device_up"].type == "gauge"
    assert families["tango_read_errors"].type == "counter"
    assert families["tango_attribute_count"].type == "counter"
    assert "tango_exporter_scrape_duration_seconds" in families
    assert "process_resident_memory_bytes" in families


# -- the entry point, as the unit starts it ------------------------------------------


def run_module(env, **kwargs):
    # The directory this test's beamline_tango came from: src/ in CI (pyproject's
    # pythonpath, which a subprocess doesn't inherit), site-packages in the
    # install test.
    package_root = str(Path(exporter.__file__).resolve().parents[1])
    path = os.pathsep.join(filter(None, [package_root, os.environ.get("PYTHONPATH")]))
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "beamline_tango.exporter"],
        env={**os.environ, **env, "PYTHONPATH": path},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


def test_entry_point_refuses_a_bad_configuration_with_exit_2(names):
    env = make_env(names)
    env["TANGO_EXPORTER_TIMEOUT_MS"] = "fast"
    proc = run_module(env)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 2, out
    assert "refusing to start: TANGO_EXPORTER_TIMEOUT_MS" in out


def test_entry_point_serves_and_stops_on_sigterm(names):
    proc = run_module(make_env(names))
    try:
        line = proc.stdout.readline()
        m = re.search(r"serving /metrics on 127\.0\.0\.1:(\d+)", line)
        assert m, line
        url = f"http://127.0.0.1:{m.group(1)}/metrics"
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
        assert f'tango_device_up{{device="{names["exp1"]}"}} 1.0' in body
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    assert "stopping" in out
