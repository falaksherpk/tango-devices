"""Widgets for the beamline operator panel (handbook Part 3, Ch32).

DeviceTabs holds one tab per device, built from the tab specs in config.py. A tab
whose device does not answer shows why and keeps checking, so one device down never
blocks the others (and no Taurus widget is ever built for a device that is down at
start: Taurus 5.4.1 turns that into a blocking error dialog and an empty form).
"""

import logging
import threading
import time
import weakref

import pyqtgraph as pg
import tango
import taurus
from taurus.core.tango.util import tangoFormatter
from taurus.core.taurusbasetypes import TaurusEventType
from taurus.external.qt import Qt
from taurus.qt.qtgui.button import TaurusCommandButton
from taurus.qt.qtgui.panel import TaurusForm
from taurus.qt.qtgui.panel.taurusvalue import CenteredLed, DefaultReadWidgetLabel
from taurus_pyqtgraph import TaurusTrend

# ISA-101: a low-saturation gray base; the measured value is the strong line, a
# target a thin dashed reference. Color stays reserved for deviations (Taurus's
# quality colors in the form, the status bar's device states), so curves are gray.
BACKGROUND = "#e6e6e6"
FOREGROUND = "#303030"
# Pens by a curve's position in its tab's trend list: the first is the measured
# value, the second a reference (a setpoint).
CURVE_PENS = (
    {"color": "#202020", "width": 2},
    {"color": "#707070", "width": 1, "style": Qt.Qt.DashLine},
)

# How long one device gets to answer a reachability ping, and how often a tab whose
# device did not answer asks again.
PING_TIMEOUT_MS = 2000
RETRY_INTERVAL_MS = 5000
# Plain words for the Tango reasons a ping fails with; the code stays alongside for
# whoever gets called.
PING_REASONS = {
    "API_CantConnectToDevice": "no connection",
    "API_DeviceNotExported": "not running",
    "API_DeviceNotDefined": "not in the Tango database",
    "API_DeviceTimedOut": "no answer in time",
}
# Default states in which an action is enabled; an action's "states" in config.TABS
# replaces them for that button. ALARM is one: the operator must be
# able to act on what raised it (tango-devices 2c759c8). FAULT, MOVING aside, means
# no contact with the instrument.
ACTION_STATES = (tango.DevState.ON, tango.DevState.MOVING, tango.DevState.ALARM)
# Status-bar colors for deviations only (ISA-101): normal states stay plain.
STATE_STYLES = {
    "ALARM": "background: #f0a000; color: #000000;",
    "FAULT": "background: #d02020; color: #ffffff;",
    "NO CONTACT": "background: #d02020; color: #ffffff;",
}


class RepeatFilter(logging.Filter):
    """Lets a log message through once per interval and logger, counting the rest.

    A trend reads its attributes every second; with a device down, Taurus's trend
    set logs the same warning each time (two lines a second for two curves, about
    7000 an hour: Ch32, 31 Oct). The first one still shows, then one a minute with
    the number held back.
    """

    def __init__(self, interval_s=60.0):
        super().__init__()
        self._interval_s = interval_s
        self._last = {}
        self._held = {}

    def filter(self, record):
        key = (record.name, record.msg)
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self._interval_s:
            self._held[key] = self._held.get(key, 0) + 1
            return False
        held = self._held.pop(key, 0)
        if held:
            record.msg = f"{record.msg} ({held} more since the last one)"
        self._last[key] = now
        return True


