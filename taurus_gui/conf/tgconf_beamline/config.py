"""
Beamline operator panel -- TaurusGui configuration (handbook Part 3, Ch32).

One tab per device, each built against the design brief (ISA-101 information levels
and screen zones), not widget by widget:

- Level 2, at a glance: the device's State and Status and its process values in a
  form, and a trend of what moves.
- Act: buttons for the device's commands, one way to act per thing; a dangerous one
  asks for confirmation, Cancel as the default. Limits are enforced by the devices.
- Expert items (polling, archiving, Init, the instruments' links) stay off the tabs;
  Jive is the diagnostic level.

A tab whose device does not answer says so and keeps checking; the other tabs work
(widgets.DeviceTabs). The status bar shows every device's State.

Runs on beamlinehost, the operator's client, never on tango-ds. Start it with the
launcher, which lays the panels out and maximizes the window:
    beamline-gui-env python3 -I taurus_gui/beamline_gui.py
(or beamline-gui, installed by the operator_gui role). Never run it while the
Sardana tests, or a tango-devices pipeline, run against the same devices: they move
the live magnet (Ch8, 11 Sep 2026).
"""

from pathlib import Path

from taurus.qt.qtgui.taurusgui.utils import PanelDescription

# Imported here so that "tgconf_beamline.widgets" is already loaded when TaurusGui
# imports the panel class by module name.
from .widgets import DeviceTabs  # noqa: F401

GUI_NAME = "BEAMLINE OPERATOR PANEL"
ORGANIZATION = "Beamline Lab"

# The help panel can't be switched off from a config file; it shows the operator
# notes next to this file instead of the Taurus home page.
MANUAL_URI = (Path(__file__).parent / "operator_notes.html").as_uri()

SYNOPTIC = None
INSTRUMENTS_FROM_POOL = False

# One spec per tab, in tab order. form: attributes, in rows; read_only: form rows
# shown without a write widget; tango_format: form rows shown in their Tango format
# (set in the Tango database by the tango_ds role); trend: (attribute, legend label)
# pairs, measured value first; actions: command buttons, confirm is the danger
# message (None: no confirmation).
TABS = [
    {
        "title": "Magnet",
        "device": "linac/magnet/q1",
        "form": ["State", "Status", "current", "setpoint"],
        "trend": {"curves": [("current", "current (A)"), ("setpoint", "setpoint (A)")]},
        "actions": [
            {
                "command": "Reset",
                "text": "Reset magnet (ramp to 0 A)",
                "confirm": "The magnet will ramp from its present current to 0 A.",
            },
        ],
    },
    {
        "title": "Vacuum",
        "device": "linac/vacuum/gauge1",
        "form": ["State", "Status", "VacuumPressure", "PumpStatus"],
        "read_only": ["PumpStatus"],
        "tango_format": ["VacuumPressure"],
        "trend": {"curves": [("VacuumPressure", "pressure (mbar)")], "log_y": True},
        "actions": [
            {"command": "PumpOn", "text": "Pump ON", "confirm": None},
            {
                "command": "PumpOff",
                "text": "Pump OFF",
                "confirm": "The vacuum pump will stop; the pressure will stop falling.",
            },
        ],
    },
]

devices = PanelDescription(
    "Devices",
    classname="tgconf_beamline.widgets:DeviceTabs",
    widget_properties={"tabSpecs": TABS},
    floating=False,
)
