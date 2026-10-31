#!/usr/bin/env python3
"""
Beamline operator panel launcher (handbook Part 3, Ch32).

Starts the TaurusGui configured in conf/tgconf_beamline, docks its two panels and
shows the window maximized, at every start:

    +------------------------------------------------------------------+
    | [Magnet] [Vacuum] ...  one tab per device (widgets.DeviceTabs)   |
    |  +-----------------------+-------------------------------------+ |
    |  | form                  | trend                               | |
    |  | actions               |                                     | |
    |  +-----------------------+-------------------------------------+ |
    | [Devices] [Manual]                        status: device states  |
    +------------------------------------------------------------------+

The layout lives here, in code, rather than in an exported Taurus settings file:
that file holds a pickled copy of the whole GUI configuration, with the Tango host
baked into every model name (Ch32 T1). For the same reason the GUI runs on a
throwaway settings file: Taurus's per-user settings would restore the last
session's panel positions and even a trend's old time range (Ch32 U1). Every start
gives the same screen.

The panels are docked before the window is shown, so its size limits are final when
it is mapped. mutter (under Xwayland) drops the maximize request Qt makes before the
window is mapped, and some made after, so StartupMaximizer asks again until the
window really is maximized (Ch32, 30 Oct).

Devices that do not answer no longer stop the start: each tab checks its own device
and says so while it is down (Ch32, 31 Oct; until then one device down meant a
dialog before anything showed).

Usage, on beamlinehost (the operator's client, never tango-ds):
    beamline-gui-env python3 taurus_gui/beamline_gui.py [CONFDIR]
"""

import sys
import tempfile
from pathlib import Path

from taurus.external.qt import Qt
from taurus.qt.qtgui.application import TaurusApplication
from taurus.qt.qtgui.taurusgui import TaurusGui

DEFAULT_CONF = Path(__file__).resolve().parent / "conf" / "tgconf_beamline"

DEVICES, NOTES = "Devices", "Manual"
# How long the window manager gets to maximize the window after each request, and
# how many times to ask again before giving up.
MAXIMIZE_CHECK_MS = 500
MAXIMIZE_RETRIES = 6


def panels(gui):
    """The two panels, in the order DEVICES, NOTES."""
    names = (DEVICES, NOTES)
    missing = [name for name in names if gui.getPanel(name) is None]
    if missing:
        raise SystemExit(f"beamline_gui: no panel(s) {missing} in the configuration")
    return [gui.getPanel(name) for name in names]


def arrange(gui):
    """Dock the panels: the device tabs in front, the manual behind them. Call
    before the window is shown."""
    devices, notes = panels(gui)
    # TaurusGui starts its panels tabbed together in its own order: undock both,
    # then dock them again in ours.
    for panel in (devices, notes):
        gui.removeDockWidget(panel)
    gui.addDockWidget(Qt.Qt.TopDockWidgetArea, devices)
    gui.tabifyDockWidget(devices, notes)
    for panel in (devices, notes):
        panel.show()
    devices.raise_()
    # The Taurus/organization logo bar takes width for no operator purpose.
    logo_bar = gui.findChild(Qt.QToolBar, "jorgsToolBar")
    if logo_bar is not None:
        logo_bar.hide()


class StartupMaximizer(Qt.QObject):
    """Gets the window maximized at start.

    mutter drops the maximize request Qt makes before the window is mapped, and
    some made just after it (Ch32, 30 Oct). Qt's window state can't tell: after a
    dropped request it may still read "maximized". So only the outcome counts, a
    resize of the visible window into the maximized state. Until it comes, every
    MAXIMIZE_CHECK_MS the state is cleared and requested again, up to
    MAXIMIZE_RETRIES times. Then it stops watching, so the operator can still
    un-maximize the window.
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
            Qt.QTimer.singleShot(0, self._finish)
        return False

    def _check(self):
        if self._done:
            self._timer.stop()
            return
        if self._retries == MAXIMIZE_RETRIES:
            self._gui.warning(
                "window not maximized after %d retries; leaving it as it is",
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
        self._gui.info("window at %dx%d", self._gui.width(), self._gui.height())


def main(argv):
    conf = Path(argv[1]) if len(argv) > 1 else DEFAULT_CONF
    # The notes panel is a QtWebEngine view, which needs shared OpenGL contexts
    # declared before the QApplication exists.
    Qt.QCoreApplication.setAttribute(Qt.Qt.AA_ShareOpenGLContexts)
    app = TaurusApplication(cmd_line_parser=None, app_name="taurusgui")
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
