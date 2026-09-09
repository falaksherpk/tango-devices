"""
LAB 7.2 -- proving both controllers standalone, direct instantiation,
no Sardana Pool involved. Confirms StartOne/StateOne/ReadOne genuinely
work against the real, already-running Ch2/Ch4 devices before either
controller is ever wired into a live Pool.
"""
import sys
import time

sys.path.insert(0, ".")
from sardana_controllers.magnet_motor_ctrl import MagnetMotorController
from sardana_controllers.faraday_zerod_ctrl import FaradayZeroDController

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
    from sardana import State
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
