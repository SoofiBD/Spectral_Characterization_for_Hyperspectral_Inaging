"""Background workers so hardware I/O never blocks the GUI thread."""

from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, QThread


class _TaskSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Task(QRunnable):
    """Run a blocking callable in the global thread pool and report back via signals."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _TaskSignals()

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # report every failure to the GUI
            traceback.print_exc()
            self.signals.error.emit(str(exc))
        else:
            self.signals.result.emit(res)
        finally:
            self.signals.finished.emit()


_active_tasks: set[Task] = set()  # keep Python wrappers alive until the task finishes


def run_task(fn, *args, on_result=None, on_error=None, on_finished=None, **kwargs) -> Task:
    task = Task(fn, *args, **kwargs)
    task.setAutoDelete(False)
    _active_tasks.add(task)
    task.signals.finished.connect(lambda: _active_tasks.discard(task))
    if on_result:
        task.signals.result.connect(on_result)
    if on_error:
        task.signals.error.connect(on_error)
    if on_finished:
        task.signals.finished.connect(on_finished)
    QThreadPool.globalInstance().start(task)
    return task


class LiveAcquisitionThread(QThread):
    """Continuous spectrum acquisition loop.

    The thread never pushes frames to the GUI. It only keeps the newest spectrum; the GUI pulls
    it with a timer at whatever rate it can draw. Acquisition speed therefore can never fill the
    GUI event queue, and clicks (Stop Live) are handled immediately.
    """

    error = Signal(str)

    def __init__(self, hdx, parent=None):
        super().__init__(parent)
        self.hdx = hdx
        self._running = True
        self._lock = threading.Lock()
        self._latest = None  # (wavelength, intensity) not yet taken by the GUI

    def stop(self):
        self._running = False

    def take_latest(self):
        """Return the newest unseen frame, or None."""
        with self._lock:
            frame, self._latest = self._latest, None
        return frame

    def run(self):
        while self._running:
            try:
                frame = self.hdx.acquire_spectrum()
            except Exception as exc:
                if self._running:
                    self.error.emit(str(exc))
                return
            with self._lock:
                self._latest = frame
            self.msleep(1)
