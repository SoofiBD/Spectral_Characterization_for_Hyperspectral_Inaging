"""Ocean HDX spectrometer panel with live PyQtGraph spectrum."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QMessageBox,
                               QSpinBox,
                               QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from analysis.spectral_analysis import analyze_spectrum
from devices.ocean_hdx import OceanHDXController
from gui.widgets import IntSpinBox, NumberSpinBox, commit_spinboxes
from gui.workers import LiveAcquisitionThread, run_task


class HDXPanel(QWidget):
    connection_changed = Signal(bool)
    log_message = Signal(str)

    def __init__(self, controller: OceanHDXController, simulation_enabled, data_dir: Path, parent=None):
        super().__init__(parent)
        self.ctrl = controller
        self.simulation_enabled = simulation_enabled
        self.data_dir = data_dir
        self._live: LiveAcquisitionThread | None = None
        self._stopping: set[LiveAcquisitionThread] = set()
        # Live view pulls the newest frame on a timer; the interval adapts to the draw cost
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(50)
        self._live_timer.timeout.connect(self._draw_live_frame)
        self._last: tuple[np.ndarray, np.ndarray] | None = None
        self._external_busy = False
        self._build_ui()
        self._update_state()

    def _build_ui(self):
        conn = QGroupBox("Spectrometer")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.info_lbl = QLabel("Not connected")
        self.info_lbl.setObjectName("statusBad")
        cf = QFormLayout(conn)
        cf.addRow(self.connect_btn)
        cf.addRow("Device:", self.info_lbl)

        acq = QGroupBox("Acquisition")
        self.int_spin = NumberSpinBox(decimals=3, minimum=OceanHDXController.MIN_INT_MS,
                                       maximum=OceanHDXController.MAX_INT_MS, value=100.0, suffix=" ms")
        self.apply_int_btn = QPushButton("Apply")
        self.acquire_btn = QPushButton("Acquire")
        self.live_btn = QPushButton("Start Live")
        self.live_btn.setCheckable(True)
        self.save_btn = QPushButton("Save Spectrum…")
        self.autoscale_chk = QCheckBox("Auto-scale Y", checked=True)
        self.autoscale_chk.toggled.connect(lambda on: setattr(self, "_y_range", None))
        ih = QHBoxLayout()
        ih.addWidget(self.int_spin, 1)
        ih.addWidget(self.apply_int_btn)
        af = QFormLayout(acq)
        af.addRow("Integration:", ih)
        af.addRow(self.acquire_btn)
        af.addRow(self.live_btn)
        af.addRow(self.save_btn)
        af.addRow(self.autoscale_chk)

        bg = QGroupBox("Background (dark)")
        self.dark_avg_spin = IntSpinBox(minimum=1, maximum=500, value=10)
        self.dark_btn = QPushButton("Take Dark…")
        self.dark_clear_btn = QPushButton("Clear")
        self.subtract_chk = QCheckBox("Subtract dark", checked=True)
        self.dark_lbl = QLabel("No dark")
        self.dark_lbl.setWordWrap(True)
        dh = QHBoxLayout()
        dh.addWidget(self.dark_btn, 1)
        dh.addWidget(self.dark_clear_btn)
        bf = QFormLayout(bg)
        bf.addRow("Averages:", self.dark_avg_spin)
        bf.addRow(dh)
        bf.addRow(self.subtract_chk)
        bf.addRow("Status:", self.dark_lbl)

        stats = QGroupBox("Line analysis")
        self.peak_lbl = QLabel("—")
        self.fwhm_lbl = QLabel("—")
        self.max_lbl = QLabel("—")
        self.snr_lbl = QLabel("—")
        for l in (self.peak_lbl, self.fwhm_lbl, self.max_lbl, self.snr_lbl):
            l.setObjectName("readout")
        sf = QFormLayout(stats)
        sf.addRow("Peak:", self.peak_lbl)
        sf.addRow("FWHM:", self.fwhm_lbl)
        sf.addRow("Max counts:", self.max_lbl)
        sf.addRow("SNR:", self.snr_lbl)

        side = QVBoxLayout()
        side.addWidget(conn)
        side.addWidget(acq)
        side.addWidget(bg)
        side.addWidget(stats)
        side.addStretch(1)
        sw = QWidget()
        sw.setLayout(side)
        sw.setFixedWidth(300)

        self.plot = pg.PlotWidget(title="Live Spectrum")
        # Plain labels: with units= pyqtgraph auto-scales to SI prefixes ("0.8 knm", "kcounts")
        self.plot.setLabel("bottom", "Wavelength (nm)")
        self.plot.setLabel("left", "Intensity (counts)")
        for ax in ("bottom", "left"):
            self.plot.getAxis(ax).enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        # Fixed axis width: changing tick-label widths would otherwise re-layout (jump) the plot
        self.plot.getAxis("left").setWidth(70)
        self._y_range: tuple[float, float] | None = None
        # No antialiasing on the live curve: it dominates draw time on slower machines
        self.curve = self.plot.plot(pen=pg.mkPen("#4fc3f7", width=1), antialias=False)
        self.curve.setClipToView(True)
        self.peak_marker = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ffb74d", style=pg.QtCore.Qt.DashLine))
        self.plot.addItem(self.peak_marker)
        self.peak_marker.hide()

        root = QHBoxLayout(self)
        root.addWidget(sw)
        root.addWidget(self.plot, 1)

        self.connect_btn.clicked.connect(self._toggle_connect)
        self.apply_int_btn.clicked.connect(self._apply_integration)
        self.dark_btn.clicked.connect(self._take_dark)
        self.dark_clear_btn.clicked.connect(lambda: (self.ctrl.clear_dark(), self._update_state()))
        self.subtract_chk.toggled.connect(self._set_subtract)
        self.acquire_btn.clicked.connect(self._acquire_once)
        self.live_btn.toggled.connect(self._toggle_live)
        self.save_btn.clicked.connect(self._save)

    # ------------------------------------------------------------------ state
    def _update_state(self):
        c = self.ctrl.is_connected
        live = self._live is not None
        self.connect_btn.setText("Disconnect" if c else "Connect")
        self.connect_btn.setEnabled(not live and not self._external_busy)
        for w in (self.apply_int_btn, self.acquire_btn, self.int_spin):
            w.setEnabled(c and not live and not self._external_busy)
        self.live_btn.setEnabled(c and not self._external_busy)
        self.save_btn.setEnabled(self._last is not None)
        self.dark_btn.setEnabled(c and not live and not self._external_busy)
        self.dark_clear_btn.setEnabled(self.ctrl.dark is not None and not self._external_busy)
        self.subtract_chk.setChecked(self.ctrl.subtract_dark)
        self._update_dark_label()
        self.info_lbl.setText(self.ctrl.device_info)
        self.info_lbl.setObjectName("statusOk" if c else "statusBad")
        self.info_lbl.style().unpolish(self.info_lbl)
        self.info_lbl.style().polish(self.info_lbl)

    def _update_dark_label(self):
        c = self.ctrl
        if c.dark is None:
            text, color = "No dark — raw counts", "#d29922"
        elif not c.dark_valid:
            text, color = (f"Invalid: taken at {c.dark_integration_ms:g} ms, "
                           f"now {c.integration_time_ms:g} ms — retake"), "#f85149"
        elif not c.subtract_dark:
            text, color = f"Valid ({c.dark_integration_ms:g} ms), not subtracted", "#d29922"
        else:
            text, color = f"Subtracting ({c.dark_integration_ms:g} ms)", "#3fb950"
        self.dark_lbl.setText(text)
        self.dark_lbl.setStyleSheet(f"color:{color}; font-weight:600;")

    def _set_subtract(self, on: bool):
        self.ctrl.subtract_dark = on
        self._update_dark_label()

    def _take_dark(self):
        commit_spinboxes(self)
        if not self.ctrl.is_simulated:
            if QMessageBox.question(
                    self, "Background measurement",
                    "Block the light (close the shutter or turn the lamp off), then press OK.",
                    QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
                return
        self.dark_btn.setEnabled(False)
        n = self.dark_avg_spin.value()
        ms = self.int_spin.value()
        sim = self.ctrl.sim_state if self.ctrl.is_simulated else None

        def go():
            self.ctrl.set_integration_time(ms)
            if sim:
                sim.light_blocked = True
            try:
                return self.ctrl.acquire_dark(n)
            finally:
                if sim:
                    sim.light_blocked = False

        def ok(dark):
            self.log_message.emit(f"Dark taken: {n} averages at {ms:g} ms, mean {dark.mean():.0f} counts")
            if not self.ctrl.is_simulated:
                QMessageBox.information(self, "Background measurement", "Dark stored. Restore the light.")

        run_task(go, on_result=ok, on_error=lambda m: self.log_message.emit(f"Dark failed: {m}"),
                 on_finished=self._update_state)

    def set_external_busy(self, busy: bool):
        if busy and self._live:
            self.live_btn.setChecked(False)
        self._external_busy = busy
        self._update_state()

    # ------------------------------------------------------------------ actions
    def _toggle_connect(self):
        if self.ctrl.is_connected:
            self.ctrl.disconnect()
            self._update_state()
            self.connection_changed.emit(False)
            self.log_message.emit("Spectrometer disconnected")
            return
        self.connect_btn.setEnabled(False)
        self.info_lbl.setText("Connecting…")

        def ok(_):
            self.ctrl.set_integration_time(self.int_spin.value())
            self._update_state()
            self.connection_changed.emit(True)
            self.log_message.emit(f"Spectrometer connected: {self.ctrl.device_info}")

        def fail(msg):
            self._update_state()
            self.log_message.emit(f"Spectrometer connection failed: {msg}")

        run_task(self.ctrl.connect, self.simulation_enabled(), on_result=ok, on_error=fail,
                 on_finished=self._update_state)

    def _apply_integration(self):
        commit_spinboxes(self)
        run_task(self.ctrl.set_integration_time, self.int_spin.value(),
                 on_result=lambda _: self.log_message.emit(
                     f"Integration time set to {self.int_spin.value():g} ms"),
                 on_error=lambda m: self.log_message.emit(f"Spectrometer: {m}"),
                 on_finished=self._update_state)

    def _acquire_once(self):
        commit_spinboxes(self)
        self.acquire_btn.setEnabled(False)

        def go():
            self.ctrl.set_integration_time(self.int_spin.value())
            return self.ctrl.acquire_spectrum()

        run_task(go, on_result=lambda r: self.show_spectrum(*r),
                 on_error=lambda m: self.log_message.emit(f"Spectrometer: {m}"),
                 on_finished=self._update_state)

    def _toggle_live(self, on: bool):
        if on:
            if self._live is not None:
                return
            commit_spinboxes(self)
            ms = self.int_spin.value()
            self._live = LiveAcquisitionThread(self.ctrl, self)
            thread = self._live
            thread.error.connect(lambda m, t=thread: self._on_live_error(t, m))
            # Set the integration time in the worker thread, never on the GUI thread
            run_task(self.ctrl.set_integration_time, ms,
                     on_error=lambda m: self.log_message.emit(f"Spectrometer: {m}"),
                     on_finished=lambda t=thread: t.start() if t is self._live else None)
            self._live_timer.start()
            self.live_btn.setText("Stop Live")
        else:
            self.stop_live()
            self.live_btn.setText("Start Live")
        self._update_state()

    def _on_live_error(self, thread, msg: str):
        if thread is self._live:
            self.log_message.emit(f"Live: {msg}")
            self.live_btn.setChecked(False)

    def _draw_live_frame(self):
        if self._live is None:
            return
        frame = self._live.take_latest()
        if frame is None:
            return
        t0 = time.perf_counter()
        self.show_spectrum(*frame)
        cost_ms = (time.perf_counter() - t0) * 1000
        # Leave the event loop at least half the time for user input
        self._live_timer.setInterval(int(min(500, max(50, 2 * cost_ms))))

    def stop_live(self, block: bool = False):
        """Stop live mode immediately. The thread finishes its current exposure in the
        background (the controller lock prevents overlapping acquisitions)."""
        self._live_timer.stop()
        thread, self._live = self._live, None
        if thread is None:
            return
        thread.stop()
        if block:
            thread.wait(15000)
        elif thread.isRunning():
            self._stopping.add(thread)
            thread.finished.connect(lambda t=thread: self._stopping.discard(t))

    def show_spectrum(self, wl, inten, set_wavelength=None):
        self._last = (wl, inten)
        self.curve.setData(wl, inten)
        if self.autoscale_chk.isChecked():
            self._smooth_autoscale(inten)
        r = analyze_spectrum(wl, inten, set_wavelength)
        self.max_lbl.setText(f"{np.max(inten):.0f}")
        self.snr_lbl.setText(f"{r.snr:.1f}")
        if np.isfinite(r.measured_peak):
            self.peak_lbl.setText(f"{r.measured_peak:.3f} nm")
            self.fwhm_lbl.setText(f"{r.fwhm_nm:.3f} nm")
            self.peak_marker.setValue(r.measured_peak)
            self.peak_marker.show()
        else:
            self.peak_lbl.setText("no line")
            self.fwhm_lbl.setText("—")
            self.peak_marker.hide()
        self.save_btn.setEnabled(True)

    def _smooth_autoscale(self, inten):
        """Hysteresis autoscale: grow at once when the data leaves the range, shrink only when
        the data uses less than half of it. Noise alone never changes the axis, so it can't jitter."""
        lo, hi = float(np.min(inten)), float(np.max(inten))
        span = max(hi - lo, 50.0)  # never zoom into pure noise
        want = (lo - 0.1 * span, hi + 0.1 * span)
        cur = self._y_range
        if cur is not None:
            fits = cur[0] <= lo and hi <= cur[1]
            too_loose = (hi - lo) < 0.5 * (cur[1] - cur[0]) and span > 50.0 or \
                        max(hi - lo, 50.0) < 0.25 * (cur[1] - cur[0])
            if fits and not too_loose:
                return
        self._y_range = want
        self.plot.setYRange(*want, padding=0)

    def _save(self):
        if self._last is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save spectrum", str(self.data_dir / "spectrum.csv"),
                                              "CSV (*.csv)")
        if path:
            pd.DataFrame({"wavelength_nm": self._last[0], "intensity": self._last[1]}).to_csv(path, index=False)
            self.log_message.emit(f"Spectrum saved to {path}")
