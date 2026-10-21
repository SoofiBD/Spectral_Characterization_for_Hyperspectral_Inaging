"""Camera tab: standalone ZWO ASI connection, live RAW16 preview and explicit raw frame recording.

The camera is the primary device. This tab needs neither the HDX nor the monochromator. One
ASICameraClient (one asi_camd process) is the only owner of the camera: the live preview uses it,
and a scan borrows it only after the preview thread has finished its last exposure.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from acquisition.camera_measurement import record_camera_frame
from devices.asi_camera import ASICameraClient, CameraError
from gui.widgets import IntSpinBox, NumberSpinBox, commit_spinboxes, hint, set_role, set_state, side_column
from gui.workers import run_task

PREVIEW_PREFIX = "hsi_camera_preview_"  # per-connection temp folder, removed on disconnect/close
LIVE_TEXT = {False: "▶  Start Live", True: "■  Stop Live"}


class CameraLiveThread(QThread):
    """Preview loop: EXPOSE into the temp folder -> load -> delete. Only the newest frame is kept
    (the GUI pulls it on a timer), so neither memory nor disk use can grow while previewing."""

    error = Signal(str)
    applied = Signal(dict)
    saved = Signal(str)
    save_failed = Signal(str)

    def __init__(self, client, preview_dir: Path, settings: tuple, parent=None):
        super().__init__(parent)
        self.client, self.preview_dir = client, preview_dir
        self._running = True
        self._lock = threading.Lock()
        self._latest = None
        self._settings = settings  # applied before the first preview frame
        self._save_root: Path | None = None
        self.frames = 0

    def stop(self):
        self._running = False

    def request_settings(self, settings: tuple):
        with self._lock:
            self._settings = settings

    def request_save(self, root: Path):
        with self._lock:
            self._save_root = root

    def take_latest(self):
        with self._lock:
            frame, self._latest = self._latest, None
        return frame

    def run(self):
        while self._running:
            with self._lock:
                settings, self._settings = self._settings, None
                save, self._save_root = self._save_root, None
            try:
                if settings is not None:
                    self.applied.emit(self.client.configure(*settings))
                if save is not None:
                    try:
                        session = record_camera_frame(self.client, save)
                    except CameraError:
                        raise
                    except Exception as exc:  # e.g. disk full: preview continues, nothing is overwritten
                        self.save_failed.emit(str(exc))
                        continue
                    meta = session.manifest["points"][0]["camera"]
                    frame = np.load(save / meta["file"], allow_pickle=False)
                    self.saved.emit(str(save))
                else:
                    path = self.preview_dir / f"preview_{self.frames:06d}.npy"
                    try:
                        meta = self.client.expose(path)
                        frame = np.load(path, allow_pickle=False)
                    finally:
                        path.unlink(missing_ok=True)
                        Path(str(path) + ".tmp").unlink(missing_ok=True)
                    if frame.dtype != np.dtype("uint16") or frame.ndim != 2:
                        raise CameraError("Camera returned an invalid RAW16 frame")
            except Exception as exc:
                if self._running:  # an abort requested by stop/close is not an error
                    self.error.emit(str(exc))
                return
            self.frames += 1
            with self._lock:
                self._latest = (frame, meta)


class CameraHandover:
    """Lend the camera client to a scan after the preview thread has stopped."""

    def __init__(self, threads: list[QThread], client):
        self.threads, self.client = threads, client

    def acquire(self, aborted) -> ASICameraClient | None:
        for thread in self.threads:
            while not thread.wait(100):
                if aborted():
                    return None
        return self.client if self.client is not None and self.client.is_running else None


class CameraPanel(QWidget):
    log_message = Signal(str)
    connection_changed = Signal(bool)

    def __init__(self, simulation_enabled, data_dir: Path, log_dir: Path,
                 camera_factory=ASICameraClient, parent=None):
        super().__init__(parent)
        self.simulation_enabled = simulation_enabled
        self.data_dir, self.log_dir = data_dir, log_dir
        self.camera_factory = camera_factory
        self.client: ASICameraClient | None = None
        self.preview_dir: Path | None = None
        self.run_dir: Path | None = None
        self._live: CameraLiveThread | None = None
        self._stopping: set[CameraLiveThread] = set()
        self._busy = False
        self._external_busy = False
        self._shown, self._fps_clock, self.fps = 0, time.monotonic(), 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._draw_latest)
        self._build_ui()
        self.sync_simulation(bool(simulation_enabled()))
        self._update_state()

    def _build_ui(self):
        conn = QGroupBox("Camera")
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Real camera (asi_camd)", "Simulated camera"])
        self.index_spin = IntSpinBox(minimum=0, maximum=63, value=0)
        self.index_spin.setToolTip("ZWO camera index when more than one camera is attached")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("accent")
        self.info_lbl = QLabel("Not connected")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setObjectName("statusIdle")
        cf = QFormLayout(conn)
        cf.addRow("Source:", self.source_combo)
        cf.addRow("Index:", self.index_spin)
        cf.addRow(self.connect_btn)
        cf.addRow(self.info_lbl)

        cfg = QGroupBox("Exposure (RAW16)")
        self.exp_spin = NumberSpinBox(decimals=3, minimum=0.001, maximum=3_600_000, value=100, suffix=" ms")
        # Wide input ranges: the camera SDK decides the real limits and reports the applied values.
        self.gain_spin = IntSpinBox(minimum=-10000, maximum=100000, value=100)
        self.offset_spin = IntSpinBox(minimum=-32768, maximum=65535, value=50)
        self.bin_spin = IntSpinBox(minimum=1, maximum=16, value=1)
        self.roi_spins = [IntSpinBox(minimum=0, maximum=100000, value=0) for _ in range(4)]
        self.apply_btn = QPushButton("Apply Settings")
        self.apply_btn.setToolTip("Send the settings to the camera now. During live view changes apply automatically.")
        self.applied_lbl = hint("Applied: —")
        sf = QFormLayout(cfg)
        sf.addRow("Exposure:", self.exp_spin)
        sf.addRow("Gain:", self.gain_spin)
        sf.addRow("Offset:", self.offset_spin)
        sf.addRow("Binning:", self.bin_spin)

        rg = QGridLayout()
        for i, (label, widget) in enumerate(zip(("X", "Y", "W", "H"), self.roi_spins)):
            rg.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            rg.addWidget(widget, i // 2, (i % 2) * 2 + 1)
        rg.setColumnStretch(1, 1)
        rg.setColumnStretch(3, 1)
        sf.addRow("ROI:", rg)
        sf.addRow(hint("ROI width/height 0 = full sensor. During live view exposure, gain, offset and "
                       "binning apply automatically; ROI needs Apply Settings."))
        sf.addRow(self.apply_btn)
        sf.addRow(self.applied_lbl)

        self.live_btn = QPushButton(LIVE_TEXT[False], checkable=True)
        self.live_btn.setObjectName("accent")
        self.live_btn.setToolTip("Start or stop the live preview (F5). Preview frames are temporary.")
        self.save_btn = QPushButton("●  Save Raw Frame")
        self.save_btn.setToolTip("Records the next RAW16 frame with the applied settings into "
                                 "data/camera_frame_<time>/ (Ctrl+S)")
        self.folder_btn = QPushButton("Open Saved Folder")
        self.auto_chk = QCheckBox("Auto contrast", checked=True)
        self.auto_chk.setToolTip("Display scaling only; the recorded RAW16 data is never changed")
        bar = QHBoxLayout()
        for w in (self.live_btn, self.save_btn, self.folder_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.auto_chk)

        self.stats_lbl = QLabel("—")
        self.stats_lbl.setObjectName("metric")
        self.sat_lbl = QLabel("Saturated: —")
        self.sat_lbl.setObjectName("metric")
        self.sat_lbl.setToolTip("Pixels at the camera's maximum value; their intensities are clipped")
        self.saved_lbl = hint("Live frames are temporary; only Save Raw Frame writes data.")
        self.stats_lbl.hide()  # shown with the first frame
        self.sat_lbl.hide()
        stats = QHBoxLayout()
        stats.addWidget(self.stats_lbl)
        stats.addWidget(self.sat_lbl)
        stats.addWidget(self.saved_lbl, 1)

        self.view = pg.PlotWidget()
        self.view.setAspectLocked(True)
        self.view.invertY(True)
        self.image = pg.ImageItem(axisOrder="row-major")
        self.image.setAutoDownsample(True)
        self.view.addItem(self.image)
        self.placeholder = QLabel()
        self.placeholder.setObjectName("placeholder")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.view)

        right = QVBoxLayout()
        right.addLayout(bar)
        right.addLayout(stats)
        right.addWidget(self.stack, 1)
        root = QHBoxLayout(self)
        root.addWidget(side_column(conn, cfg))
        root.addLayout(right, 1)

        self.connect_btn.clicked.connect(self._toggle_connect)
        self.apply_btn.clicked.connect(self._apply)
        self.live_btn.toggled.connect(self._toggle_live)
        self.save_btn.clicked.connect(self.save_frame)
        self.folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_dir))))
        # ROI is left out: its four fields are edited one at a time and an intermediate ROI could be
        # rejected by the camera, which would end the live view. ROI changes need Apply Settings.
        for w in (self.exp_spin, self.gain_spin, self.offset_spin, self.bin_spin):
            w.valueChanged.connect(self._auto_apply)
        for key, button in (("F5", self.live_btn), ("Ctrl+S", self.save_btn)):
            QShortcut(QKeySequence(key), self, lambda b=button: b.isVisible() and b.isEnabled() and b.click())

    def _auto_apply(self, *_):
        """During live view a changed value is applied before the next frame (no Apply click needed)."""
        if self._live is not None:
            self._live.request_settings(self.settings_tuple())

    @property
    def busy(self) -> bool:
        """True while the camera is in use here (live, stopping or a background command)."""
        return self._live is not None or bool(self._stopping) or self._busy

    def sync_simulation(self, on: bool):
        if self.client is None:
            self.source_combo.setCurrentIndex(1 if on else 0)

    def settings_tuple(self) -> tuple:
        commit_spinboxes(self)
        return (round(self.exp_spin.value() * 1000), self.gain_spin.value(), self.offset_spin.value(),
                self.bin_spin.value(), tuple(w.value() for w in self.roi_spins))

    def scan_settings(self) -> dict:
        """Camera parameters for a scan (same values as the live view)."""
        exp, gain, offset, binning, roi = self.settings_tuple()
        simulate = self.client.simulated if self.client is not None else self.source_combo.currentIndex() == 1
        return dict(index=self.index_spin.value(), exposure_us=exp, gain=gain, offset=offset, bin=binning,
                    roi=list(roi), simulate=simulate)

    def _update_state(self):
        c = self.client is not None
        idle = not self.busy and not self._external_busy
        self.connect_btn.setText("Disconnect" if c else "Connect")
        self.connect_btn.setEnabled(idle)
        self.source_combo.setEnabled(not c and idle)
        self.index_spin.setEnabled(not c and idle)
        self.apply_btn.setEnabled(c and not self._external_busy and not self._busy and not self._stopping)
        self.live_btn.setEnabled(c and not self._external_busy and not self._busy and not self._stopping)
        self.save_btn.setEnabled(c and not self._external_busy and not self._busy and not self._stopping)
        self.folder_btn.setEnabled(self.run_dir is not None)
        set_role(self.info_lbl, "statusOk" if c else "statusIdle")
        if self.image.image is not None:
            self.stack.setCurrentWidget(self.view)
        else:
            self.placeholder.setText("Press  ▶ Start Live  (F5) to see the camera image" if c else
                                     "Connect a camera to begin\n\nChoose the source on the left, "
                                     "then press Connect")
            self.stack.setCurrentWidget(self.placeholder)

    def set_external_busy(self, busy: bool):
        """A scan owns the camera: stop the preview and lock the controls; on release re-check the client."""
        if busy and self._live is not None:
            self.live_btn.setChecked(False)
        self._external_busy = busy
        if not busy and self.client is not None:
            if not self.client.is_running:
                self._client_lost("Camera process ended during the scan (abort or device loss); reconnect")
            elif self.client.settings:
                self._show_applied(self.client.settings)
        self._update_state()

    def begin_scan_handover(self) -> CameraHandover:
        """GUI thread, at scan start: stop live view; the worker waits for the last exposure."""
        self.set_external_busy(True)
        return CameraHandover(list(self._stopping), self.client)

    def _toggle_connect(self):
        if self.client is not None:
            self.disconnect()
            return
        simulate = self.source_combo.currentIndex() == 1
        index = self.index_spin.value()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"camera_{dt.datetime.now():%Y%m%d_%H%M%S_%f}.log"
        self._busy = True
        self.info_lbl.setText("Connecting…")
        self._update_state()

        def ok(client):
            self.client = client
            self.preview_dir = Path(tempfile.mkdtemp(prefix=PREVIEW_PREFIX))
            i = client.info
            self.info_lbl.setText(f"{i.get('model', '?')} S/N {i.get('serial') or '?'}"
                                  f"{' (SIM)' if client.simulated else ''}\nSDK {i.get('sdk', '?')}, "
                                  f"{i.get('max_width')}×{i.get('max_height')} px, {i.get('bit_depth')} bit, "
                                  f"{i.get('pixel_um')} µm")
            self.connection_changed.emit(True)
            self.log_message.emit(f"Camera connected: {self.info_lbl.text().splitlines()[0]}")

        def fail(msg):
            self.info_lbl.setText(f"Connection failed: {msg}")
            self.log_message.emit(f"Camera connection failed: {msg}")

        def done():
            self._busy = False
            self._update_state()

        run_task(self.camera_factory, simulate=simulate, log_path=log_path, index=index,
                 on_result=ok, on_error=fail, on_finished=done)

    def disconnect(self):
        if self.client is None or self.busy:
            return
        client = self.client
        self._busy = True
        self._update_state()

        def done():
            self._busy = False
            self._release_client()
            self.log_message.emit("Camera disconnected")

        run_task(client.close, on_finished=done)

    def _release_client(self):
        self.client = None
        if self.preview_dir is not None:
            shutil.rmtree(self.preview_dir, ignore_errors=True)
            self.preview_dir = None
        self.info_lbl.setText("Not connected")
        self.applied_lbl.setText("Applied: —")
        self.connection_changed.emit(False)
        self._update_state()

    def _log_hint(self) -> str:
        path = getattr(self.client, "log_path", None)
        return f" (camera log: {path})" if path else ""

    def _client_lost(self, message: str):
        self.log_message.emit(message + self._log_hint())
        client = self.client
        if client is not None:
            client.close()  # process already ended: no QUIT wait; closes pipes and the log
        self._release_client()

    def shutdown(self):
        """Application exit: abort any exposure and end the camera process."""
        self.stop_live(abort=True)
        for thread in list(self._stopping):
            thread.wait(5000)
        if self.client is not None:
            self.client.close()
        self._release_client()

    def _apply(self):
        settings = self.settings_tuple()
        if self._live is not None:
            self._live.request_settings(settings)
            return
        self._busy = True
        self._update_state()
        run_task(self.client.configure, *settings, on_result=self._show_applied,
                 on_error=self._command_failed, on_finished=self._command_done)

    def _show_applied(self, s: dict):
        x, y, w, h = s["roi"]
        self.applied_lbl.setText(f"Applied (camera): {s['exposure_us'] / 1000:g} ms, gain {s['gain']}, "
                                 f"offset {s['offset']}, bin {s['bin']}, ROI {x},{y} {w}×{h}, {s['format']}"
                                 + (f", {s['temperature_c']:.1f} °C" if s.get("temperature_c") is not None else ""))

    def _command_failed(self, msg: str):
        self.log_message.emit(f"Camera: {msg}")
        if self.client is not None and not self.client.is_running:
            self._client_lost("Camera process ended (device disconnected?); reconnect")

    def _command_done(self):
        self._busy = False
        self._update_state()

    def _toggle_live(self, on: bool):
        if on:
            if self._live is not None or self.client is None:
                return
            thread = CameraLiveThread(self.client, self.preview_dir, self.settings_tuple(), self)
            thread.error.connect(lambda m, t=thread: self._on_live_error(t, m))
            thread.applied.connect(self._show_applied)
            thread.saved.connect(self._on_saved)
            thread.save_failed.connect(lambda m: self.log_message.emit(f"Frame not saved: {m}"))
            self._live = thread
            self._shown, self._fps_clock = 0, time.monotonic()
            thread.start()
            self._timer.start()
            self.live_btn.setText(LIVE_TEXT[True])
        else:
            self.stop_live()
            self.live_btn.setText(LIVE_TEXT[False])
        self._update_state()

    def stop_live(self, abort: bool = False):
        """Stop the preview without blocking. abort=True also cancels the running exposure (ends the process)."""
        self._timer.stop()
        thread, self._live = self._live, None
        if thread is None:
            return
        thread.stop()
        if abort and self.client is not None:
            self.client.abort()
        if thread.isRunning():
            self._stopping.add(thread)
            thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        else:
            self._on_thread_finished(thread)
        if self.live_btn.isChecked():
            self.live_btn.blockSignals(True)
            self.live_btn.setChecked(False)
            self.live_btn.setText(LIVE_TEXT[False])
            self.live_btn.blockSignals(False)

    def _on_thread_finished(self, thread):
        self._stopping.discard(thread)
        if self.client is not None and not self.client.is_running and not self._external_busy:
            self._client_lost("Camera process ended; reconnect")
        self._update_state()

    def _on_live_error(self, thread, msg: str):
        self.log_message.emit(f"Camera live view stopped: {msg}{self._log_hint()}")
        if thread is self._live:
            self.live_btn.setChecked(False)

    def _draw_latest(self):
        if self._live is None:
            return
        latest = self._live.take_latest()
        if latest is None:
            return
        frame, meta = latest
        maximum = self.client.info.get("max_intensity", 65535) if self.client is not None else 65535
        if self.auto_chk.isChecked():  # display scaling only: the RAW16 array is never modified
            step = max(1, int(np.sqrt(frame.size / 250_000)))
            lo, hi = np.percentile(frame[::step, ::step], (0.5, 99.5))
            levels = (float(lo), float(hi) if hi > lo else float(lo) + 1)
        else:
            levels = (0, maximum)
        self.image.setImage(frame, autoLevels=False, levels=levels)
        self.stack.setCurrentWidget(self.view)
        self._shown += 1
        elapsed = time.monotonic() - self._fps_clock
        if elapsed >= 1:
            self.fps, self._shown, self._fps_clock = self._shown / elapsed, 0, time.monotonic()
        saturated = int(np.count_nonzero(frame >= maximum))
        self.stats_lbl.setText(f"{frame.shape[1]}×{frame.shape[0]} px · {self.fps:.1f} fps · "
                               f"min {int(frame.min())}  max {int(frame.max())}  mean {frame.mean():.1f}")
        self.sat_lbl.setText(f"Saturated: {saturated} px")
        self.stats_lbl.show()
        self.sat_lbl.show()
        set_state(self.sat_lbl, "warn" if saturated else "")

    def save_frame(self):
        if self.client is None or self._external_busy:
            return
        root = self.data_dir / f"camera_frame_{dt.datetime.now():%Y%m%d_%H%M%S_%f}"
        if self._live is not None:
            self._live.request_save(root)  # the live thread records its next frame there
            self.saved_lbl.setText("Recording the next frame…")
            return
        client, settings = self.client, self.settings_tuple()
        self._busy = True
        self._update_state()

        def go():
            client.configure(*settings)
            return record_camera_frame(client, root)

        def ok(session):
            self._show_applied(client.settings)
            meta = session.manifest["points"][0]["camera"]
            self.image.setImage(np.load(root / meta["file"], allow_pickle=False), autoLevels=True)
            self.stack.setCurrentWidget(self.view)
            self._on_saved(str(root))

        def fail(msg):
            self.saved_lbl.setText(f"Frame not saved: {msg}")
            self._command_failed(msg)

        run_task(go, on_result=ok, on_error=fail, on_finished=self._command_done)

    def _on_saved(self, root: str):
        self.run_dir = Path(root)
        self.saved_lbl.setText(f"Saved {dt.datetime.now():%H:%M:%S}: {Path(root).name}")
        self.saved_lbl.setToolTip(root)
        self.log_message.emit(f"Camera raw frame saved: {root}")
        self._update_state()