class StatusLabel(DefaultReadWidgetLabel):
    """A form row's read widget for a device's Status text.

    - Wraps a long text instead of trimming it. TaurusLabel replaces a text wider
      than the row by a bare "..." (the text only in its tooltip): a device's FAULT
      reason, the one Status the operator most needs, showed as "..." at any window
      width (Ch32, 31 Oct).
    - On the plain gray base. TaurusLabel colours by the attribute's quality, and
      Status is always VALID: a green "Lost contact ..." in FAULT. Colour comes
      from State, the status bar and the value rows (ISA-101; Ch32, 31 Oct).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAutoTrim(False)
        self.setWordWrap(True)
        self.setBgRole("none")


class TangoFormatLabel(DefaultReadWidgetLabel):
    """A form row's read widget that shows the value in its Tango format.

    Taurus's default formatter takes only the precision from the Tango format and
    always shows fixed-point: a pressure with format "%.2e" (tango_ds role) still
    read 0.01, and a real gauge's 1e-11 mbar would read 0.00 (Ch32, 31 Oct).
    tangoFormatter uses the format as it is. Not for every row: an attribute
    without a format (a boolean) would give it an invalid one.

    A label works out its format string once, from the type of the first value it
    shows. DefaultReadWidgetLabel.setModel() switches to the value's magnitude
    only after the model is set, so the first value of an attribute that other
    widgets already read can arrive as a Quantity: "{:~.2e}", which then fails on
    every float and falls back to "{0}", 0.01 (Ch32, 31 Oct). The format is
    worked out again once the label shows magnitudes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFormat(tangoFormatter)

    def setModel(self, model, **kwargs):
        super().setModel(model, **kwargs)
        self.resetFormat()


class DeviationLed(CenteredLed):
    """A boolean whose True is a deviation (ISA-101): orange when True, plain when
    False, centered like Taurus's own boolean LED (CenteredLed), whose green reads
    as "fine" (Faraday cup zero check, Ch32)."""

    DefaultOnColor = "orange"
    DefaultOffColor = "white"


class OkLed(CenteredLed):
    """A boolean OK flag (door closed, vacuum OK...): plain when True, orange when
    False, the deviation (ISA-101). Inverted: QLed lights the False state, in the
    color the controller picks for False (offColor) (interlock, Ch32)."""

    DefaultLedInverted = True
    DefaultOnColor = "white"
    DefaultOffColor = "orange"


class OperatorForm(TaurusForm):
    """A TaurusForm for the operator screen.

    - Rows start at the top of the panel. TaurusForm.fillWithChildren() (5.4.1)
      puts an expanding spacer above the rows as well as below them, so a few rows
      sit in the middle of a tall panel (Ch32, 31 Oct). After every fill, the top
      spacer is collapsed.
    - No Reset/Apply button box: its Reset (drop a typed, unsent value) sat next to
      a device's Reset command, and Enter in a write widget already sends the value
      (Ch32, 31 Oct).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWithButtons(False)

    def fillWithChildren(self):
        super().fillWithChildren()
        layout = self.scrollArea.widget().layout()
        top = layout.itemAt(0).spacerItem() if layout.count() else None
        if top is None:
            self.warning("no top spacer found; rows stay where Taurus put them")
            return
        top.changeSize(0, 0, Qt.QSizePolicy.Minimum, Qt.QSizePolicy.Fixed)
        layout.invalidate()

    def setReadOnly(self, model):
        """Show model without its write widget: one way to act, the tab's buttons."""
        item = self.getItemByModel(model)
        if item is None:
            self.warning("no row for %s; it stays writable", model)
            return
        item.setAllowWrite(False)
        item.updateWriteWidget()

    def setStatusRow(self, model):
        """Show model, a Status text, in full and on the gray base (StatusLabel)."""
        item = self.getItemByModel(model)
        if item is None:
            self.warning("no row for %s; it keeps Taurus's label", model)
            return
        item.setReadWidgetClass(StatusLabel)

    def setTangoFormat(self, model):
        """Show model's value in its Tango format (TangoFormatLabel)."""
        item = self.getItemByModel(model)
        if item is None:
            self.warning("no row for %s; it keeps Taurus's format", model)
            return
        item.setReadWidgetClass(TangoFormatLabel)

    def setDeviationLed(self, model):
        """Show model, a boolean, as a DeviationLed (True orange, False plain)."""
        item = self.getItemByModel(model)
        if item is None:
            self.warning("no row for %s; it keeps Taurus's green LED", model)
            return
        item.setReadWidgetClass(DeviationLed)

    def setOkLed(self, model):
        """Show model, a boolean OK flag, as an OkLed (True plain, False orange)."""
        item = self.getItemByModel(model)
        if item is None:
            self.warning("no row for %s; it keeps Taurus's green LED", model)
            return
        item.setReadWidgetClass(OkLed)


