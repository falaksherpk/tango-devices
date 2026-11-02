"""
LAB 7.8 -- integration tests for the Sardana Pool/MacroServer layer,
exercising the real, permanent magnet_mot / fc_ctr / fc_mg elements
registered in Pool_linac_1 (created in LAB 7.3-7.6).

Design note (deviation from the original LAB 7.8 plan): the original
design called for a namespaced test_linac Pool created and destroyed
per test run via defctrl/defelem/udefctrl/udefelem. That approach was
abandoned after reproducing a real, upstream Tango/Sardana race
(matches known issues e.g. pytango#22, cppTango#409) between
event-subscription threads and command-execution threads, which
crashed the real MacroServer six times in manual testing -- on
magnet-only sequences, Faraday-only sequences, and even a single
isolated udefctrl call, with no reliable mitigation via sleeps or
ordering found. Since CI reaches this same real, shared MacroServer
over the network (per LAB 7.10's reachability decision), an
unattended crash there is a materially worse outcome than one caught
interactively.

These tests instead exercise the pre-existing, permanent elements
directly -- no controller/element creation or deletion -- eliminating
the crash trigger entirely, at the cost of the originally-planned
namespace isolation.

Waiting for a macro (Ch31): RunMacro is asynchronous and returns before the Door is
RUNNING, so a wait that polls the Door's state can read ON and return before the
macro has even started (1 in 20 moves on the rebuilt fleet); the ascan test could
then read the previous scan's Output. run_and_wait follows the Door's State and
MacroStatus change events instead, and fails the test unless the macro finished.
"""

import json
import queue
import re
import time

import pytest
import tango


DOOR_NAME = "Door/linac/1"
MACRO_ENDS = ("finish", "stop", "abort", "exception")


def run_and_wait(door, args, timeout=15):
    """Run one macro on the Door and wait for its own end; fail unless it finished.

    The Door's State and MacroStatus change events arrive in order: the macro ended
    when the Door leaves RUNNING, and finished if MacroStatus says "finish" for its
    id. A macro rejected before it starts sends no MacroStatus and leaves the Door
    in ALARM.
    """
    events = queue.Queue()
    subscriptions = [
        door.subscribe_event(name, tango.EventType.CHANGE_EVENT, events.put)
        for name in ("State", "MacroStatus")
    ]
    try:
        macro_id = re.search(r'id="([^"]+)"', door.RunMacro(args)[0]).group(1)
        status, running, ended = None, False, None
        deadline = time.monotonic() + timeout
        while status is None:
            if ended is not None:
                # The Door has left RUNNING: give a late MacroStatus a moment.
                deadline = min(deadline, ended + 1)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = events.get(timeout=remaining)
            except queue.Empty:
                break
            if event.err:
                continue
            value = event.attr_value.value
            if event.attr_name.rsplit("/", 1)[-1].lower() == "macrostatus":
                for item in json.loads(value[1]) if value and value[1] else []:
                    if item.get("id") == macro_id and item.get("state") in MACRO_ENDS:
                        status = item["state"]
            elif value == tango.DevState.RUNNING:
                running = True
            elif running and ended is None:
                ended = time.monotonic()
    finally:
        for subscription in subscriptions:
            door.unsubscribe_event(subscription)
    if status == "finish":
        return
    if ended is None and status is None:
        pytest.fail(f"macro {args} did not end within {timeout} s")
    error = " ".join(door.read_attribute("Error").value or []).strip()
    pytest.fail(f"macro {args} ended {status or door.state()}: {error}")


@pytest.fixture
def door():
    return tango.DeviceProxy(DOOR_NAME)


@pytest.fixture
def restore_active_mntgrp(door):
    yield
    run_and_wait(door, ["senv", "ActiveMntGrp", "fc_mg"])


@pytest.fixture
def restore_magnet_position(door):
    mot = tango.DeviceProxy("motor/magnet_ctrl/1")
    original_position = mot.position

    yield mot

    run_and_wait(door, ["mv", "magnet_mot", str(original_position)])


def test_ct_via_measurement_group(door, restore_active_mntgrp):  # noqa: ARG001
    fc_ctr = tango.DeviceProxy("expchan/fc_ctrl/1")

    run_and_wait(door, ["senv", "ActiveMntGrp", "fc_mg"])
    run_and_wait(door, ["ct", "1.0"])

    reading = fc_ctr.value
    assert isinstance(reading, float)
    assert reading > 0


def test_ascan_records_real_increasing_timestamps(door, restore_magnet_position):
    mot = restore_magnet_position
    start = mot.position
    # A 1 A scan around where the magnet sits, kept inside the device's
    # setpoint limits (0-100 A since Ch32): a magnet left at 0 by a Reset
    # would otherwise scan from -0.5 A, which the device refuses.
    low = min(max(start - 0.5, 0.0), 99.0)
    high = low + 1.0

    run_and_wait(door, ["senv", "ActiveMntGrp", "fc_mg"])
    run_and_wait(door, ["ascan", "magnet_mot", str(low), str(high), "2", "0.3"])

    output_lines = list(door.Output)
    data_lines = [
        line for line in output_lines if line.strip().startswith(("0", "1", "2"))
    ]
    assert len(data_lines) == 3

    timestamps = [float(line.split()[-1]) for line in data_lines]
    assert timestamps == sorted(timestamps)
    assert all(t > 0 for t in timestamps)

    final_position = mot.position
    assert abs(final_position - high) < 0.01
