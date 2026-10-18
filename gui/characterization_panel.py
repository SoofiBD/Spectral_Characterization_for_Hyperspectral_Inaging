"""Automated characterization workflow: scan, analyze, plot, export, report."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
import pyqtgraph.exporters
from PySide6.QtCore import QThread, QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QLineEdit, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QProgressBar, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

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
from gui.widgets import IntSpinBox, NumberSpinBox, commit_spinboxes, hint, side_column
from gui.camera_review import CameraReview, RunPicker

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
            res["camera_file"] = str(self.run_dir / point["camera"]["file"])  # preview only, popped by the GUI
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

def draw_plots(plots: dict[str, pg.PlotWidget], df: pd.DataFrame, profiles: dict, light=False):
    """Populate the six result plots. Used for both the GUI and the PDF export."""
    fg = "#222222" if light else "#8b949e"
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

class CharacterizationPanel(QWidget):
    log_message = Signal(str)
    running_changed = Signal(bool)
    progress_text = Signal(str)  # one-line scan progress for the status bar (empty when idle)

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
        self.show_tab = lambda name: None  # set by the main window: "camera" / "mono" / "hdx"
        self.recorded_devices = {}
        self._started = 0.0
        self._build_ui()

    def _build_ui(self):

        run_box = QGroupBox("Run")
        self.ready_lbl = QLabel()
        self.ready_lbl.setTextFormat(Qt.RichText)
        self.ready_lbl.setWordWrap(True)
        self.ready_lbl.linkActivated.connect(lambda name: self.show_tab(name))
        self.plan_lbl = hint("")
        self.start_btn = QPushButton("▶  Start Scan")
        self.start_btn.setObjectName("primary")
        self.start_btn.setToolTip("Start the scan (Ctrl+Return)")
        self.abort_btn = QPushButton("■  Abort")
        self.abort_btn.setObjectName("danger")
        self.abort_btn.setToolTip("Stop after the current point. Does not physically stop a motor move in progress.")
        self.abort_btn.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m")
        self.state_lbl = QLabel("Idle")
        self.state_lbl.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_btn, 2)
        buttons.addWidget(self.abort_btn, 1)
        rl = QVBoxLayout(run_box)
        rl.addWidget(self.ready_lbl)
        rl.addWidget(self.plan_lbl)
        rl.addLayout(buttons)
        rl.addWidget(self.progress)
        rl.addWidget(self.state_lbl)

        pg_box = QGroupBox("Wavelength scan")
        self.start_spin = NumberSpinBox(decimals=1, minimum=200, maximum=1100, value=400, suffix=" nm")
        self.stop_spin = NumberSpinBox(decimals=1, minimum=200, maximum=1100, value=800, suffix=" nm")
        self.step_spin = NumberSpinBox(decimals=2, minimum=0.1, maximum=200, value=10, suffix=" nm")
        self.stop_spin.setToolTip("Equal start and stop = a single wavelength")
        self.avg_spin = IntSpinBox(minimum=1, maximum=1000, value=5)
        self.int_spin = NumberSpinBox(decimals=3, minimum=OceanHDXController.MIN_INT_MS, maximum=OceanHDXController.MAX_INT_MS, value=100, suffix=" ms")
        self.settle_spin = NumberSpinBox(decimals=2, minimum=0, maximum=60, value=0.2, suffix=" s")
        self.settle_spin.setToolTip("Wait after each move before exposing")
        self.entrance_slit_spin = NumberSpinBox(decimals=1, minimum=0, maximum=5000, value=0, suffix=" µm")
        self.exit_slit_spin = NumberSpinBox(decimals=1, minimum=0, maximum=5000, value=0, suffix=" µm")
        self.bwd_chk = QCheckBox("Include backward scan", checked=True)
        self.bwd_chk.setToolTip("Scan back down after reaching stop (hysteresis check)")
        self.dark_chk = QCheckBox("Measure background (dark) first", checked=True)
        self.dark_avg_spin = IntSpinBox(minimum=1, maximum=500, value=20)
        self.white_chk = QCheckBox("This scan measures a white reference")
        self.white_chk.setToolTip("Labels the run as a white reference for later normalization")
        f = QFormLayout(pg_box)
        f.addRow("Start:", self.start_spin)
        f.addRow("Stop:", self.stop_spin)
        f.addRow("Step:", self.step_spin)
        f.addRow("Settling:", self.settle_spin)
        f.addRow(self.bwd_chk)
        f.addRow(self.dark_chk)
        f.addRow(self.white_chk)

        optics = QGroupBox("Slits (recorded only)")
        of = QFormLayout(optics)
        of.addRow("Entrance:", self.entrance_slit_spin)
        of.addRow("Exit:", self.exit_slit_spin)
        of.addRow(hint("Written to the manifest; 0 = unknown. Nothing is moved."))

        # Camera: the primary channel. Its settings and connection live on the Camera tab.
        cam_box = QGroupBox("Camera channel")
        cam_box.setCheckable(True)
        cam_box.setChecked(True)
        self.cam_box = cam_box
        self.cam_settings_lbl = QLabel()
        self.cam_settings_lbl.setObjectName("readout")
        self.cam_settings_lbl.setWordWrap(True)
        cam_link = QLabel('<a href="camera" style="color:#58a6ff">Edit on the Camera tab</a>')
        cam_link.linkActivated.connect(lambda name: self.show_tab(name))
        cf = QVBoxLayout(cam_box)
        cf.addWidget(self.cam_settings_lbl)
        cf.addWidget(cam_link)

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

        white_box = QGroupBox("White reference")
        self.white_path = QLineEdit()
        self.white_path.setPlaceholderText("None (optional)")
        self.white_path.setClearButtonEnabled(True)
        white_select = QPushButton("Choose…")
        white_select.clicked.connect(self._choose_white)
        self.temp_tolerance = NumberSpinBox(minimum=0, maximum=100, value=2, decimals=1, suffix=" °C")
        self.temp_tolerance.setToolTip("Maximum sensor temperature difference between a run and its references")
        wh = QHBoxLayout()
        wh.addWidget(self.white_path, 1)
        wh.addWidget(white_select)
        wf = QFormLayout(white_box)
        wf.addRow(wh)
        wf.addRow("ΔT limit:", self.temp_tolerance)
        wf.addRow(hint("Used by ENVI export, white-normalized spectra and camera review."))

        out_box = QGroupBox("Results")
        self.summary_lbl = QLabel("—")
        self.summary_lbl.setObjectName("summary")
        self.summary_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.folder_lbl = hint("")
        self.folder_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.folder_btn = QPushButton("Open Folder")
        self.folder_btn.setToolTip("Open the current run folder (or the data folder)")
        self.load_run_btn = QPushButton("Open Run…")
        self.load_run_btn.setToolTip("Open a recorded run, including interrupted ones (Ctrl+O)")
        self.load_btn = QPushButton("Load CSV…")
        self.camera_review_btn = QPushButton("Review ROI…")
        self.camera_review_btn.setToolTip("Read-only review of a camera run: frames and region-of-interest spectra")
        self.csv_btn = QPushButton("CSV…")
        self.csv_btn.setToolTip("Export the result table as CSV")
        self.pdf_btn = QPushButton("PDF Report")
        self.pdf_btn.setToolTip("PDF report with plots (needs HDX spectral data)")
        self.envi_btn = QPushButton("ENVI Cube")
        self.envi_btn.setToolTip("ENVI cube from the camera frames (forward scan); white-corrected if a reference is chosen")
        self.white_spectra_btn = QPushButton("White Spectra")
        self.white_spectra_btn.setToolTip("Export HDX spectra normalized by the chosen white reference")
        for b in (self.csv_btn, self.pdf_btn, self.white_spectra_btn, self.envi_btn):
            b.setEnabled(False)
        grid = QGridLayout()
        for i, b in enumerate((self.load_run_btn, self.load_btn, self.camera_review_btn, self.folder_btn)):
            grid.addWidget(b, i // 2, i % 2)
        grid.addWidget(hint("Export"), 2, 0, 1, 2)
        for i, b in enumerate((self.csv_btn, self.pdf_btn, self.envi_btn, self.white_spectra_btn)):
            grid.addWidget(b, 3 + i // 2, i % 2)
        ol = QVBoxLayout(out_box)
        ol.addWidget(self.summary_lbl)
        ol.addWidget(self.folder_lbl)
        ol.addLayout(grid)
        self.param_boxes = (pg_box, optics, cam_box, hdx_box)

        self.cam_view = pg.PlotWidget(title="Last Camera Frame")
        self.cam_view.setAspectLocked(True)
        self.cam_view.invertY(True)
        self.cam_view.hideAxis("left")
        self.cam_view.hideAxis("bottom")
        self.cam_image = pg.ImageItem(axisOrder="row-major")
        self.cam_view.addItem(self.cam_image)
        self.live_plot = pg.PlotWidget(title="Current Spectrum (HDX)")
        # Plain labels: with units= pyqtgraph auto-scales to SI prefixes ("0.8 knm", "kcounts")
        self.live_plot.setLabel("bottom", "Wavelength (nm)")
        self.live_plot.setLabel("left", "Intensity (counts)")
        for ax in ("bottom", "left"):
            self.live_plot.getAxis(ax).enableAutoSIPrefix(False)
        self.live_plot.showGrid(x=True, y=True, alpha=0.25)
        self.live_curve = self.live_plot.plot(pen=pg.mkPen("#4fc3f7", width=1.5))
        self.live_set = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ffb74d", style=Qt.DashLine))
        self.live_plot.addItem(self.live_set)
        live = QSplitter(Qt.Horizontal)
        live.addWidget(self.cam_view)
        live.addWidget(self.live_plot)
        live.setSizes([500, 500])

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.cam_plot = pg.PlotWidget()
        self.cam_plot.setLabel("bottom", "Set wavelength (nm)")
        self.cam_plot.setLabel("left", "Mean counts (raw)")
        for ax in ("bottom", "left"):
            self.cam_plot.getAxis(ax).enableAutoSIPrefix(False)
        self.cam_plot.showGrid(x=True, y=True, alpha=0.25)
        self.cam_plot.addLegend(offset=(10, 10))
        self.tabs.addTab(self.cam_plot, "Camera Signal")
        self.plots: dict[str, pg.PlotWidget] = {}
        for key, title, *_ in PLOT_DEFS:
            pw = pg.PlotWidget()
            self.plots[key] = pw
            self.tabs.addTab(pw, title.split(" (")[0])
        self.table = QTableWidget(0, len(self.TABLE_COLS))
        self.table.setHorizontalHeaderLabels(self.TABLE_COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.tabs.addTab(self.table, "Data Table")
        draw_plots(self.plots, pd.DataFrame(), {})

        split = QSplitter(Qt.Vertical)
        split.addWidget(live)
        split.addWidget(self.tabs)
        split.setSizes([300, 500])

        root = QHBoxLayout(self)
        root.addWidget(side_column(pg_box, cam_box, hdx_box, optics, white_box, out_box, header=run_box))
        root.addWidget(split, 1)

        self.start_btn.clicked.connect(self.start)
        self.abort_btn.clicked.connect(self.abort)
        self.csv_btn.clicked.connect(self.export_csv_dialog)
        self.load_btn.clicked.connect(self.load_csv)
        self.load_run_btn.clicked.connect(self.load_run)
        self.camera_review_btn.clicked.connect(self.review_camera)
        self.pdf_btn.clicked.connect(self.generate_pdf)
        self.envi_btn.clicked.connect(self.export_cube)
        self.white_spectra_btn.clicked.connect(self.export_white_spectra)
        self.folder_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_dir or self.data_dir))))
        QShortcut(QKeySequence("Ctrl+Return"), self,
                  lambda: self.start_btn.isVisible() and self.start_btn.isEnabled() and self.start_btn.click())
        self._update_plan()
        self._update_folder()

    def scan_summary(self) -> tuple[int, int, float]:
        """Positions, HDX sample exposures (0 without HDX) and first-move target (mirrors ScanCoordinator)."""
        step = self.step_spin.value()
        plan = scan_plan(self.start_spin.value(), self.stop_spin.value(), step, self.bwd_chk.isChecked())
        hdx = len(plan) * self.avg_spin.value() if self.hdx_box.isChecked() else 0
        return len(plan), hdx, max(plan[0][1] - 2 * step, 200.0)

    def _update_plan(self, *_):
        self.cam_view.setVisible(self.cam_box.isChecked())
        self.live_plot.setVisible(self.hdx_box.isChecked())
        self.update_readiness()
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

    def showEvent(self, event):
        super().showEvent(event)
        self.update_readiness()  # Camera tab settings may have changed meanwhile

    def update_readiness(self, *_):
        """Checklist of what the next scan needs; links jump to the tab that fixes a missing item."""
        def line(state, text, link=None):
            color = {"ok": "#3fb950", "sim": "#d29922", "off": "#f85149", "idle": "#8b949e"}[state]
            mark = "●" if state in ("ok", "sim") else "○"
            tail = f' &nbsp;<a href="{link}" style="color:#58a6ff">Connect…</a>' if link else ""
            return f'<span style="color:{color}">{mark}</span>&nbsp; {text}{tail}'
        def device(dev):
            return "sim" if dev.is_simulated else "ok"
        lines = []
        if self.mono.is_connected:
            pos = self.mono.position_nm
            lines.append(line(device(self.mono), "Monochromator" + (f" at {pos:g} nm" if pos is not None else
                                                                    " (position unknown)")))
        else:
            lines.append(line("off", "Monochromator — required", "mono"))
        cam_settings = "—"
        if self.cam_box.isChecked() and self.camera_panel is not None:
            client = self.camera_panel.client
            if client is not None:
                lines.append(line("sim" if client.simulated else "ok", "Camera connected"))
            else:
                lines.append(line("idle", "Camera — the scan opens it itself", "camera"))
            c = self.camera_panel  # displayed values only; scan_settings() commits typed text at start
            w, h = c.roi_spins[2].value(), c.roi_spins[3].value()
            cam_settings = (f"{c.exp_spin.value():g} ms · gain {c.gain_spin.value()} · offset {c.offset_spin.value()}\n"
                            f"bin {c.bin_spin.value()} · ROI " + (f"{w}×{h}" if w and h else "full sensor"))
        self.cam_settings_lbl.setText(cam_settings)
        if self.hdx_box.isChecked():
            lines.append(line(device(self.hdx), "HDX connected") if self.hdx.is_connected else
                         line("off", "HDX — required when selected", "hdx"))
        if not self.cam_box.isChecked() and not self.hdx_box.isChecked():
            lines.append(line("off", "Select the camera and/or the HDX channel"))
        self.ready_lbl.setText("<br>".join(lines))

    def _update_folder(self):
        self.folder_lbl.setText(f"Saved folder: {self.run_dir}" if self.run_dir else
                                f"New scans are saved under: {self.data_dir}")

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
        self._started = time.monotonic()
        draw_plots(self.plots, pd.DataFrame(), {})
        self._draw_camera_signal()
        self.cam_image.clear()
        self.live_curve.setData([], [])
        self.tabs.setCurrentWidget(self.plots["accuracy"] if use_hdx else self.cam_plot)
        handover = self.camera_panel.begin_scan_handover() if use_cam else None  # stops live preview
        self.worker = CharacterizationWorker(self.mono, self.hdx if use_hdx else None, self.params, self.run_dir, self,
                                             camera_factory=self.camera_factory, camera_handover=handover)
        self.worker.point_done.connect(self._on_point)
        self.worker.status.connect(self.state_lbl.setText)
        self.worker.progress.connect(self._on_progress)
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

    def _on_progress(self, i: int, n: int):
        self.progress.setMaximum(n)
        self.progress.setValue(i)
        left = (time.monotonic() - self._started) / i * (n - i) if i else 0
        eta = f"~{left / 60:.0f} min left" if left >= 90 else f"~{left:.0f} s left"
        self.progress.setFormat(f"%v / %m · {eta}" if i < n else "%v / %m")
        self.progress_text.emit(f"Scan {i}/{n} · {eta}" if i < n else f"Scan {i}/{n}")

    def abort(self):
        if self.worker:
            self.worker.abort()
            self.state_lbl.setText("Aborting after current point…")

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.abort_btn.setEnabled(running)
        self.load_btn.setEnabled(not running)
        self.load_run_btn.setEnabled(not running)
        self.camera_review_btn.setEnabled(not running)  # opens the run picker (current run preselected)
        spectral = bool(self.rows) and "measured_peak" in self.dataframe  # HDX metrics exist
        self.csv_btn.setEnabled(not running and bool(self.rows))
        self.pdf_btn.setEnabled(not running and spectral)
        self.envi_btn.setEnabled(not running and bool(self.params.get("camera")) and self.run_dir is not None)
        self.white_spectra_btn.setEnabled(not running and self.run_dir is not None and self.params.get("hdx", True))
        for box in self.param_boxes:  # the running scan has its parameters; editing would only mislead
            box.setEnabled(not running)
        if running:
            self.progress.setFormat("%v / %m")
        else:
            self.progress_text.emit("")
        self._update_folder()
        self.running_changed.emit(running)

    def _on_point(self, res: dict, wl, avg):
        frame_path = res.pop("camera_file", None)
        self.rows.append(res)
        if frame_path is not None:
            self._show_camera_frame(Path(frame_path))
            self._draw_camera_signal()
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

    def _show_camera_frame(self, path: Path):
        """Display a decimated copy of a recorded frame (the raw file is only read)."""
        try:
            frame = np.load(path, mmap_mode="r", allow_pickle=False)
            step = max(1, int(np.ceil(max(frame.shape) / 1000)))
            view = np.asarray(frame[::step, ::step], dtype=np.float32)
        except (OSError, ValueError) as exc:
            self.log_message.emit(f"Camera preview unavailable: {exc}")
            return
        lo, hi = np.percentile(view, (0.5, 99.5))
        self.cam_image.setImage(view, autoLevels=False, levels=(float(lo), float(hi) if hi > lo else float(lo) + 1))

    def _draw_camera_signal(self):
        """Mean raw counts per set wavelength (camera channel), forward and backward separately."""
        pw = self.cam_plot
        pw.clear()
        pw.plotItem.legend.clear()
        df = self.dataframe
        if df.empty or "camera_mean_counts" not in df:
            return
        for direction, color, symbol in (("forward", "#4fc3f7", "o"), ("backward", "#ff8a65", "t")):
            part = df[(df["direction"] == direction) & df["camera_mean_counts"].notna()].sort_values("set_wavelength")
            if len(part):
                pw.plot(part["set_wavelength"].to_numpy(), part["camera_mean_counts"].to_numpy(),
                        pen=pg.mkPen(color, width=2), symbol=symbol, symbolSize=6, symbolBrush=color,
                        name=direction.capitalize())

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
        self.worker = None  # first: an unreadable manifest must not leave the controls locked
        if self.run_dir and (self.run_dir / "manifest.json").exists():
            try:
                info = json.loads((self.run_dir / "manifest.json").read_text())
                self.params = info["parameters"]
                self.recorded_devices = info["devices"]
            except (OSError, ValueError, KeyError) as exc:
                self.log_message.emit(f"Run manifest could not be read: {exc}")
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
        path = RunPicker.choose(self.data_dir, self, kind="white", title="Choose white-reference run", camera_only=False,
                                current=Path(self.white_path.text()) if self.white_path.text().strip() else None)
        if path:
            self.white_path.setText(str(path))

    def review_camera(self):
        """Pick a verified camera run (current one preselected) and open the read-only ROI review."""
        path = RunPicker.choose(self.data_dir, self, current=self.run_dir, title="Review camera run")
        if path is None:
            return
        white = self.white_path.text().strip()
        try:
            CameraReview(path, self, self.temp_tolerance.value(), Path(white) if white else None).exec()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot review camera scan", str(exc))

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
            self._draw_camera_signal()
            if "measured_peak" not in self.dataframe:
                self.tabs.setCurrentWidget(self.cam_plot)
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
            self._draw_camera_signal()
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
