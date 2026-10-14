"""Automated characterization workflow: scan, analyze, plot, export, report."""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
import pyqtgraph.exporters
from PySide6.QtCore import QThread, QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QLineEdit, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from acquisition.coordinator import ScanCoordinator, State, scan_plan
from analysis.cube_export import export_envi
from analysis.white_export import export_white_spectra
from analysis.session_analysis import analyze_raw, camera_row, load_results
from analysis.report_generator import generate_report
from analysis.spectral_analysis import (CSV_COLUMNS, analyze_spectrum, peak_repeatability,
                                        relative_response, summarize)
from devices.asi_camera import ASICameraClient, CameraError
from devices.monochromator import MonochromatorController
from devices.ocean_hdx import OceanHDXController
from gui.widgets import IntSpinBox, NumberSpinBox, commit_spinboxes

PROFILE_EVERY_NM = 50  # keep the averaged spectrum of every set wavelength that is a multiple of this

PLOT_DEFS = [
    ("accuracy", "Wavelength Accuracy", "Set wavelength (nm)", "Measured wavelength (nm)"),
    ("error", "Wavelength Error", "Wavelength (nm)", "Error (nm)"),
    ("fwhm", "Spectral Resolution (FWHM)", "Wavelength (nm)", "FWHM (nm)"),
    ("profiles", "Normalized Spectral Profiles", "Wavelength (nm)", "Normalized intensity"),
    ("profiles_counts", "Spectral Profiles (counts)", "Wavelength (nm)", "Intensity (counts, baseline removed)"),
    ("response", "Relative Spectral Response", "Wavelength (nm)", "Relative intensity"),
    ("fwd_bwd", "Forward / Backward Scan Comparison", "Set wavelength (nm)", "Measured − set (nm)"),
]


# ---------------------------------------------------------------------- worker
class CharacterizationWorker(QThread):
    point_done = Signal(dict, object, object)   # result row, wavelength array (None: camera only), HDX average
    status = Signal(str)
    progress = Signal(int, int)
    failed = Signal(str)
    ask_user = Signal(str)   # GUI shows the message, then calls answer(ok)

    def __init__(self, mono: MonochromatorController, hdx: OceanHDXController | None,
                 params: dict, run_dir: Path, parent=None, camera_factory=ASICameraClient,
                 camera_handover=None):
        super().__init__(parent)
        self.mono, self.hdx, self.p = mono, hdx, params  # hdx None: camera-only scan
        self.run_dir = run_dir
        self.camera_factory = camera_factory
        self.camera_handover = camera_handover  # Camera tab client, lent after its preview has stopped
        self.coordinator: ScanCoordinator | None = None
        self._abort = False
        self._answer = threading.Event()
        self._answer_ok = False

    def abort(self):
        self._abort = True
        self._answer_ok = False
        self._answer.set()
        if self.coordinator is not None:
            self.coordinator.abort()

    def answer(self, ok: bool):
        self._answer_ok = ok
        self._answer.set()

    def _prompt(self, text: str) -> bool:
        """Block the worker until the operator confirms a manual step."""
        self._answer.clear()
        if self._abort:
            return False
        self.ask_user.emit(text)
        self._answer.wait()
        return self._answer_ok and not self._abort

    def _on_point(self, point: dict, wl: np.ndarray, raw_scans: list):
        """Analysis of one completed (already saved) point; raw data is never changed here."""
        res, avg = {}, None
        if wl is not None:
            res, avg = analyze_raw(wl, raw_scans, point["target_nm"], point["direction"],
                                   point["reported_nm"], self.hdx.dark if self.hdx.dark_active else None,
                                   self.hdx.max_intensity)
        else:  # camera only: no spectral metrics exist
            res = {"set_wavelength": point["target_nm"], "direction": point["direction"],
                   "reported_position": point["reported_nm"]}
        if "camera" in point:
            res.update(camera_row(self.run_dir, point))
        self.point_done.emit(res, wl, avg)
        self.progress.emit(point["index"] + 1, self._total)

    def run(self):
        p = self.p
        self._total = len(scan_plan(p["start"], p["stop"], p["step"], p["backward"]))
        camera, own_camera = None, False
        hdx = self.hdx
        previous_subtract = hdx.subtract_dark if hdx is not None else None  # restored below
        try:
            modes = {self.mono.is_simulated}
            if hdx is not None:
                modes.add(hdx.is_simulated)
            if p.get("camera"):
                modes.add(p["camera"]["simulate"])
            if len(modes) != 1:
                raise ValueError("Mixed real/simulated devices are not allowed; reconnect devices in one mode")
            if p.get("camera"):
                if self.camera_handover is not None:  # waits until the live preview exposure has ended
                    camera = self.camera_handover.acquire(lambda: self._abort)
                if self._abort:
                    return
                if camera is None:  # Camera tab not connected: this scan owns its own camera process
                    camera = self.camera_factory(simulate=p["camera"]["simulate"], log_path=self.run_dir / "camera.log",
                                                 index=p["camera"].get("index", 0))
                    own_camera = True
                elif camera.simulated != p["camera"]["simulate"]:
                    raise ValueError("Connected camera mode does not match the scan mode")
            if hdx is not None:
                hdx.subtract_dark = True
            self.coordinator = ScanCoordinator(
                self.mono, self.hdx, p, self.run_dir, camera, prompt=self._prompt,
                on_state=lambda state, detail: self.status.emit(
                    f"{state.value}: {detail}" if detail else state.value),
                on_point=self._on_point)
            if self._abort:  # aborted while the camera process was starting
                return
            self.coordinator.run()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if hdx is not None:
                hdx.subtract_dark = previous_subtract
            if camera is not None and own_camera:  # a lent camera goes back to the Camera tab
                camera.close()