class OperatorTrend(TaurusTrend):
    """A TaurusTrend for the operator screen.

    - Reads its attributes every second. TaurusTrend plots one point per change
      event; a device that pushes events only while its value changes (the
      magnet's current, Ch24) gives a constant value a single point: no line, and
      an auto-ranged time axis collapsed to microseconds (Ch32 Q1).
    - Draws on the ISA-101 gray base, with pens by curve position and plain legend
      labels ("current (A)", not "current[0]"). Taurus names a curve
      "<attribute>[0]" and finds it by that name, so only the legend's text
      changes. Curves exist once their first value arrives (and are rebuilt when a
      device comes back), so a timer styles each new one as it appears.
    - Holds back the trend sets' repeated "no actual value" warnings (RepeatFilter).
    """

    FORCED_READ_MS = 1000

    def __init__(self, curves=(), log_y=False, parent=None):
        super().__init__(parent)
        # (attribute name, legend label), in pen order.
        self._curves = [(attr.lower(), label) for attr, label in curves]
        self.setForcedReadingPeriod(self.FORCED_READ_MS)
        self.setBackground(BACKGROUND)
        for side in ("left", "bottom"):
            axis = self.getAxis(side)
            axis.setPen(FOREGROUND)
            axis.setTextPen(FOREGROUND)
        self.getAxis("bottom").enableAutoSIPrefix(False)  # no "(x1e+09)"
        # The axis shows the client's local time; beamlinehost runs Etc/UTC.
        self.setLabel("bottom", f"time ({time.strftime('%Z')})")
        if log_y:
            self.getPlotItem().setLogMode(y=True)
        self._repeats = RepeatFilter()
        self._styled = weakref.WeakSet()
        self._filtered = weakref.WeakSet()
        self._styler = Qt.QTimer(self)
        self._styler.timeout.connect(self._style_new_curves)
        self._styler.start(self.FORCED_READ_MS)

    def _style_new_curves(self):
        for trend_set in self.getTrendSets():
            if trend_set not in self._filtered:
                trend_set.getLogObj().addFilter(self._repeats)
                self._filtered.add(trend_set)
        legend = self.getPlotItem().legend
        names = [attr for attr, _ in self._curves]
        for item in self.getPlotItem().listDataItems():
            attr = (item.name() or "").split("[")[0].lower()
            if attr not in names:
                continue
            position = names.index(attr)
            if item not in self._styled:
                item.setPen(pg.mkPen(**CURVE_PENS[min(position, len(CURVE_PENS) - 1)]))
                self._styled.add(item)
            text = self._curves[position][1]
            label = legend.getLabel(item) if legend is not None else None
            if label is not None and label.text != text:
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


class DeviceWatch(Qt.QObject):
    """Follows a device's State through Taurus and reports it in the GUI thread.

    Emits stateChanged with the state's name, or "NO CONTACT" on an error event.
    Connect to stateChanged first, then call start(): the first value comes once,
    possibly from a Taurus worker thread straight away, and with State on Tango
    change events the next one only when the state changes (Ch32, 31 Oct).
    """

    stateChanged = Qt.pyqtSignal(str)

    def __init__(self, device, parent=None):
        super().__init__(parent)
        self._device = device
        self._attr = None

    def start(self):
        if self._attr is None:
            self._attr = taurus.Attribute(f"{self._device}/state")
            self._attr.addListener(self)

    def eventReceived(self, _src, evt_type, evt_value):
        # Called from Taurus's event thread: the signal crosses to the GUI thread.
        if evt_type == TaurusEventType.Error:
            self.stateChanged.emit("NO CONTACT")
        elif (
            evt_type in (TaurusEventType.Change, TaurusEventType.Periodic)
            and evt_value is not None
        ):
            # Taurus gives a DevState enum: its name ("ON"), not str() ("DevState.ON").
            state = evt_value.rvalue
            self.stateChanged.emit(getattr(state, "name", str(state)))

    def stop(self):
        if self._attr is not None:
            self._attr.removeListener(self)
            self._attr = None


