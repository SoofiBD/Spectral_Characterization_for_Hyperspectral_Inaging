"""Main application window."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDockWidget, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
                               QTabWidget, QToolBar)

from devices.monochromator import MonochromatorController, SimulationState
from devices.ocean_hdx import OceanHDXController
from gui.characterization_panel import CharacterizationPanel
from gui.hdx_panel import HDXPanel
from gui.monochromator_panel import MonochromatorPanel

STYLE = """
QWidget { background:#161b22; color:#d0d7de; font-size:13px; }
QMainWindow, QTabWidget::pane { background:#0d1117; }
QGroupBox { border:1px solid #30363d; border-radius:6px; margin-top:14px; padding:8px; font-weight:600; }
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#8b949e; }
QPushButton { background:#21262d; border:1px solid #30363d; border-radius:5px; padding:6px 12px; }
QPushButton:hover { background:#30363d; }
QPushButton:disabled { color:#6e7681; background:#161b22; }
QPushButton#primary { background:#1f6feb; border-color:#1f6feb; color:white; font-weight:600; }
QPushButton#primary:disabled { background:#1c2a44; color:#6e7681; }
QPushButton#danger { background:#8e1519; border-color:#da3633; color:white; }
QPushButton#danger:disabled { background:#2a1517; color:#6e7681; }
QPushButton:checked { background:#238636; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTableWidget {
  background:#0d1117; border:1px solid #30363d; border-radius:4px; padding:3px; }
QPlainTextEdit#console { font-family:Menlo, Consolas, monospace; font-size:12px; }
QHeaderView::section { background:#21262d; padding:4px; border:none; }
QTabBar::tab { background:#161b22; padding:7px 14px; border:1px solid #30363d; border-bottom:none; }
QTabBar::tab:selected { background:#0d1117; color:#58a6ff; }
QLabel#statusOk { color:#3fb950; font-weight:700; }
QLabel#statusBad { color:#f85149; font-weight:700; }
QLabel#statusBusy { color:#d29922; font-weight:700; }
QLabel#bigReadout { font-family:Menlo, Consolas, monospace; font-size:22px; color:#58a6ff; }
QLabel#readout, QLabel#summary { font-family:Menlo, Consolas, monospace; }
QProgressBar { border:1px solid #30363d; border-radius:4px; text-align:center; background:#0d1117; }
QProgressBar::chunk { background:#1f6feb; }
QToolBar { background:#0d1117; border-bottom:1px solid #30363d; spacing:8px; }
QDockWidget::title { background:#21262d; padding:4px; }
"""


class MainWindow(QMainWindow):
    def __init__(self, base_dir: Path, simulate: bool = False, backend: str = "native"):
        super().__init__()
        self.setWindowTitle("Spectral Characterization Suite")
        self.resize(1400, 900)
        self.setStyleSheet(STYLE)
        data_dir, report_dir = base_dir / "data", base_dir / "reports"
        data_dir.mkdir(exist_ok=True)
        report_dir.mkdir(exist_ok=True)

        self.sim_state = SimulationState()
        self.mono = MonochromatorController(self.sim_state)
        self.hdx = OceanHDXController(self.sim_state, backend)

        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.sim_action = QAction("Simulation Mode", self, checkable=True, checked=simulate)
        self.sim_action.toggled.connect(self._sim_toggled)
        tb.addAction(self.sim_action)
        tb.addSeparator()
        self.mono_ind = QLabel("● Monochromator")
        self.hdx_ind = QLabel("● Spectrometer")
        tb.addWidget(self.mono_ind)
        tb.addWidget(self.hdx_ind)

        sim = self.sim_action.isChecked
        self.mono_panel = MonochromatorPanel(self.mono, sim)
        self.hdx_panel = HDXPanel(self.hdx, sim, data_dir)
        self.char_panel = CharacterizationPanel(self.mono, self.hdx, data_dir, report_dir, sim)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.mono_panel, "Monochromator")
        self.tabs.addTab(self.hdx_panel, "Spectrometer (HDX)")
        self.tabs.addTab(self.char_panel, "Characterization")
        self.setCentralWidget(self.tabs)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setObjectName("console")
        self.log.setMaximumBlockCount(2000)
        dock = QDockWidget("Event Log", self)
        dock.setWidget(self.log)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [140], Qt.Vertical)

        for p in (self.mono_panel, self.hdx_panel, self.char_panel):
            p.log_message.connect(self.log_event)
        self.mono_panel.connection_changed.connect(self._update_indicators)
        self.hdx_panel.connection_changed.connect(self._update_indicators)
        self.char_panel.running_changed.connect(self._on_scan_running)

        self._update_indicators()
        self.log_event("Application started" + (" in SIMULATION mode" if simulate else ""))

    def log_event(self, msg: str):
        self.log.appendPlainText(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}")
        self.statusBar().showMessage(msg, 8000)

    def _sim_toggled(self, on: bool):
        if self.mono.is_connected or self.hdx.is_connected:
            self.log_event("Simulation mode change takes effect on next connect")
        self.mono_panel.refresh_ports()
        self.log_event(f"Simulation mode {'ON' if on else 'OFF'}")

    def _update_indicators(self, *_):
        for lbl, dev in ((self.mono_ind, self.mono), (self.hdx_ind, self.hdx)):
            color = "#3fb950" if dev.is_connected else "#f85149"
            if dev.is_connected and dev.is_simulated:
                color = "#d29922"
            lbl.setStyleSheet(f"color:{color}; font-weight:600; background:transparent;")

    def _on_scan_running(self, running: bool):
        self.mono_panel.set_external_busy(running)
        self.hdx_panel.set_external_busy(running)
        self.sim_action.setEnabled(not running)
        if not running:
            self.mono_panel.update_position(self.mono.position_nm)

    def closeEvent(self, event):
        if self.char_panel.worker is not None:
            if QMessageBox.question(self, "Scan running", "A characterization is running. Abort and quit?") \
                    != QMessageBox.Yes:
                event.ignore()
                return
            self.char_panel.worker.abort()
            self.char_panel.worker.wait(10000)
        self.hdx_panel.stop_live(block=True)
        self.mono.disconnect()
        self.hdx.disconnect()
        event.accept()
