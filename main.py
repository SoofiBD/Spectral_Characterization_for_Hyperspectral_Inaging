"""Spectral Characterization Suite - entry point.

    python main.py              # hardware mode (simulation can be toggled in the toolbar)
    python main.py --simulate   # start with simulated devices
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from gui.qt_bootstrap import prepare_qt
from devices.contracts import driver_factory


def main() -> int:
    parser = argparse.ArgumentParser(description="Spectral Characterization Suite")
    parser.add_argument("--simulate", action="store_true", help="start in simulation mode")
    parser.add_argument("--backend", choices=["native", "pyseabreeze", "cseabreeze"], default="native",
                        help="spectrometer driver: native HDX USB driver (default) or a seabreeze backend")
    parser.add_argument("--spectrometer-driver", default="devices.ocean_hdx:OceanHDXController",
                        help="trusted installed Python module:factory implementing the spectrometer controller")
    parser.add_argument("--camera-driver", default="devices.asi_camera:ASICameraClient",
                        help="trusted installed Python module:factory implementing RAW16 camera acquisition")
    args = parser.parse_args()

    prepare_qt()
    app = QApplication(sys.argv)
    app.setApplicationName("Spectral Characterization Suite")
    app.setStyle("Fusion")
    win = MainWindow(BASE_DIR, simulate=args.simulate, backend=args.backend,
                     spectrometer_factory=driver_factory(args.spectrometer_driver),
                     camera_factory=driver_factory(args.camera_driver),
                     settings=QSettings("HSI", "SpectralCharacterization"))
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
