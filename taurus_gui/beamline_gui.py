#!/usr/bin/env python3
"""
Beamline operator panel launcher (handbook Part 3, Ch32).

Starts the TaurusGui configured in conf/tgconf_beamline, lays its panels out
into the design brief's zones and shows the window maximized, at every start:

    +----------------------+------------------------------------+
    | Magnet               | Magnet Trend  (Manual: tab behind)  |
    | (state, status,      |                                    |
    |  current, setpoint)  |                                    |
    +----------------------+                                    |
    | Magnet Actions       |                                    |
    +----------------------+------------------------------------+

The layout lives here, in code, rather than in an exported Taurus settings
file: that file holds a pickled copy of the whole GUI configuration, with the
Tango host baked into every model name (Ch32 T1). For the same reason the
GUI runs on a throwaway settings file: Taurus's per-user settings would
restore the last session's panel positions and even a trend's old time range
(Ch32 U1). Every start gives the same screen.

The panels are docked into their zones before the window is shown, so its
size limits are final when it is mapped. mutter (under Xwayland) drops the
maximize request Qt makes before the window is mapped, and some made after,
so StartupMaximizer asks again until the window really is maximized; only the
zones' pixel sizes wait for that (Ch32, 30 Oct).

Before the GUI is built, every Tango device its panels name must answer a
ping. A device that is down when TaurusGui builds its panels leaves an empty
form that never recovers, after a modal exception dialog that blocks the
start (a Taurus 5.4.1 defect in TaurusValue.updateReadWidget, Ch32, 30 Oct).
So the operator gets a dialog that names the missing devices, with Retry.

Usage, on beamlinehost (the operator's client, never tango-ds):
    beamline-gui-env python3 taurus_gui/beamline_gui.py [CONFDIR]
"""

import importlib.util
import inspect
import sys
import tempfile
from pathlib import Path

import tango
import taurus
from taurus.core.tango.tangovalidator import (
    TangoAttributeNameValidator,
    TangoDeviceNameValidator,
)
from taurus.external.qt import Qt
from taurus.qt.qtgui.application import TaurusApplication
from taurus.qt.qtgui.taurusgui import TaurusGui
from taurus.qt.qtgui.taurusgui.utils import PanelDescription

DEFAULT_CONF = Path(__file__).resolve().parent / "conf" / "tgconf_beamline"

FORM, ACTIONS, TREND, NOTES = "Magnet", "Magnet Actions", "Magnet Trend", "Manual"
# Pixel sizes on a 1920x1080 screen; Qt shares out the real window in proportion.
LEFT_WIDTH, RIGHT_WIDTH = 640, 1280
FORM_HEIGHT, ACTIONS_HEIGHT = 880, 70
# How long the window manager gets to maximize the window after each request,
# and how many times to ask again before the zones are sized anyway.
MAXIMIZE_CHECK_MS = 500
MAXIMIZE_RETRIES = 6
# How long one device gets to answer the startup ping.
PING_TIMEOUT_MS = 2000
# Plain words for the Tango reasons a ping fails with; the code stays alongside
# for whoever gets called.
PING_REASONS = {
    "API_CantConnectToDevice": "no connection",
    "API_DeviceNotExported": "not running",
    "API_DeviceNotDefined": "not in the Tango database",
    "API_DeviceTimedOut": "no answer in time",
}


def load_config(conf):
    """Import the configuration package by path, as TaurusGui does."""
    init = conf / "__init__.py"
    if not init.is_file():
        raise SystemExit(f"beamline_gui: {conf} is not a configuration package")
    spec = importlib.util.spec_from_file_location(conf.name, init)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its relative imports need the package
    spec.loader.exec_module(module)
    return module


def required_devices(conf_module):
    """The Tango devices behind the configuration's panels, sorted."""
    attr_names, dev_names = TangoAttributeNameValidator(), TangoDeviceNameValidator()
    devices = set()
    for _, panel in inspect.getmembers(conf_module):
        if not isinstance(panel, PanelDescription):
            continue
        models = panel.model
        if isinstance(models, str) or models is None:
            models = (models or "").split()
        for model in models:
            groups = attr_names.getUriGroups(model) or dev_names.getUriGroups(model)
            if groups is None:
                raise SystemExit(f"beamline_gui: {model!r} is not a Tango name")
            devices.add(groups["devname"])
    return sorted(devices)


def unreachable(devices):
    """(device, reason) for every device that doesn't answer a ping."""
    down = []
    for name in devices:
        try:
            proxy = tango.DeviceProxy(name)
            proxy.set_timeout_millis(PING_TIMEOUT_MS)
            proxy.ping()
        except tango.DevFailed as exc:
            down.append((name, exc.args[0].reason))
    return down


