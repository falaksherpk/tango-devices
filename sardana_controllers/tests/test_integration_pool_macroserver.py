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
"""
import time

import pytest
import tango


DOOR_NAME = "Door/linac/1"


def run_and_wait(door, args, timeout=15):
    door.RunMacro(args)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if door.state() != tango.DevState.RUNNING:
            break
        time.sleep(0.2)


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

    run_and_wait(door, ["senv", "ActiveMntGrp", "fc_mg"])
    run_and_wait(
        door,
        ["ascan", "magnet_mot", str(start - 0.5), str(start + 0.5), "2", "0.3"],
    )

    output_lines = list(door.Output)
    data_lines = [
        line for line in output_lines if line.strip().startswith(("0", "1", "2"))
    ]
    assert len(data_lines) == 3

    timestamps = [float(line.split()[-1]) for line in data_lines]
    assert timestamps == sorted(timestamps)
    assert all(t > 0 for t in timestamps)

    final_position = mot.position
    assert abs(final_position - (start + 0.5)) < 0.01