class DeviceTab(Qt.QWidget):
    """One device's tab: form and actions on the left, its trend on the right."""

    def __init__(self, spec, parent=None):
        super().__init__(parent)
        device = spec["device"]
        self.form = OperatorForm(self)
        self.form.setModel([f"{device}/{attr}" for attr in spec["form"]])
        for attr in spec.get("read_only", ()):
            self.form.setReadOnly(f"{device}/{attr}")
        for attr in spec["form"]:
            if attr.lower() == "status":
                self.form.setStatusRow(f"{device}/{attr}")
        for attr in spec.get("tango_format", ()):
            self.form.setTangoFormat(f"{device}/{attr}")
        for attr in spec.get("deviation_leds", ()):
            self.form.setDeviationLed(f"{device}/{attr}")
        for attr in spec.get("ok_leds", ()):
            self.form.setOkLed(f"{device}/{attr}")

        self.buttons = []
        actions = Qt.QHBoxLayout()
        for action in spec.get("actions", ()):
            button = OperatorCommandButton(self)
            button.setModel(device)
            button.setCommand(action["command"])
            button.setCustomText(action["text"])
            if action.get("confirm"):
                button.setDangerMessage(action["confirm"])
            # Disabled until the device's State says actions are allowed.
            button.setEnabled(False)
            actions.addWidget(button)
            states = action.get("states", [s.name for s in ACTION_STATES])
            self.buttons.append((button, frozenset(states)))

        left = Qt.QWidget(self)
        column = Qt.QVBoxLayout(left)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.form, 1)
        column.addLayout(actions)

        trend = spec["trend"]
        self.trend = OperatorTrend(
            curves=trend["curves"], log_y=trend.get("log_y", False), parent=self
        )
        self.trend.setModel([f"{device}/{attr}" for attr, _ in trend["curves"]])

        splitter = Qt.QSplitter(Qt.Qt.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(self.trend)
        # Start at 1 : 2 whatever the form's size hint (stretch factors alone only
        # share out the space left over beyond it), and keep that on resizes.
        splitter.setSizes([1000, 2000])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout = Qt.QVBoxLayout(self)
        layout.addWidget(splitter)

    def setActionsForState(self, state):
        """Enable each action whose states include the device's state (a name)."""
        for button, states in self.buttons:
            button.setEnabled(state in states)


class UnreachableTab(Qt.QWidget):
    """What a tab shows while its device does not answer, with a Retry button."""

    retry = Qt.pyqtSignal()

    def __init__(self, device, parent=None):
        super().__init__(parent)
        self._device = device
        self._text = Qt.QLabel(self)
        self._text.setWordWrap(True)
        self._text.setAlignment(Qt.Qt.AlignCenter)
        button = Qt.QPushButton("Retry now", self)
        button.clicked.connect(self.retry)
        layout = Qt.QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self._text)
        layout.addWidget(button, 0, Qt.Qt.AlignCenter)
        layout.addStretch(2)
        self.show_checking()

    def show_checking(self):
        self._text.setText(f"Checking {self._device} ...")

    def show_reason(self, reason):
        plain = PING_REASONS.get(reason, "no answer")
        self._text.setText(
            f"{self._device} does not answer: {plain} ({reason}).\n\n"
            "Its device server is probably not running. This tab appears by itself "
            f"once the device answers (checked every {RETRY_INTERVAL_MS // 1000} s)."
        )