def wait_for_devices(devices, title):
    """Return once every device answers; exit if the operator closes instead."""
    while down := unreachable(devices):
        for name, reason in down:
            taurus.warning("startup check: %s does not answer (%s)", name, reason)
        lines = "\n".join(
            f"    {name}: {PING_REASONS.get(reason, 'no answer')} ({reason})"
            for name, reason in down
        )
        if len(down) == 1:
            head, tail = "this device doesn't", "Its device server is"
            action = "Start it"
        else:
            head, tail = "these devices don't", "Their device servers are"
            action = "Start them"
        box = Qt.QMessageBox(
            Qt.QMessageBox.Warning,
            title,
            f"The operator panel can't start: {head} answer.\n\n{lines}\n\n"
            f"{tail} probably not running. {action}, then press Retry.",
            Qt.QMessageBox.Retry | Qt.QMessageBox.Close,
        )
        box.setDefaultButton(Qt.QMessageBox.Retry)
        if box.exec_() != Qt.QMessageBox.Retry:
            taurus.warning("startup check: closed by the operator")
            raise SystemExit(1)
    taurus.info("startup check: %s answer", ", ".join(devices))


def panels(gui):
    """The four panels of the brief, in the order FORM, ACTIONS, TREND, NOTES."""
    names = (FORM, ACTIONS, TREND, NOTES)
    missing = [name for name in names if gui.getPanel(name) is None]
    if missing:
        raise SystemExit(f"beamline_gui: no panel(s) {missing} in the configuration")
    return [gui.getPanel(name) for name in names]


def arrange(gui):
    """Dock the panels into the brief's zones. Call before the window is shown."""
    form, actions, trend, notes = panels(gui)
    # TaurusGui starts its panels tabbed together, and Qt won't split tabbed
    # docks (splitDockWidget would add a tab instead): undock them all first.
    for panel in (form, actions, trend, notes):
        gui.removeDockWidget(panel)
    gui.addDockWidget(Qt.Qt.TopDockWidgetArea, form)
    gui.splitDockWidget(form, trend, Qt.Qt.Horizontal)
    gui.splitDockWidget(form, actions, Qt.Qt.Vertical)
    gui.tabifyDockWidget(trend, notes)
    for panel in (form, actions, trend, notes):
        panel.show()
    trend.raise_()
    # The Taurus/organization logo bar takes width for no operator purpose.
    logo_bar = gui.findChild(Qt.QToolBar, "jorgsToolBar")
    if logo_bar is not None:
        logo_bar.hide()


def size_zones(gui):
    """Share the window's real size out between the zones."""
    form, actions, trend, _ = panels(gui)
    gui.resizeDocks([form, trend], [LEFT_WIDTH, RIGHT_WIDTH], Qt.Qt.Horizontal)
    gui.resizeDocks([form, actions], [FORM_HEIGHT, ACTIONS_HEIGHT], Qt.Qt.Vertical)
    gui.info("zones sized for a %dx%d window", gui.width(), gui.height())


class StartupMaximizer(Qt.QObject):
    """Gets the window maximized at start, then sizes the zones once.

    mutter drops the maximize request Qt makes before the window is mapped,
    and some made just after it (Ch32, 30 Oct). Qt's window state can't tell:
    after a dropped request it may still read "maximized". So only the outcome
    counts, a resize of the visible window into the maximized state. Until it
    comes, every MAXIMIZE_CHECK_MS the state is cleared and requested again, up
    to MAXIMIZE_RETRIES times; then the zones are sized anyway, with a warning.
    Then it stops watching, so the operator can still un-maximize the window.
    """

    def __init__(self, gui):
        super().__init__(gui)
        self._gui = gui
        self._retries = 0
        self._done = False
        self._timer = Qt.QTimer(self)
        self._timer.setInterval(MAXIMIZE_CHECK_MS)
        self._timer.timeout.connect(self._check)
        gui.installEventFilter(self)

    def start(self):
        """Start checking. Call after gui.showMaximized() has returned: calling
        it again from inside show() would re-enter Qt mid-show."""
        if not self._done:
            self._timer.start()

    def eventFilter(self, obj, event):
        if event.type() == Qt.QEvent.Resize and obj.isVisible() and obj.isMaximized():
            # The filter runs before the window lays itself out at the new size.
            Qt.QTimer.singleShot(0, self._finish)
        return False

    def _check(self):
        if self._done:
            self._timer.stop()
            return
        if self._retries == MAXIMIZE_RETRIES:
            self._gui.warning(
                "window not maximized after %d retries; sizing the zones anyway",
                MAXIMIZE_RETRIES,
            )
            self._finish()
            return
        self._retries += 1
        self._gui.info(
            "window not maximized yet; asking again (%d/%d)",
            self._retries,
            MAXIMIZE_RETRIES,
        )
        self._gui.showNormal()
        self._gui.showMaximized()

    def _finish(self):
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self._gui.removeEventFilter(self)
        size_zones(self._gui)


def main(argv):
    conf = Path(argv[1]) if len(argv) > 1 else DEFAULT_CONF
    # The notes panel is a QtWebEngine view, which needs shared OpenGL contexts
    # declared before the QApplication exists.
    Qt.QCoreApplication.setAttribute(Qt.Qt.AA_ShareOpenGLContexts)
    app = TaurusApplication(cmd_line_parser=None, app_name="taurusgui")
    conf_module = load_config(conf)
    wait_for_devices(required_devices(conf_module), conf_module.GUI_NAME)
    with tempfile.TemporaryDirectory(prefix="beamline-gui-") as tmp:
        settings = str(Path(tmp) / "session.ini")
        gui = TaurusGui(None, confname=str(conf), settingsname=settings)
        arrange(gui)
        maximizer = StartupMaximizer(gui)
        gui.showMaximized()
        maximizer.start()
        return app.exec_()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
