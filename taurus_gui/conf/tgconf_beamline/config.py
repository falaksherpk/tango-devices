"""
Chapter 8 -- minimal TaurusGui config for the beamline operator panel.

Runs on beamlinehost (the real GUI client host, per LAB 8.1's decision),
against the real Chapter 2 magnet device and Chapter 7's real Sardana
elements. Deliberately minimal for the first real run: one TaurusForm,
one TaurusTrend, both against the real magnet -- no synoptic, no
Sardana instrument auto-discovery, no macro console yet. Those are
real, separate next steps once this minimal config is proven to work.
"""
from taurus.qt.qtgui.taurusgui.utils import PanelDescription

GUI_NAME = "BEAMLINE OPERATOR PANEL"
ORGANIZATION = "Beamline Lab"

SYNOPTIC = None
INSTRUMENTS_FROM_POOL = False

magnet_form = PanelDescription(
    "Magnet",
    classname="taurus.qt.qtgui.panel:TaurusForm",
    model=[
        "linac/magnet/q1/state",
        "linac/magnet/q1/current",
        "linac/magnet/q1/setpoint",
    ],
)

magnet_reset = PanelDescription(
    "Magnet Reset",
    classname="taurus.qt.qtgui.button:TaurusCommandButton",
    model="linac/magnet/q1",
    widget_properties={"Command": "Reset", "CustomText": "Reset Magnet"},
)

magnet_trend = PanelDescription(
    "Magnet Trend",
    classname="taurus_pyqtgraph:TaurusTrend",
    model=["linac/magnet/q1/current"],
)