# ---------------------------------------------------------------------- plotting
def draw_plots(plots: dict[str, pg.PlotWidget], df: pd.DataFrame, profiles: dict, light=False):
    """Populate the six result plots. Used for both the GUI and the PDF export."""
    fg = "#222222" if light else "#d0d7de"
    c1, c2, c3 = ("#1565c0", "#c62828", "#2e7d32") if light else ("#4fc3f7", "#ff8a65", "#81c784")
    for key, title, xl, yl in PLOT_DEFS:
        pw = plots[key]
        pw.clear()
        pw.setTitle(title, color=fg, size="11pt")
        pw.setLabel("bottom", xl, color=fg)
        pw.setLabel("left", yl, color=fg)
        pw.showGrid(x=True, y=True, alpha=0.25)
        for ax in ("left", "bottom"):
            pw.getAxis(ax).setPen(fg)
            pw.getAxis(ax).setTextPen(fg)
            pw.getAxis(ax).enableAutoSIPrefix(False)
        if pw.plotItem.legend is None:
            pw.addLegend(offset=(10, 10), labelTextColor=fg)
        else:
            pw.plotItem.legend.clear()
    if df is None or df.empty or "measured_peak" not in df:  # spectral plots need HDX data
        return
    fwd = df[df["direction"] == "forward"].sort_values("set_wavelength")
    bwd = df[df["direction"] == "backward"].sort_values("set_wavelength")
    sym = dict(symbol="o", symbolSize=6)

    x = fwd["set_wavelength"].to_numpy()
    if len(x):
        lo, hi = x.min(), x.max()
        plots["accuracy"].plot([lo, hi], [lo, hi], pen=pg.mkPen(fg, style=Qt.DashLine), name="Ideal")
        plots["accuracy"].plot(x, fwd["measured_peak"].to_numpy(), pen=pg.mkPen(c1, width=2),
                               symbolBrush=c1, name="Measured", **sym)

        err = fwd["error_nm"].to_numpy()
        std = np.nan_to_num(fwd["peak_std_nm"].to_numpy()) if "peak_std_nm" in fwd else np.zeros_like(err)
        plots["error"].addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(fg, style=Qt.DashLine)))
        plots["error"].addItem(pg.ErrorBarItem(x=x, y=err, height=2 * std, pen=pg.mkPen(c1)))
        plots["error"].plot(x, err, pen=pg.mkPen(c1, width=2), symbolBrush=c1, name="Error", **sym)

        plots["fwhm"].plot(x, fwd["fwhm_nm"].to_numpy(), pen=pg.mkPen(c3, width=2),
                           symbolBrush=c3, name="FWHM", **sym)

        rr = relative_response(df)
        plots["response"].plot(rr["set_wavelength"].to_numpy(), rr["relative"].to_numpy(),
                               pen=pg.mkPen(c1, width=2), symbolBrush=c1, name="Integrated", **sym)
        rp = relative_response(df, "peak_intensity")
        plots["response"].plot(rp["set_wavelength"].to_numpy(), rp["relative"].to_numpy(),
                               pen=pg.mkPen(c2, width=2, style=Qt.DashLine), name="Peak", **sym)

        plots["fwd_bwd"].addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(fg, style=Qt.DashLine)))
        plots["fwd_bwd"].plot(x, err, pen=pg.mkPen(c1, width=2), symbolBrush=c1, name="Forward", **sym)
    if len(bwd):
        plots["fwd_bwd"].plot(bwd["set_wavelength"].to_numpy(), bwd["error_nm"].to_numpy(),
                              pen=pg.mkPen(c2, width=2), symbolBrush=c2, symbol="t",
                              symbolSize=7, name="Backward")

    for i, w in enumerate(sorted(profiles)):
        wl, inten = profiles[w]
        y = inten - np.median(np.sort(inten)[: inten.size // 3])
        if y.max() <= 0:
            continue
        mask = np.abs(wl - w) < 25
        color = pg.intColor(i, hues=len(profiles), maxValue=200 if light else 255)
        plots["profiles"].plot(wl[mask], (y / y.max())[mask], pen=pg.mkPen(color, width=2), name=f"{w:g} nm")
        plots["profiles_counts"].plot(wl[mask], y[mask], pen=pg.mkPen(color, width=2), name=f"{w:g} nm")


def export_plots(df: pd.DataFrame, profiles: dict, out_dir: Path, stamp: str) -> list[tuple[str, str]]:
    """Render plots into off-screen, white-background PlotWidgets and export PNGs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    plots = {}
    for key, *_ in PLOT_DEFS:
        pw = pg.PlotWidget(background="w")
        pw.setAttribute(Qt.WA_DontShowOnScreen, True)
        pw.resize(1200, 700)
        pw.show()  # layout only resolves on a shown widget; WA_DontShowOnScreen keeps it invisible
        plots[key] = pw
    draw_plots(plots, df, profiles, light=True)
    QApplication.processEvents()
    images = []
    for key, title, *_ in PLOT_DEFS:
        path = out_dir / f"{stamp}_{key}.png"
        exp = pg.exporters.ImageExporter(plots[key].plotItem)
        exp.export(str(path))
        images.append((title, str(path)))
    for pw in plots.values():
        pw.deleteLater()
    return images


# ---------------------------------------------------------------------- panel
class CharacterizationPanel(QWidget):
    log_message = Signal(str)
    running_changed = Signal(bool)

    TABLE_COLS = CSV_COLUMNS + ["peak_std_nm", "direction", "saturated_pixels",
                                "camera_mean_counts", "camera_saturated_pixels"]

    def __init__(self, mono: MonochromatorController, hdx: OceanHDXController,
                 data_dir: Path, report_dir: Path, simulation_enabled, parent=None):
        super().__init__(parent)
        self.mono, self.hdx = mono, hdx
        self.data_dir, self.report_dir = data_dir, report_dir
        self.simulation_enabled = simulation_enabled
        self.worker: CharacterizationWorker | None = None
        self.rows: list[dict] = []
        self.profiles: dict[float, tuple[np.ndarray, np.ndarray]] = {}
        self.params: dict = {}
        self.last_csv: Path | None = None
        self.run_dir: Path | None = None
        self.camera_factory = ASICameraClient
        self.camera_panel = None  # Camera tab: settings source and owner of the connected camera
        self.can_start = lambda: True
        self.recorded_devices = {}
        self._build_ui()

    def _build_ui(self):
        pg_box = QGroupBox("Scan parameters")
        self.start_spin = NumberSpinBox(decimals=1, minimum=200, maximum=1100, value=400, suffix=" nm")
        self.stop_spin = NumberSpinBox(decimals=1, minimum=200, maximum=1100, value=800, suffix=" nm")
        self.step_spin = NumberSpinBox(decimals=2, minimum=0.1, maximum=200, value=10, suffix=" nm")
        self.avg_spin = IntSpinBox(minimum=1, maximum=1000, value=5)
        self.int_spin = NumberSpinBox(decimals=3, minimum=OceanHDXController.MIN_INT_MS, maximum=OceanHDXController.MAX_INT_MS, value=100, suffix=" ms")
        self.settle_spin = NumberSpinBox(decimals=2, minimum=0, maximum=60, value=0.2, suffix=" s")
        self.entrance_slit_spin = NumberSpinBox(decimals=1, minimum=0, maximum=5000, value=0, suffix=" µm")
        self.exit_slit_spin = NumberSpinBox(decimals=1, minimum=0, maximum=5000, value=0, suffix=" µm")
        self.bwd_chk = QCheckBox("Include backward scan", checked=True)
        self.dark_chk = QCheckBox("Measure background (dark) first", checked=True)
        self.dark_avg_spin = IntSpinBox(minimum=1, maximum=500, value=20)
        f = QFormLayout(pg_box)
        f.addRow("Start:", self.start_spin)
        f.addRow("Stop:", self.stop_spin)
        f.addRow("Step:", self.step_spin)
        f.addRow("Settling time:", self.settle_spin)
        f.addRow("Entrance slit (0 = unknown):", self.entrance_slit_spin)
        f.addRow("Exit slit (0 = unknown):", self.exit_slit_spin)
        f.addRow(self.bwd_chk)
        f.addRow(self.dark_chk)
        self.white_chk = QCheckBox("This scan measures a white reference")
        f.addRow(self.white_chk)
        self.plan_lbl = QLabel()
        self.plan_lbl.setWordWrap(True)
        f.addRow(self.plan_lbl)

        # Camera: the primary channel. Its settings and connection live on the Camera tab.
        cam_box = QGroupBox("Camera — primary (settings: Camera tab)")
        cam_box.setCheckable(True)
        cam_box.setChecked(True)
        self.cam_box = cam_box
        cf = QFormLayout(cam_box)
        self.white_path = QLineEdit()
        self.white_path.setPlaceholderText("Optional white-reference run for ENVI export")
        white_select = QPushButton("Choose white reference…")
        white_select.clicked.connect(self._choose_white)
        self.temp_tolerance = NumberSpinBox(minimum=0, maximum=100, value=2, decimals=1, suffix=" °C")
        cf.addRow("Reference ΔT limit:", self.temp_tolerance)

        # HDX: optional comparison / test channel
        hdx_box = QGroupBox("HDX spectrometer (optional)")
        hdx_box.setCheckable(True)
        hdx_box.setChecked(False)
        self.hdx_box = hdx_box
        hf = QFormLayout(hdx_box)
        hf.addRow("Averages:", self.avg_spin)
        hf.addRow("Integration:", self.int_spin)
        hf.addRow("Dark averages:", self.dark_avg_spin)
        self.dark_chk.toggled.connect(lambda on: self.dark_avg_spin.setEnabled(on))
        for w in (self.start_spin, self.stop_spin, self.step_spin, self.avg_spin, self.dark_avg_spin):
            w.valueChanged.connect(self._update_plan)
        for w in (self.bwd_chk, self.dark_chk, self.cam_box, self.hdx_box):
            w.toggled.connect(self._update_plan)

        run_box = QGroupBox("Run")
        self.start_btn = QPushButton("▶  Start Scan")
        self.start_btn.setObjectName("primary")
        self.abort_btn = QPushButton("■  Abort")
        self.abort_btn.setObjectName("danger")
        self.abort_btn.setEnabled(False)
        self.progress = QProgressBar()
        self.state_lbl = QLabel("Idle")
        self.state_lbl.setWordWrap(True)
        rl = QVBoxLayout(run_box)
        for w in (self.start_btn, self.abort_btn, self.progress, self.state_lbl):
            rl.addWidget(w)

        out_box = QGroupBox("Output")
        self.csv_btn = QPushButton("Export CSV…")
        self.load_btn = QPushButton("Load CSV…")
        self.load_run_btn = QPushButton("Open run / recover…")
        self.pdf_btn = QPushButton("Generate PDF Report")
        self.white_spectra_btn = QPushButton("Export white-normalized spectra")
        self.envi_btn = QPushButton("Export ENVI Cube")
        self.csv_btn.setEnabled(False)
        self.pdf_btn.setEnabled(False)
        self.white_spectra_btn.setEnabled(False)
        self.envi_btn.setEnabled(False)
        self.folder_lbl = QLabel()
        self.folder_lbl.setWordWrap(True)
        self.folder_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.folder_btn = QPushButton("Open Saved Folder")
        ol = QVBoxLayout(out_box)
        ol.addWidget(self.folder_lbl)
        ol.addWidget(self.folder_btn)
        ol.addWidget(self.white_path)
        ol.addWidget(white_select)
        for w in (self.csv_btn, self.load_btn, self.load_run_btn, self.pdf_btn, self.envi_btn, self.white_spectra_btn):
            ol.addWidget(w)

        summ_box = QGroupBox("Summary")
        self.summary_lbl = QLabel("—")
        self.summary_lbl.setObjectName("summary")
        self.summary_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        QVBoxLayout(summ_box).addWidget(self.summary_lbl)

        side = QVBoxLayout()
        for b in (pg_box, cam_box, hdx_box, run_box, out_box, summ_box):
            side.addWidget(b)
        side.addStretch(1)
        inner = QWidget()
        inner.setLayout(side)
        inner.setFixedWidth(320)
        sw = QScrollArea()  # the parameter column no longer fits short screens
        sw.setWidget(inner)
        sw.setWidgetResizable(True)
        sw.setFrameShape(QScrollArea.NoFrame)
        sw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sw.setFixedWidth(320 + sw.verticalScrollBar().sizeHint().width() + 2)

        # Live spectrum + result tabs
        self.live_plot = pg.PlotWidget(title="Current Spectrum")
        # Plain labels: with units= pyqtgraph auto-scales to SI prefixes ("0.8 knm", "kcounts")
        self.live_plot.setLabel("bottom", "Wavelength (nm)")
        self.live_plot.setLabel("left", "Intensity (counts)")
        for ax in ("bottom", "left"):
            self.live_plot.getAxis(ax).enableAutoSIPrefix(False)
        self.live_plot.showGrid(x=True, y=True, alpha=0.25)
        self.live_curve = self.live_plot.plot(pen=pg.mkPen("#4fc3f7", width=1.5))
        self.live_set = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ffb74d", style=Qt.DashLine))
        self.live_plot.addItem(self.live_set)

        self.tabs = QTabWidget()
        self.plots: dict[str, pg.PlotWidget] = {}
        for key, title, *_ in PLOT_DEFS:
            pw = pg.PlotWidget()
            self.plots[key] = pw
            self.tabs.addTab(pw, title.split(" (")[0])
        self.table = QTableWidget(0, len(self.TABLE_COLS))
        self.table.setHorizontalHeaderLabels(self.TABLE_COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tabs.addTab(self.table, "Data Table")
        draw_plots(self.plots, pd.DataFrame(), {})

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.live_plot)
        split.addWidget(self.tabs)
        split.setSizes([250, 550])

        root = QHBoxLayout(self)
        root.addWidget(sw)
        root.addWidget(split, 1)

        self.start_btn.clicked.connect(self.start)
        self.abort_btn.clicked.connect(self.abort)
        self.csv_btn.clicked.connect(self.export_csv_dialog)
        self.load_btn.clicked.connect(self.load_csv)
        self.load_run_btn.clicked.connect(self.load_run)
        self.pdf_btn.clicked.connect(self.generate_pdf)
        self.envi_btn.clicked.connect(self.export_cube)
        self.white_spectra_btn.clicked.connect(self.export_white_spectra)
        self.folder_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_dir or self.data_dir))))
        self._update_plan()
        self._update_folder()

    def scan_summary(self) -> tuple[int, int, float]:
        """Positions, HDX sample exposures (0 without HDX) and first-move target (mirrors ScanCoordinator)."""
        step = self.step_spin.value()
        plan = scan_plan(self.start_spin.value(), self.stop_spin.value(), step, self.bwd_chk.isChecked())
        hdx = len(plan) * self.avg_spin.value() if self.hdx_box.isChecked() else 0
        return len(plan), hdx, max(plan[0][1] - 2 * step, 200.0)

    def _update_plan(self, *_):
        try:
            n, exposures, pre = self.scan_summary()
        except ValueError:
            self.plan_lbl.setText("Plan: invalid range (stop must be ≥ start)")
            return
        parts = []
        if self.cam_box.isChecked():
            parts.append(f"{n} camera frame{'' if n == 1 else 's'}" + (" + 1 dark" if self.dark_chk.isChecked() else ""))
        if self.hdx_box.isChecked():
            parts.append(f"{exposures} HDX exposures" +
                         (f" + {self.dark_avg_spin.value()} dark" if self.dark_chk.isChecked() else ""))
        self.plan_lbl.setText(f"Plan: {n} position{'' if n == 1 else 's'}, "
                              f"{', '.join(parts) or 'no channel selected'}. "
                              f"First move: GOTO {pre:g} nm (start − 2 steps, approach from below).")

    def _update_folder(self):
        self.folder_lbl.setText(f"Saved folder: {self.run_dir}" if self.run_dir else
                                f"New scans are saved under: {self.data_dir}")

    # ------------------------------------------------------------------ run control
    @property
    def dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)

    def start(self):
        if not self.can_start():
            QMessageBox.warning(self, "Device busy", "Stop live acquisition and wait for pending device work before scanning.")
            return
        commit_spinboxes(self)  # use typed values even if Enter was not pressed
        use_cam, use_hdx = self.cam_box.isChecked(), self.hdx_box.isChecked()
        if not use_cam and not use_hdx:
            QMessageBox.warning(self, "No channel", "Select the camera and/or the optional HDX spectrometer.")
            return
        if use_cam and self.camera_panel is None:
            QMessageBox.warning(self, "Camera unavailable", "The Camera tab is required for camera scans.")
            return
        missing = [name for name, needed, dev in (("monochromator (Arduino)", True, self.mono),
                                                  ("HDX spectrometer", use_hdx, self.hdx))
                   if needed and not dev.is_connected]
        if missing:
            QMessageBox.warning(self, "Not ready", "Connect the " + " and the ".join(missing) + " first. "
                                "A motorized scan needs the monochromator; the HDX only when it is selected.")
            return
        if self.stop_spin.value() < self.start_spin.value():
            QMessageBox.warning(self, "Invalid range", "Stop wavelength must be greater than or equal to start (equal = one wavelength).")
            return
        camera = self.camera_panel.scan_settings() if use_cam else None
        modes = {self.mono.is_simulated, bool(self.simulation_enabled())}
        if use_hdx:
            modes.add(self.hdx.is_simulated)
        if camera is not None:
            modes.add(camera["simulate"])
        if len(modes) != 1:
            QMessageBox.warning(self, "Device mode mismatch", "Reconnect all devices in the selected simulation/hardware mode.")
            return
        params = dict(kind="white" if self.white_chk.isChecked() else "sample", start=self.start_spin.value(), stop=self.stop_spin.value(),
                           step=self.step_spin.value(), hdx=use_hdx,
                           averages=self.avg_spin.value() if use_hdx else None,
                           integration_ms=self.int_spin.value() if use_hdx else None, settle_s=self.settle_spin.value(),
                           entrance_slit_um=self.entrance_slit_spin.value() or None,
                           exit_slit_um=self.exit_slit_spin.value() or None,
                           backward=self.bwd_chk.isChecked(),
                           measure_dark=self.dark_chk.isChecked(),
                           dark_averages=self.dark_avg_spin.value() if use_hdx else None,
                           simulation=self.mono.is_simulated,
                           camera=camera)
        # Fresh operator check for every scan, before any folder, worker or motion exists.
        check = {"mode": "simulation", "confirmed": False} if params["simulation"] else self._confirm_position()
        if check is None:
            self.log_message.emit("Characterization not started: monochromator position not confirmed")
            return
        params["position_check"] = check
        self.params = params
        self.recorded_devices = {"Monochromator": self.mono.port}
        if use_hdx:
            self.recorded_devices["Spectrometer"] = self.hdx.device_info
        self.rows.clear()
        self.profiles.clear()
        self.table.setRowCount(0)
        self.last_csv = None
        self.run_dir = self.data_dir / f"run_{dt.datetime.now():%Y%m%d_%H%M%S_%f}"
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.progress.setValue(0)
        draw_plots(self.plots, pd.DataFrame(), {})
        handover = self.camera_panel.begin_scan_handover() if use_cam else None  # stops live preview
        self.worker = CharacterizationWorker(self.mono, self.hdx if use_hdx else None, self.params, self.run_dir, self,
                                             camera_factory=self.camera_factory, camera_handover=handover)
        self.worker.point_done.connect(self._on_point)
        self.worker.status.connect(self.state_lbl.setText)
        self.worker.progress.connect(lambda i, n: (self.progress.setMaximum(n), self.progress.setValue(i)))
        self.worker.failed.connect(self._on_failed)
        self.worker.ask_user.connect(self._on_ask_user)
        self.worker.finished.connect(self._on_finished)
        self._set_running(True)
        self.log_message.emit("Characterization started: "
                              f"{self.params['start']:g}-{self.params['stop']:g} nm, step {self.params['step']:g} nm")
        self.worker.start()
        self.log_message.emit(f"Raw spectra directory: {self.run_dir}")

    def _confirm_position(self) -> dict | None:
        """Ask the operator to compare the Arduino position with the physical counter; never moves."""
        reported = self.mono.position_nm
        if reported is None:
            QMessageBox.warning(self, "Position unknown",
                                "The monochromator position is unknown. Use POS on the Monochromator tab and compare "
                                "it with the physical counter; correct a mismatch with SET (no motion) before scanning.")
            return None
        answer = QMessageBox.question(
            self, "Confirm monochromator position",
            f"The Arduino reports {reported:g} nm.\n\nDoes the physical wavelength counter show the same value?\n\n"
            "If unsure, choose No and check with POS. SET only defines the position; HOME and RESET move the grating.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return None
        return {"mode": "hardware", "confirmed": True, "reported_nm": reported,
                "confirmed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}

    def abort(self):
        if self.worker:
            self.worker.abort()
            self.state_lbl.setText("Aborting after current point…")

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.abort_btn.setEnabled(running)
        self.load_btn.setEnabled(not running)
        self.load_run_btn.setEnabled(not running)
        spectral = bool(self.rows) and "measured_peak" in self.dataframe  # HDX metrics exist
        self.csv_btn.setEnabled(not running and bool(self.rows))
        self.pdf_btn.setEnabled(not running and spectral)
        self.envi_btn.setEnabled(not running and bool(self.params.get("camera")) and self.run_dir is not None)
        self.white_spectra_btn.setEnabled(not running and self.run_dir is not None and self.params.get("hdx", True))
        self.cam_box.setEnabled(not running)
        self.hdx_box.setEnabled(not running)
        self._update_folder()
        self.running_changed.emit(running)

    def _on_point(self, res: dict, wl, avg):
        self.rows.append(res)
        for key, what in (("saturated_pixels", "HDX"), ("camera_saturated_pixels", "camera")):
            if res.get(key):
                self.log_message.emit(f"{what} saturation at {res['set_wavelength']:g} nm ({res['direction']}): "
                                      f"{res[key]} pixels; derived values may be invalid")
        if wl is not None:  # live spectrum and profiles only exist with the HDX
            self.live_curve.setData(wl, avg)
            self.live_set.setValue(res["set_wavelength"])
            if res["direction"] == "forward" and abs(res["set_wavelength"] / PROFILE_EVERY_NM - round(res["set_wavelength"] / PROFILE_EVERY_NM)) < 1e-6:
                self.profiles[res["set_wavelength"]] = (wl, avg)
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, key in enumerate(self.TABLE_COLS):
            v = res.get(key)
            text = f"{v:.4f}" if isinstance(v, float) else str(v)
            self.table.setItem(r, c, QTableWidgetItem(text))
        self.table.scrollToBottom()
        draw_plots(self.plots, self.dataframe, self.profiles)

    def _on_ask_user(self, text: str):
        ok = QMessageBox.question(self, "Operator action", text,
                                  QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Ok
        if self.worker is not None:
            self.worker.answer(ok)

    def _on_failed(self, msg: str):
        self.state_lbl.setText(f"ERROR: {msg}")
        self.log_message.emit(f"Characterization failed: {msg}")
        QMessageBox.critical(self, "Characterization failed", msg)

    def _on_finished(self):
        if self.run_dir and (self.run_dir / "manifest.json").exists():
            info = json.loads((self.run_dir / "manifest.json").read_text())
            self.params = info["parameters"]
            self.recorded_devices = info["devices"]
        self.worker = None
        self._set_running(False)
        if self.rows:
            self._show_summary()
            try:
                self.last_csv = self.save_csv(self.run_dir / "summary.csv")
                self.log_message.emit(f"Results auto-saved to {self.last_csv}")
            except Exception as exc:
                self.log_message.emit(f"Auto-save failed: {exc}")

    def _show_summary(self):
        df = self.dataframe
        if "measured_peak" not in df:  # camera-only run: no spectral metrics
            sat = df["camera_saturated_pixels"].fillna(0) if "camera_saturated_pixels" in df else pd.Series(dtype=float)
            lines = [f"Points: {len(df)}", f"Camera frames: {int(df['camera_mean_counts'].notna().sum()) if 'camera_mean_counts' in df else 0}",
                     f"Frames with saturation: {int((sat > 0).sum())}"]
            if "camera_mean_counts" in df and df["camera_mean_counts"].notna().any():
                lines.append(f"Mean counts: {df['camera_mean_counts'].min():.1f} – {df['camera_mean_counts'].max():.1f}")
            self.summary_lbl.setText("\n".join(lines))
            return
        s = summarize(df)
        def f(k, d=3):
            v = s.get(k)
            return "n/a" if v is None or not np.isfinite(v) else f"{v:.{d}f}"
        lines = [f"Points: {s.get('n_points')}",
                 f"Mean error: {f('mean_error_nm')} nm",
                 f"Max |error|: {f('max_abs_error_nm')} nm",
                 f"RMSE: {f('rmse_nm')} nm",
                 f"MAE: {f('mae_nm')} nm",
                 f"Mean FWHM: {f('mean_fwhm_nm')} nm",
                 f"FWHM range: {f('min_fwhm_nm')} – {f('max_fwhm_nm')} nm",
                 f"Repeatability σ: {f('repeatability_mean_std_nm', 4)} nm"]
        if "hysteresis_mean_nm" in s:
            lines.append(f"Fwd−Bwd: {f('hysteresis_mean_nm')} nm")
        self.summary_lbl.setText("\n".join(lines))

    # ------------------------------------------------------------------ I/O
    def save_csv(self, path: Path) -> Path:
        if path.suffix.lower() != ".csv":
            raise ValueError("CSV export requires a .csv path; raw files cannot be overwritten")
        df = self.dataframe
        base = CSV_COLUMNS if "measured_peak" in df else ["set_wavelength"]
        cols = base + [c for c in ("direction", "peak_mean_nm", "peak_std_nm", "reported_position", "saturated_pixels",
                                   "camera_mean_counts", "camera_saturated_pixels") if c in df]
        path.parent.mkdir(parents=True, exist_ok=True)
        df[cols].to_csv(path, index=False, float_format="%.5f")
        return path

    def export_csv_dialog(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", str(self.last_csv or self.data_dir / "results.csv"),
                                              "CSV (*.csv)")
        if path:
            try:
                self.last_csv = self.save_csv(Path(path))
                self.log_message.emit(f"CSV exported to {path}")
            except Exception as exc:
                QMessageBox.critical(self, "Export failed", str(exc))

    def _choose_white(self):
        path = QFileDialog.getExistingDirectory(self, "White-reference run", str(self.data_dir))
        if path:
            self.white_path.setText(path)

    def load_run(self):
        path = QFileDialog.getExistingDirectory(self, "Open verified run", str(self.data_dir))
        if not path:
            return
        try:
            manifest, rows, profiles, problems = load_results(Path(path), include_camera=True)
            self.run_dir = Path(path)
            self.params = manifest["parameters"]
            self.recorded_devices = manifest["devices"]
            self.rows.clear()
            self.profiles.clear()
            self.table.setRowCount(0)
            for row in rows:
                self.rows.append(row)
                index = self.table.rowCount()
                self.table.insertRow(index)
                for column, key in enumerate(self.TABLE_COLS):
                    value = row.get(key)
                    self.table.setItem(index, column, QTableWidgetItem(str(value)))
            self.profiles.update(profiles)
            self.last_csv = None
            draw_plots(self.plots, self.dataframe, self.profiles)
            if rows:
                self._show_summary()
            self._set_running(False)
            self.state_lbl.setText(f"Recovered {len(rows)} complete points; run {manifest['status']}")
            if problems:
                QMessageBox.warning(self, "Recovery details", "\n".join(problems))
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open run", str(exc))

    def load_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load results", str(self.data_dir), "CSV (*.csv)")
        if not path:
            return
        try:
            df = pd.read_csv(path)
            missing = set(CSV_COLUMNS) - set(df.columns)
            if missing:
                raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
            if "direction" not in df:
                df["direction"] = "forward"
            self.rows = df.to_dict("records")
            self.profiles.clear()  # raw spectra are not stored in the CSV
            self.last_csv = Path(path)
            self.run_dir = Path(path).parent
            info_path = self.run_dir / "manifest.json"
            if not info_path.exists():
                info_path = self.run_dir / "run.json"
            info = json.loads(info_path.read_text()) if info_path.exists() else {}
            vals = df["set_wavelength"]
            self.params = dict(start=vals.min(), stop=vals.max(),
                               step=float(np.median(np.diff(np.unique(vals)))) if len(vals) > 1 else 0,
                               averages=None, integration_ms=None, settle_s=None,
                               backward=bool((df["direction"] == "backward").any()), simulation=None)
            self.params.update(info.get("parameters", {}))
            self.recorded_devices = info.get("devices", {"Monochromator": info.get("monochromator_port", "unknown"),
                                                        "Spectrometer": info.get("spectrometer", "unknown")})
            self.table.setRowCount(0)
            for res in self.rows:
                r = self.table.rowCount()
                self.table.insertRow(r)
                for c, key in enumerate(self.TABLE_COLS):
                    v = res.get(key)
                    self.table.setItem(r, c, QTableWidgetItem(f"{v:.4f}" if isinstance(v, float) else str(v)))
            draw_plots(self.plots, df, self.profiles)
            self._show_summary()
            self._set_running(False)
            self.log_message.emit(f"Loaded {len(df)} rows from {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))

    def generate_pdf(self, path: Path | None = None, interactive: bool = True) -> Path | None:
        if not self.rows:
            return None
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            df = self.dataframe
            images = export_plots(df, self.profiles, self.report_dir / "plots", stamp)
            dev = {k: json.dumps(v) if isinstance(v, dict) else v for k, v in self.recorded_devices.items()}
            out = generate_report(path or self.report_dir / f"spectral_report_{stamp}.pdf", self.params,
                                  summarize(df), images, str(self.last_csv) if self.last_csv else None, dev)
        except Exception as exc:
            self.log_message.emit(f"Report generation failed: {exc}")
            if interactive:
                QMessageBox.critical(self, "Report failed", str(exc))
            return None
        self.log_message.emit(f"PDF report written to {out}")
        if interactive:
            QMessageBox.information(self, "Report generated", f"Report saved to:\n{out}")
        return out

    def export_white_spectra(self):
        if not self.run_dir or not self.white_path.text().strip():
            QMessageBox.warning(self, "White reference required", "Choose a verified white-reference run first.")
            return
        try:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = export_white_spectra(self.run_dir, Path(self.white_path.text()),
                                       self.run_dir.parent / f"{self.run_dir.name}_normalized_{stamp}")
            self.log_message.emit(f"White-normalized spectra: {path}")
            return path
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def export_cube(self) -> Path | None:
        """ENVI cube from the run's verified RAW16 frames (forward scan); raw files stay untouched."""
        try:
            hdr = export_envi(self.run_dir, self.run_dir.parent / f"{self.run_dir.name}_envi",
                              white_session=Path(self.white_path.text()) if self.white_path.text().strip() else None,
                              temperature_tolerance_c=self.temp_tolerance.value())
        except Exception as exc:
            self.log_message.emit(f"ENVI export failed: {exc}")
            QMessageBox.critical(self, "ENVI export failed", str(exc))
            return None
        self.log_message.emit(f"ENVI cube written to {hdr}")
        return hdr
