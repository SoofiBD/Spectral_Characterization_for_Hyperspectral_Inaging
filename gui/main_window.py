"""Main application window."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QDockWidget, QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPlainTextEdit, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from devices.monochromator import MonochromatorController, SimulationState
from devices.ocean_hdx import OceanHDXController
from devices.asi_camera import ASICameraClient
from gui.camera_panel import CameraPanel
from gui.characterization_panel import CharacterizationPanel
from gui.hdx_panel import HDXPanel
from gui.monochromator_panel import MonochromatorPanel
from gui.theme import STATE_COLORS, STYLE, apply_plot_defaults, icon
from gui.widgets import set_state

# key, navigation label, icon, page title, one-line purpose
PAGES = [("camera", "Camera", "camera", "Camera",
          "Live view and RAW16 frames. Works without the other devices."),
         ("mono", "Mono", "mono", "Monochromator",
          "Serial connection, position check and manual motion."),
         ("scan", "Scan", "scan", "HSI Scan",
          "Wavelength scan with the camera and the optional HDX; results and exports."),
         ("hdx", "HDX", "hdx", "HDX Spectrometer (optional)",
          "Stationary spectra for testing and comparison.")]

SHORTCUTS = [("Ctrl+1 … Ctrl+4", "Camera / Monochromator / HSI Scan / HDX page"),
             ("F5", "Camera: start / stop live view"),
             ("Ctrl+S", "Camera: save the next raw frame"),
             ("Ctrl+Return", "HSI Scan: start scan"),
             ("Ctrl+O", "Open a recorded run"),
             ("Ctrl+L", "Show / hide the event log"),
             ("F1", "This list")]

# Remembered between sessions. Left out on purpose: the white-reference flag (would mislabel the next
# scan), SET/RESET values (they declare or move the position) and dark subtraction (device state).
PERSIST_SKIP = {"white_chk", "set_spin", "reset_spin", "subtract_chk"}


from gui.workers import _active_tasks


class MainWindow(QMainWindow):
    def __init__(self, base_dir: Path, simulate: bool = False, backend: str = "native",
                 spectrometer_factory=OceanHDXController, camera_factory=ASICameraClient,
                 settings: QSettings | None = None):
        super().__init__()
        self._closing = False
        self.settings = settings  # None (tests): nothing is restored or saved
        self.setWindowTitle("Spectral Characterization Suite")
        self.resize(1440, 900)
        self.setStyleSheet(STYLE)
        self.data_dir, report_dir = base_dir / "data", base_dir / "reports"
        data_dir = self.data_dir
        data_dir.mkdir(exist_ok=True)
        report_dir.mkdir(exist_ok=True)

        self.sim_state = SimulationState()
        self.mono = MonochromatorController(self.sim_state)
        self.hdx = spectrometer_factory(self.sim_state, backend)

        apply_plot_defaults()
        self.sim_action = QAction("Simulation Mode", self, checkable=True, checked=simulate)
        self.sim_action.setIconText("Sim")
        self.sim_action.setToolTip("Simulated devices for practice and testing. Takes effect on the next connect.")
        self.sim_action.toggled.connect(self._sim_toggled)

        sim = self.sim_action.isChecked
        self.camera_panel = CameraPanel(sim, data_dir, base_dir / "logs", camera_factory)
        self.mono_panel = MonochromatorPanel(self.mono, sim)
        self.hdx_panel = HDXPanel(self.hdx, sim, data_dir)
        self.char_panel = CharacterizationPanel(self.mono, self.hdx, data_dir, report_dir, sim)
        self.char_panel.camera_factory = camera_factory
        self.char_panel.camera_panel = self.camera_panel
        # Live camera preview is handed over by the scan itself (it waits for the last exposure).
        self.char_panel.can_start = lambda: not (_active_tasks or self.hdx_panel._live or
                                                self.hdx_panel._stopping or self.mono_panel._busy)
        panels = {"camera": self.camera_panel, "mono": self.mono_panel, "scan": self.char_panel, "hdx": self.hdx_panel}
        self.char_panel.show_tab = lambda name: self.tabs.setCurrentWidget(panels[name])

        self.tabs = QStackedWidget()  # pages, switched by the navigation rail
        for key, *_ in PAGES:
            self.tabs.addWidget(panels[key])
        self._page_state = {key: ("off", "") for key, *_ in PAGES}

        header = QFrame()
        header.setObjectName("pageHeader")
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel()
        self.page_sub.setObjectName("hint")
        self.page_pill = QLabel()
        self.page_pill.setObjectName("pill")
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_sub)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(4, 6, 4, 10)
        hl.addLayout(titles, 1)
        hl.addWidget(self.page_pill, 0, Qt.AlignVCenter)

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(14, 8, 14, 10)
        cl.setSpacing(10)
        cl.addWidget(header)
        cl.addWidget(self.tabs, 1)
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(content, 1)
        self.setCentralWidget(central)
        self.tabs.currentChanged.connect(self._on_page)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setObjectName("console")
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("Device events, saved files and errors appear here.")
        dock = QDockWidget("Event Log", self)
        dock.setObjectName("eventLog")
        dock.setWidget(self.log)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable |
                         QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [120], Qt.Vertical)
        self.log_dock = dock

        self.scan_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.scan_lbl)
        self._build_menus()

        for p in (self.camera_panel, self.mono_panel, self.hdx_panel, self.char_panel):
            p.log_message.connect(self.log_event)
        self.camera_panel.connection_changed.connect(self._update_indicators)
        self.mono_panel.connection_changed.connect(self._update_indicators)
        self.hdx_panel.connection_changed.connect(self._update_indicators)
        self.char_panel.running_changed.connect(self._on_scan_running)
        self.char_panel.progress_text.connect(self.scan_lbl.setText)
        self.char_panel.progress_text.connect(lambda _: self._update_indicators())
        # Bound method (not a lambda): traffic is emitted from worker threads and must queue to the GUI thread.
        self.mono_panel.traffic.connect(self.char_panel.update_readiness)  # refresh the reported position

        self._restore_settings()
        self._on_page(self.tabs.currentIndex())
        self._update_indicators()
        self.log_event("Application started" + (" in SIMULATION mode" if simulate else ""))

    def _build_sidebar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(84)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(4)
        brand = QLabel("HSI")
        brand.setObjectName("brand")
        brand.setAlignment(Qt.AlignCenter)
        lay.addWidget(brand)
        lay.addSpacing(8)
        group = QActionGroup(self)
        self.nav: list[QToolButton] = []
        for i, (key, label, ic, title, sub) in enumerate(PAGES):
            action = QAction(icon(ic), label, self, checkable=True)
            action.setToolTip(f"{title} (Ctrl+{i + 1})")
            action.triggered.connect(lambda _=False, i=i: self.tabs.setCurrentIndex(i))
            group.addAction(action)
            lay.addWidget(self._rail_button(action))
        lay.addStretch(1)
        self._rail = lay  # Log and Help are added once their actions exist (_build_menus)
        self.sim_action.setIcon(icon("sim"))
        sim_btn = self._rail_button(self.sim_action)
        sim_btn.setObjectName("simToggle")
        lay.addWidget(sim_btn)
        return bar

    def _rail_button(self, action: QAction) -> QToolButton:
        b = QToolButton()
        b.setDefaultAction(action)
        b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        b.setIconSize(QSize(22, 22))
        b.setFixedWidth(68)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)  # keyboard: Ctrl+1…4, Ctrl+L, F1
        if action.actionGroup() is not None:  # page buttons (not Sim / Log / Help)
            self.nav.append(b)
        return b

    def _on_page(self, i: int):
        key, label, ic, title, sub = PAGES[i]
        self.nav[i].defaultAction().setChecked(True)
        self.page_title.setText(title)
        self.page_sub.setText(sub)
        self._update_pill()

    def _update_pill(self):
        state, text = self._page_state[PAGES[self.tabs.currentIndex()][0]]
        self.page_pill.setText(text)
        self.page_pill.setVisible(bool(text))
        set_state(self.page_pill, state)

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        open_run = file_menu.addAction("Open Run / Recover…", self._open_run)
        open_run.setShortcut(QKeySequence.Open)
        file_menu.addAction("Open Data Folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))))
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Quit", self.close)
        quit_action.setShortcut(QKeySequence.Quit)

        view = self.menuBar().addMenu("&View")
        for i, page in enumerate(PAGES):
            action = view.addAction(page[3], lambda i=i: self.tabs.setCurrentIndex(i))
            action.setShortcut(f"Ctrl+{i + 1}")
        view.addSeparator()
        log_action = self.log_dock.toggleViewAction()
        log_action.setShortcut("Ctrl+L")
        log_action.setIcon(icon("log"))
        log_action.setIconText("Log")
        view.addAction(log_action)
        self._rail.addWidget(self._rail_button(log_action))
        view.addAction(self.sim_action)

        help_menu = self.menuBar().addMenu("&Help")
        keys = help_menu.addAction("Keyboard Shortcuts", self._show_shortcuts)
        keys.setShortcut(QKeySequence.HelpContents)
        keys.setIcon(icon("help"))
        keys.setIconText("Help")
        keys.setToolTip("Keyboard shortcuts (F1)")
        self._rail.addWidget(self._rail_button(keys))

    def _show_shortcuts(self):
        rows = "".join(f"<tr><td style='padding:2px 16px 2px 0'><b>{k}</b></td><td>{v}</td></tr>" for k, v in SHORTCUTS)
        QMessageBox.information(self, "Keyboard shortcuts", f"<table>{rows}</table>")

    def _open_run(self):
        self.tabs.setCurrentWidget(self.char_panel)
        if self.char_panel.worker is None:
            self.char_panel.load_run()

    def _persisted(self):
        for panel in (self.camera_panel, self.mono_panel, self.char_panel, self.hdx_panel):
            for name, w in vars(panel).items():
                if name not in PERSIST_SKIP and (isinstance(w, (QAbstractSpinBox, QCheckBox)) or
                                                 (isinstance(w, QGroupBox) and w.isCheckable())):
                    yield f"{type(panel).__name__}/{name}", w

    def _restore_settings(self):
        if self.settings is None:
            return
        s = self.settings
        if s.contains("window/geometry"):
            self.restoreGeometry(s.value("window/geometry"))
            self.restoreState(s.value("window/state"))
        for key, w in self._persisted():
            v = s.value(key)
            if v is None:
                continue
            try:
                if isinstance(w, QAbstractSpinBox):
                    w.setValue(type(w.value())(float(v)))
                else:
                    w.setChecked(v in (True, "true", 1, "1"))
            except (TypeError, ValueError, OverflowError):
                pass  # a stale or foreign value keeps the default
        try:
            self.tabs.setCurrentIndex(int(s.value("window/tab", 0)))
        except (TypeError, ValueError):
            pass

    def _save_settings(self):
        if self.settings is None:
            return
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/state", self.saveState())
        s.setValue("window/tab", self.tabs.currentIndex())
        for key, w in self._persisted():
            s.setValue(key, w.value() if isinstance(w, QAbstractSpinBox) else w.isChecked())
        s.sync()

    def log_event(self, msg: str):
        self.log.appendPlainText(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}")
        self.statusBar().showMessage(msg, 8000)

    def _sim_toggled(self, on: bool):
        if self.mono.is_connected or self.hdx.is_connected or self.camera_panel.client is not None:
            self.log_event("Simulation mode change takes effect on next connect")
        self.camera_panel.sync_simulation(on)
        self.mono_panel.refresh_ports()
        self.log_event(f"Simulation mode {'ON' if on else 'OFF'}")
        self.char_panel.update_readiness()

    def _update_indicators(self, *_):
        self.char_panel.int_spin.setRange(self.hdx.MIN_INT_MS, self.hdx.MAX_INT_MS)
        client = self.camera_panel.client
        devices = {"camera": (None if client is None else client.simulated,
                              self.camera_panel.info_lbl.text().splitlines()[0] if client else ""),
                   "mono": (self.mono.is_simulated if self.mono.is_connected else None,
                            self.mono.port if self.mono.is_connected else ""),
                   "hdx": (self.hdx.is_simulated if self.hdx.is_connected else None,
                           self.hdx.device_info if self.hdx.is_connected else "")}
        for i, (key, label, ic, title, sub) in enumerate(PAGES):
            if key == "scan":
                running = self.char_panel.worker is not None
                state, text, tip = ("busy", self.scan_lbl.text() or "Scanning…", "Scan running") if running \
                    else ("off", "", "No scan running")
                dot = STATE_COLORS["busy"] if running else None
            else:
                simulated, info = devices[key]
                state = "off" if simulated is None else "sim" if simulated else "ok"
                text = {"off": "Not connected", "sim": "Simulated", "ok": "Connected"}[state]
                if key == "hdx" and state == "off":
                    text = "Optional · not connected"
                tip = f"{text}{': ' + info if info else ''}"
                dot = STATE_COLORS[state]
            self._page_state[key] = (state, text)
            action = self.nav[i].defaultAction()
            action.setIcon(icon(ic, dot))
            action.setToolTip(f"{title} (Ctrl+{i + 1})\n{tip}")
        self._update_pill()
        self.char_panel.update_readiness()

    def _on_scan_running(self, running: bool):
        self._update_indicators()
        self.mono_panel.set_external_busy(running)
        self.hdx_panel.set_external_busy(running)
        if not running:  # on start the scan has already taken the camera (begin_scan_handover)
            self.camera_panel.set_external_busy(False)
        self.sim_action.setEnabled(not running)
        if not running:
            self.mono_panel.update_position(self.mono.position_nm)

    def closeEvent(self, event):
        busy = (self.char_panel.worker is not None or _active_tasks or self.camera_panel.busy or
                self.hdx_panel._live is not None or self.hdx_panel._stopping)
        if busy:
            event.ignore()
            if not self._closing:
                if self.char_panel.worker is not None and QMessageBox.question(
                        self, "Scan running", "Abort and close after active device I/O finishes? "
                        "This does not physically stop the motor.") != QMessageBox.Yes:
                    return
                self._closing = True
                if self.char_panel.worker is not None:
                    self.char_panel.worker.abort()
                self.hdx_panel.stop_live()
                self.camera_panel.stop_live(abort=True)
                self.setEnabled(False)
            QTimer.singleShot(100, self.close)
            return
        self._save_settings()
        self.camera_panel.shutdown()
        self.mono.disconnect()
        self.hdx.disconnect()
        event.accept()
