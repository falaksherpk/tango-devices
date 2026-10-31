"""Widgets for the beamline operator panel (handbook Part 3, Ch32)."""

import time
import weakref

import pyqtgraph as pg
from taurus.external.qt import Qt
from taurus.qt.qtgui.button import TaurusCommandButton
from taurus.qt.qtgui.panel import TaurusForm
from taurus_pyqtgraph import TaurusTrend

# ISA-101: a low-saturation gray base; the measured value is the strong line,
# the target a thin dashed reference. Color stays reserved for deviations
# (Taurus's quality colors in the form), so the curves are grayscale.
BACKGROUND = "#e6e6e6"
FOREGROUND = "#303030"
CURVE_PENS = {
    "current": {"color": "#202020", "width": 2},
    "setpoint": {"color": "#707070", "width": 1, "style": Qt.Qt.DashLine},
}
# Legend text per attribute name. Taurus names its curves "current[0]" and finds
# them by that name, so only the legend's label changes, never the curve name.
CURVE_LABELS = {"current": "current (A)", "setpoint": "setpoint (A)"}


class OperatorForm(TaurusForm):
    """A TaurusForm whose rows start at the top of the panel.

    TaurusForm.fillWithChildren() (5.4.1) puts an expanding spacer above the
    rows as well as below them, so a few rows sit in the middle of a tall panel
    (Ch32, 31 Oct). After every fill, the top spacer is collapsed.
    """

    def fillWithChildren(self):
        super().fillWithChildren()
        layout = self.scrollArea.widget().layout()
        top = layout.itemAt(0).spacerItem() if layout.count() else None
        if top is None:
            self.warning("no top spacer found; rows stay where Taurus put them")
            return
        top.changeSize(0, 0, Qt.QSizePolicy.Minimum, Qt.QSizePolicy.Fixed)
        layout.invalidate()


class OperatorTrend(TaurusTrend):
    """A TaurusTrend for the operator screen.

    - Reads its attributes every second. TaurusTrend plots one point per change
      event; a device that pushes events only while its value changes (the
      magnet's current, Ch24) gives a constant value a single point: no line,
      and an auto-ranged time axis collapsed to microseconds (Ch32 Q1).
    - Draws on the ISA-101 gray base, with fixed pens per attribute name and
      plain legend labels ("current (A)", not "current[0]"). The curves only
      exist once their first value arrives (and are rebuilt when a device comes
      back), so a timer styles each new one as it appears.
    """

    FORCED_READ_MS = 1000

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setForcedReadingPeriod(self.FORCED_READ_MS)
        self.setBackground(BACKGROUND)
        for side in ("left", "bottom"):
            axis = self.getAxis(side)
            axis.setPen(FOREGROUND)
            axis.setTextPen(FOREGROUND)
        self.getAxis("bottom").enableAutoSIPrefix(False)  # no "(x1e+09)"
        # The axis shows the client's local time; beamlinehost runs Etc/UTC.
        self.setLabel("bottom", f"time ({time.strftime('%Z')})")
        self._styled = weakref.WeakSet()
        self._styler = Qt.QTimer(self)
        self._styler.timeout.connect(self._style_new_curves)
        self._styler.start(self.FORCED_READ_MS)

    def _style_new_curves(self):
        legend = self.getPlotItem().legend
        for item in self.getPlotItem().listDataItems():
            attr = (item.name() or "").split("[")[0]
            if item not in self._styled:
                pen = CURVE_PENS.get(attr)
                if pen is not None:
                    item.setPen(pg.mkPen(**pen))
                    self._styled.add(item)
            text = CURVE_LABELS.get(attr)
            label = legend.getLabel(item) if legend is not None else None
            if text is not None and label is not None and label.text != text:
                label.setText(text)
        if legend is not None:
            legend.setBrush(pg.mkBrush(BACKGROUND))
            legend.setLabelTextColor(FOREGROUND)


class OperatorCommandButton(TaurusCommandButton):
    """A TaurusCommandButton whose confirmation defaults to Cancel.

    TaurusCommandButton (5.4.1) confirms a dangerous command with OK as the
    default button, so Enter or Space runs it (Ch32, 30 Oct). This button asks
    itself, with a warning icon and Cancel as the default, then lets Taurus run
    the command with its own question switched off for that one call.
    """

    def _onClicked(self):
        if not self.isDangerous() or self.getForceDangerousOperations():
            return super()._onClicked()
        answer = Qt.QMessageBox.warning(
            self,
            "Confirm action",
            f"{self.getDangerMessage()}\n\nProceed?",
            Qt.QMessageBox.Ok | Qt.QMessageBox.Cancel,
            Qt.QMessageBox.Cancel,
        )
        if answer != Qt.QMessageBox.Ok:
            self.info("%s cancelled by the operator", self.getCommand())
            return None
        self.info("%s confirmed by the operator", self.getCommand())
        self.setForceDangerousOperations(True)
        try:
            return super()._onClicked()
        finally:
            self.setForceDangerousOperations(False)
