"""Handle macOS installations whose Qt plugins carry the Finder hidden flag."""
import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile
from PySide6.QtCore import QFileInfo, QLibraryInfo


def prepare_qt():
    if sys.platform != 'darwin' or os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH'):
        return
    plugins = Path(QLibraryInfo.path(QLibraryInfo.PluginsPath)) / 'platforms'
    files = list(plugins.glob('*.dylib'))
    if files and any(QFileInfo(str(p)).isHidden() for p in files):
        # Qt skips hidden plugins. Copy bytes without macOS flags; do not alter the installation.
        cache = tempfile.TemporaryDirectory(prefix='spectral-qt-')
        for p in files:
            shutil.copyfile(p, Path(cache.name) / p.name)
        os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = cache.name
        atexit.register(cache.cleanup)