def ping(device):
    """None if device answers a ping, else the Tango reason it failed with."""
    with tango.EnsureOmniThread():
        try:
            proxy = tango.DeviceProxy(device)
            proxy.set_timeout_millis(PING_TIMEOUT_MS)
            proxy.ping()
        except tango.DevFailed as exc:
            return exc.args[0].reason
    return None


class DeviceTabs(Qt.QTabWidget):
    """One tab per device; a device that does not answer never blocks the others.

    Set tabSpecs (from config.py, through the panel's widget_properties). Each tab
    starts as an UnreachableTab ("Checking ...") and pings its device in a worker
    thread; a device that answers gets its DeviceTab, one that does not keeps the
    placeholder, a warning icon, and a ping every RETRY_INTERVAL_MS. Each built tab
    follows its device's State (DeviceWatch): actions are disabled while the device
    is in FAULT or out of contact, and the status bar shows every device's state.
    """

    pinged = Qt.pyqtSignal(int, object)  # tab index, reason (None: it answered)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._specs = []
        self._pending = set()
        self._watches = {}
        self._status = {}
        self.pinged.connect(self._on_pinged)
        self._retry = Qt.QTimer(self)
        self._retry.timeout.connect(self._retry_unreachable)
        self._retry.start(RETRY_INTERVAL_MS)

    def getTabSpecs(self):
        return self._specs

    def setTabSpecs(self, specs):
        self._specs = list(specs)
        for index, spec in enumerate(self._specs):
            placeholder = UnreachableTab(spec["device"], self)
            placeholder.retry.connect(lambda i=index: self._start_ping(i))
            self.addTab(placeholder, spec["title"])
            self._start_ping(index)

    tabSpecs = property(getTabSpecs, setTabSpecs)

    def _start_ping(self, index):
        if index in self._pending:
            return
        self._pending.add(index)
        device = self._specs[index]["device"]
        threading.Thread(
            target=lambda: self.pinged.emit(index, ping(device)),
            name=f"ping-{device}",
            daemon=True,
        ).start()

    def _retry_unreachable(self):
        for index in range(len(self._specs)):
            if isinstance(self.widget(index), UnreachableTab):
                self._start_ping(index)

    def _on_pinged(self, index, reason):
        self._pending.discard(index)
        placeholder = self.widget(index)
        if not isinstance(placeholder, UnreachableTab):
            return
        spec = self._specs[index]
        if reason is not None:
            placeholder.show_reason(reason)
            warning = self.style().standardIcon(Qt.QStyle.SP_MessageBoxWarning)
            self.setTabIcon(index, warning)
            self._show_state(index, "NO CONTACT")
            return
        current = self.currentIndex()
        tab = DeviceTab(spec, self)
        self.removeTab(index)
        self.insertTab(index, tab, spec["title"])
        self.setCurrentIndex(current)
        self.setTabIcon(index, Qt.QIcon())
        placeholder.deleteLater()
        watch = DeviceWatch(spec["device"], self)
        watch.stateChanged.connect(lambda state, i=index: self._on_state(i, state))
        self._watches[index] = watch
        watch.start()
        taurus.info("tab %s: %s answers, tab built", spec["title"], spec["device"])

    def _on_state(self, index, state):
        tab = self.widget(index)
        if isinstance(tab, DeviceTab):
            tab.setActionsForState(state)
        self._show_state(index, state)

    def _show_state(self, index, state):
        """The device's state in the main window's status bar (permanent labels)."""
        window = self.window()
        if not isinstance(window, Qt.QMainWindow):
            return
        if not self._status:
            # All labels at once, in tab order, whichever device reports first.
            for i, spec in enumerate(self._specs):
                label = Qt.QLabel(f"{spec['title']}: ...", self)
                label.setContentsMargins(6, 0, 6, 0)
                window.statusBar().addPermanentWidget(label)
                self._status[i] = label
        label = self._status[index]
        label.setText(f"{self._specs[index]['title']}: {state}")
        label.setStyleSheet(STATE_STYLES.get(state, ""))
