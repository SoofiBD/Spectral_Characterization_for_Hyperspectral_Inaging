"""Spectral Characterization Suite - entry point.

    python main.py              # hardware mode (simulation can be toggled in the toolbar)
    python main.py --simulate   # start with simulated devices
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description="Spectral Characterization Suite")
    parser.add_argument("--simulate", action="store_true", help="start in simulation mode")
    parser.add_argument("--backend", choices=["native", "pyseabreeze", "cseabreeze"], default="native",
                        help="spectrometer driver: native HDX USB driver (default) or a seabreeze backend")
    args = parser.parse_args()

    pg.setConfigOptions(antialias=True, background="#0d1117", foreground="#d0d7de")
    app = QApplication(sys.argv)
    app.setApplicationName("Spectral Characterization Suite")
    app.setStyle("Fusion")
    win = MainWindow(BASE_DIR, simulate=args.simulate, backend=args.backend)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
