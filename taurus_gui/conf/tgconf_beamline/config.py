"""
Beamline operator panel -- TaurusGui configuration (handbook Part 3, Ch32).

Tab 1, the magnet (linac/magnet/q1), built against a written design brief
(ISA-101 information levels and screen zones), not widget by widget:

- Level 2, at a glance: State and Status, current next to setpoint, and a
  trend of both over the last minutes with wall-clock times.
- Act: a bounded setpoint write (the device refuses outside 0-100 A since
  Ch32) and a confirmed Reset in its own panel, Cancel as the default.
- Every panel docks into the main window (Taurus 5 panels float by default).
- Expert items (ramp_rate, polling, archiving, Init) stay off this screen;
  Jive is the diagnostic level.

Runs on beamlinehost, the operator's client, never on tango-ds. Start it with
the launcher, which checks the devices first and lays the panels out:
    beamline-gui-env python3 -I taurus_gui/beamline_gui.py
(plain "taurus gui" on this directory works, but does neither). Never run it
while the Sardana tests, or a tango-devices pipeline, run against the same
devices: they move the live magnet (Ch8, 11 Sep 2026).
"""

from pathlib import Path

from taurus.qt.qtgui.taurusgui.utils import PanelDescription

# Imported here so that "tgconf_beamline.widgets" is already loaded when
# TaurusGui imports the panel classes by module name.
from .widgets import OperatorCommandButton, OperatorForm, OperatorTrend  # noqa: F401

GUI_NAME = "BEAMLINE OPERATOR PANEL"
ORGANIZATION = "Beamline Lab"

# The help panel can't be switched off from a config file; it shows the
# operator notes next to this file instead of the Taurus home page.
MANUAL_URI = (Path(__file__).parent / "operator_notes.html").as_uri()

SYNOPTIC = None
INSTRUMENTS_FROM_POOL = False

MAGNET = "linac/magnet/q1"

magnet_form = PanelDescription(
    "Magnet",
    classname="tgconf_beamline.widgets:OperatorForm",
    model=[
        f"{MAGNET}/state",
        f"{MAGNET}/status",
        f"{MAGNET}/current",
        f"{MAGNET}/setpoint",
    ],
    floating=False,
)

magnet_trend = PanelDescription(
    "Magnet Trend",
    classname="tgconf_beamline.widgets:OperatorTrend",
    model=[f"{MAGNET}/current", f"{MAGNET}/setpoint"],
    floating=False,
)

magnet_reset = PanelDescription(
    "Magnet Actions",
    classname="tgconf_beamline.widgets:OperatorCommandButton",
    model=MAGNET,
    widget_properties={
        "Command": "Reset",
        "CustomText": "Reset magnet (ramp to 0 A)",
        "DangerMessage": "The magnet will ramp from its present current to 0 A.",
    },
    floating=False,
)
