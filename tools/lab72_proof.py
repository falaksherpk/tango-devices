"""
LAB 7.2 -- proving both controllers standalone, direct instantiation,
no Sardana Pool involved. Confirms StartOne/StateOne/ReadOne genuinely
work against the real, already-running Ch2/Ch4 devices before either
controller is ever wired into a live Pool.

Guarded behind __main__: until tango-devices 1.0.0 this file lived under
Pool_linac_1's PoolPath, so Sardana's controller-library scan imported it on
every Pool startup (now in tools/, outside PoolPath; handbook Part 4, Ch33).
Without the guard, importing it (not running it) was executing this
proof against the real magnet on every Pool start -- confirmed via
beamlinehost-20260909.log and beamlinehost-20260910.log, both showing
unintended magnet moves.
"""

import sys
import time
from pathlib import Path

from sardana import State

# From a checkout the package is in src/ (Part 4 Ch33; it used to need
# PYTHONPATH=src, Ch38 item 7). On tango-ds the package runtime,
# /opt/beamline-tango/bin/python3, has it installed already.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from beamline_tango.sardana_controllers.magnet_motor_ctrl import MagnetMotorController
from beamline_tango.sardana_controllers.faraday_zerod_ctrl import FaradayZeroDController


def main():
    print("=== Magnet MotorController ===")
    mag = MagnetMotorController("test_inst", {"TangoDevice": "linac/magnet/q1"})
    mag.AddDevice(1)
    print("Initial:", mag.StateOne(1), "position:", mag.ReadOne(1))

    target = mag.ReadOne(1) + 2.0
    print(f"StartOne -> ramping to {target:.4f}")
    mag.StartOne(1, target)

    deadline = time.time() + 10
    while time.time() < deadline:
        state, status = mag.StateOne(1)
        print(f"  StateOne: {state}  ReadOne: {mag.ReadOne(1):.4f}")
        if state != State.Moving:
            break
        time.sleep(0.5)

    final_state, _ = mag.StateOne(1)
    final_pos = mag.ReadOne(1)
    print(f"Final: {final_state}, position={final_pos:.4f} (target was {target:.4f})")
    assert abs(final_pos - target) < 0.01, "did not settle at target"
    assert final_state == State.On, "did not return to State.On"
    print("MAGNET CONTROLLER: PASS")

    print()
    print("=== Faraday Cup ZeroDController ===")
    fc = FaradayZeroDController("test_inst", {"TangoDevice": "linac/beam/faradaycup1"})
    fc.AddDevice(1)
    state, status = fc.StateOne(1)
    reading = fc.ReadOne(1)
    print(f"StateOne: {state}  ReadOne: {reading:.3e} A")
    assert state == State.On
    assert reading > 0
    print("FARADAY CUP CONTROLLER: PASS")


if __name__ == "__main__":
    main()
