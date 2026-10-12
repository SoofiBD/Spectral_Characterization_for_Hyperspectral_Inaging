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
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from acquisition.coordinator import ScanCoordinator, State, scan_plan
from analysis.cube_export import export_envi
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
    point_done = Signal(dict, object, object)   # result row, wavelength array, averaged spectrum
    status = Signal(str)
    progress = Signal(int, int)
    failed = Signal(str)
    ask_user = Signal(str)   # GUI shows the message, then calls answer(ok)

    def __init__(self, mono: MonochromatorController, hdx: OceanHDXController,
                 params: dict, run_dir: Path, parent=None):
        super().__init__(parent)
        self.mono, self.hdx, self.p = mono, hdx, params
        self.run_dir = run_dir
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
        self.ask_user.emit(text)
        self._answer.wait()
        return self._answer_ok and not self._abort

    def _on_point(self, point: dict, wl: np.ndarray, raw_scans: list):
        """Analysis of one completed (already saved) point; raw data is never changed here."""
        w = point["target_nm"]
        scans = [scan - self.hdx.dark if self.hdx.dark_active else scan for scan in raw_scans]
        avg = np.mean(scans, axis=0)
        res = analyze_spectrum(wl, avg, w).to_dict()
        mean_pk, std_pk = peak_repeatability(wl, scans, w)
        res.update(direction=point["direction"], peak_mean_nm=mean_pk, peak_std_nm=std_pk,
                   reported_position=point["reported_nm"],
                   saturated_pixels=int(np.count_nonzero(np.asarray(raw_scans) >= 65535)))
        self.point_done.emit(res, wl, avg)
        self.progress.emit(point["index"] + 1, self._total)

    def run(self):
        p = self.p
        self._total = len(scan_plan(p["start"], p["stop"], p["step"], p["backward"]))
        camera = None
        try:
            if p.get("camera"):
                camera = ASICameraClient(simulate=p["camera"]["simulate"], log_path=self.run_dir / "camera.log")
            self.hdx.subtract_dark = True
            p["dark"] = ("measured before scan" if p["measure_dark"] else
                         "existing dark" if self.hdx.dark_valid else "none (raw counts)")
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
            if camera is not None:
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
    if df is None or df.empty:
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

    TABLE_COLS = CSV_COLUMNS + ["peak_std_nm", "direction", "saturated_pixels"]

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
        f.addRow("Averages:", self.avg_spin)
        f.addRow("Integration:", self.int_spin)
        f.addRow("Settling time:", self.settle_spin)
        f.addRow("Entrance slit (0 = unknown):", self.entrance_slit_spin)
        f.addRow("Exit slit (0 = unknown):", self.exit_slit_spin)
        f.addRow(self.bwd_chk)
        f.addRow(self.dark_chk)
        f.addRow("Dark averages:", self.dark_avg_spin)
        self.dark_chk.toggled.connect(self.dark_avg_spin.setEnabled)

        cam_box = QGroupBox("Camera (ZWO ASI, RAW16)")
        cam_box.setCheckable(True)
        cam_box.setChecked(False)
        self.cam_box = cam_box
        self.cam_exp_spin = NumberSpinBox(decimals=3, minimum=0.032, maximum=600_000, value=100, suffix=" ms")
        self.cam_gain_spin = IntSpinBox(minimum=0, maximum=600, value=100)
        self.cam_offset_spin = IntSpinBox(minimum=0, maximum=255, value=50)
        self.cam_bin_spin = IntSpinBox(minimum=1, maximum=4, value=1)
        cf = QFormLayout(cam_box)
        cf.addRow("Exposure:", self.cam_exp_spin)
        cf.addRow("Gain:", self.cam_gain_spin)
        cf.addRow("Offset:", self.cam_offset_spin)
        cf.addRow("Binning:", self.cam_bin_spin)

        run_box = QGroupBox("Run")
        self.start_btn = QPushButton("▶  Start Characterization")
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
        self.pdf_btn = QPushButton("Generate PDF Report")
        self.envi_btn = QPushButton("Export ENVI Cube")
        self.csv_btn.setEnabled(False)
        self.pdf_btn.setEnabled(False)
        self.envi_btn.setEnabled(False)
        ol = QVBoxLayout(out_box)
        for w in (self.csv_btn, self.load_btn, self.pdf_btn, self.envi_btn):
            ol.addWidget(w)

        summ_box = QGroupBox("Summary")
        self.summary_lbl = QLabel("—")
        self.summary_lbl.setObjectName("summary")
        self.summary_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        QVBoxLayout(summ_box).addWidget(self.summary_lbl)

        side = QVBoxLayout()
        for b in (pg_box, cam_box, run_box, out_box, summ_box):
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
        self.pdf_btn.clicked.connect(self.generate_pdf)
        self.envi_btn.clicked.connect(self.export_cube)

    # ------------------------------------------------------------------ run control
    @property
    def dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)

    def start(self):
        commit_spinboxes(self)  # use typed values even if Enter was not pressed
        if not self.mono.is_connected or not self.hdx.is_connected:
            QMessageBox.warning(self, "Not ready", "Connect both the monochromator and the spectrometer first.")
            return
        if self.stop_spin.value() <= self.start_spin.value():
            QMessageBox.warning(self, "Invalid range", "Stop wavelength must be greater than start.")
            return
        self.params = dict(start=self.start_spin.value(), stop=self.stop_spin.value(),
                           step=self.step_spin.value(), averages=self.avg_spin.value(),
                           integration_ms=self.int_spin.value(), settle_s=self.settle_spin.value(),
                           entrance_slit_um=self.entrance_slit_spin.value() or None,
                           exit_slit_um=self.exit_slit_spin.value() or None,
                           backward=self.bwd_chk.isChecked(),
                           measure_dark=self.dark_chk.isChecked(),
                           dark_averages=self.dark_avg_spin.value(),
                           simulation=self.mono.is_simulated or self.hdx.is_simulated,
                           camera=dict(exposure_us=round(self.cam_exp_spin.value() * 1000),
                                       gain=self.cam_gain_spin.value(),
                                       offset=self.cam_offset_spin.value(),
                                       bin=self.cam_bin_spin.value(),
                                       simulate=bool(self.simulation_enabled()))
                           if self.cam_box.isChecked() else None)
        self.rows.clear()
        self.profiles.clear()
        self.table.setRowCount(0)
        self.last_csv = None
        self.run_dir = self.data_dir / f"run_{dt.datetime.now():%Y%m%d_%H%M%S_%f}"
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.progress.setValue(0)
        draw_plots(self.plots, pd.DataFrame(), {})
        self.worker = CharacterizationWorker(self.mono, self.hdx, self.params, self.run_dir, self)
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

    def abort(self):
        if self.worker:
            self.worker.abort()
            self.state_lbl.setText("Aborting after current point…")

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.abort_btn.setEnabled(running)
        self.load_btn.setEnabled(not running)
        self.csv_btn.setEnabled(not running and bool(self.rows))
        self.pdf_btn.setEnabled(not running and bool(self.rows))
        self.envi_btn.setEnabled(not running and bool(self.params.get("camera")) and self.run_dir is not None)
        self.cam_box.setEnabled(not running)
        self.running_changed.emit(running)

    def _on_point(self, res: dict, wl, avg):
        self.rows.append(res)
        if res["saturated_pixels"]:
            self.log_message.emit(f"Saturation at {res['set_wavelength']:g} nm ({res['direction']}): "
                                  f"{res['saturated_pixels']} pixels; derived metrics may be invalid")
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
        s = summarize(self.dataframe)
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
        df = self.dataframe
        cols = CSV_COLUMNS + [c for c in ("direction", "peak_mean_nm", "peak_std_nm", "reported_position", "saturated_pixels")
                              if c in df]
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
            vals = df["set_wavelength"]
            self.params = dict(start=vals.min(), stop=vals.max(),
                               step=float(np.median(np.diff(np.unique(vals)))) if len(vals) > 1 else 0,
                               averages=self.avg_spin.value(), integration_ms=self.int_spin.value(),
                               settle_s=self.settle_spin.value(),
                               backward=bool((df["direction"] == "backward").any()), simulation=False)
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
            dev = {"Monochromator": self.mono.port or "n/a", "Spectrometer": self.hdx.device_info}
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

    def export_cube(self) -> Path | None:
        """ENVI cube from the run's verified RAW16 frames (forward scan); raw files stay untouched."""
        try:
            hdr = export_envi(self.run_dir, self.run_dir.parent / f"{self.run_dir.name}_envi")
        except Exception as exc:
            self.log_message.emit(f"ENVI export failed: {exc}")
            QMessageBox.critical(self, "ENVI export failed", str(exc))
            return None
        self.log_message.emit(f"ENVI cube written to {hdr}")
        return hdr
