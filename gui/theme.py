"""Visual theme: colors, application stylesheet, line icons and plot defaults."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Dark colors suit the dim lighting used during imaging.
BG, SURFACE, RAISED, BORDER = "#0d1117", "#161b22", "#21262d", "#30363d"
TEXT, MUTED, FAINT = "#e6edf3", "#8b949e", "#6e7681"
ACCENT, OK, WARN, BAD = "#2f81f7", "#3fb950", "#d29922", "#f85149"
STATE_COLORS = {"ok": OK, "sim": WARN, "busy": WARN, "bad": BAD, "off": FAINT}

STYLE = """
QWidget { background:#0d1117; color:#e6edf3; font-size:13px; }
QLabel, QCheckBox, QRadioButton { background:transparent; }
QWidget#sideInner, QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; }
QToolTip { background:#21262d; color:#e6edf3; border:1px solid #30363d; padding:4px 6px; }

/* Cards: title sits inside the card instead of on its border */
QGroupBox { background:#161b22; border:1px solid #262c36; border-radius:10px; margin-top:0;
  padding:36px 12px 12px 12px; font-weight:600; }
QGroupBox::title { subcontrol-origin:padding; subcontrol-position:top left; left:12px; top:12px;
  padding:0; color:#e6edf3; font-size:13px; }
QGroupBox::indicator { width:15px; height:15px; }
QGroupBox QWidget { background:transparent; }
QGroupBox QLineEdit, QGroupBox QAbstractSpinBox, QGroupBox QComboBox, QGroupBox QPlainTextEdit,
QGroupBox QListWidget, QGroupBox QPushButton { background:#0d1117; }
QGroupBox { font-weight:600; }
QGroupBox QLabel, QGroupBox QCheckBox { font-weight:400; }

QPushButton { background:#21262d; border:1px solid #30363d; border-radius:7px; padding:7px 14px; min-height:16px; }
QPushButton:hover { background:#30363d; border-color:#8b949e; }
QPushButton:pressed { background:#161b22; }
QPushButton:focus { border-color:#2f81f7; }
QPushButton:disabled, QGroupBox QPushButton:disabled { color:#6e7681; background:#161b22; border-color:#21262d; }
QPushButton#primary, QGroupBox QPushButton#primary { background:#238636; border-color:#2ea043; color:white; font-weight:600; }
QPushButton#primary:hover { background:#2ea043; }
QPushButton#primary:disabled { background:#1b2b1f; border-color:#21262d; color:#6e7681; }
QPushButton#accent, QGroupBox QPushButton#accent { background:#1f6feb; border-color:#2f81f7; color:white; font-weight:600; }
QPushButton#accent:hover { background:#2f81f7; }
QPushButton#accent:disabled { background:#1c2a44; border-color:#21262d; color:#6e7681; }
QPushButton#danger, QGroupBox QPushButton#danger { background:#8e1519; border-color:#da3633; color:white; font-weight:600; }
QPushButton#danger:hover { background:#b62324; }
QPushButton#danger:disabled { background:#2a1517; border-color:#21262d; color:#6e7681; }
QPushButton#motion, QGroupBox QPushButton#motion { border-color:#9e6a03; }
QPushButton#motion:disabled { border-color:#21262d; }
QPushButton:checked, QGroupBox QPushButton:checked, QPushButton#accent:checked { background:#8e1519;
  border-color:#da3633; color:white; }

QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTableWidget {
  background:#0d1117; border:1px solid #30363d; border-radius:7px; padding:5px 7px;
  selection-background-color:#1f6feb; }
QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover { border-color:#484f58; }
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus,
QTableWidget:focus { border-color:#2f81f7; }
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled { color:#6e7681; border-color:#21262d; }
QComboBox QAbstractItemView { background:#161b22; border:1px solid #30363d; selection-background-color:#1f6feb; }
QCheckBox { spacing:8px; }
QPlainTextEdit#console { font-family:Menlo, Consolas, monospace; font-size:12px; border:none; }
QHeaderView::section { background:#161b22; color:#8b949e; padding:6px; border:none; border-bottom:1px solid #30363d; }
QTableWidget { gridline-color:#21262d; alternate-background-color:#161b22; border-radius:10px; }
QGraphicsView { border:1px solid #262c36; border-radius:10px; }

QTabWidget::pane { border:none; }
QTabBar::tab { background:transparent; color:#8b949e; padding:8px 14px; border:none;
  border-bottom:2px solid transparent; }
QTabBar::tab:hover { color:#e6edf3; }
QTabBar::tab:selected { color:#e6edf3; border-bottom:2px solid #f78166; font-weight:600; }

QScrollBar:vertical { background:transparent; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#30363d; border-radius:5px; min-height:30px; }
QScrollBar::handle:vertical:hover { background:#484f58; }
QScrollBar:horizontal { background:transparent; height:10px; margin:0; }
QScrollBar::handle:horizontal { background:#30363d; border-radius:5px; min-width:30px; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }

QLabel#statusOk { color:#3fb950; font-weight:600; }
QLabel#statusBad { color:#f85149; font-weight:600; }
QLabel#statusBusy { color:#d29922; font-weight:600; }
QLabel#statusIdle { color:#8b949e; font-weight:600; }
QLabel#bigReadout { font-family:Menlo, Consolas, monospace; font-size:30px; font-weight:600; color:#58a6ff; }
QLabel#readout, QLabel#summary { font-family:Menlo, Consolas, monospace; }
QLabel#hint { color:#8b949e; font-size:12px; font-weight:400; }
QLabel#metric { font-family:Menlo, Consolas, monospace; background:#161b22; border:1px solid #262c36;
  border-radius:7px; padding:5px 10px; }
QLabel#metric[state="warn"] { color:#f85149; border-color:#da3633; }
QLabel#placeholder { color:#6e7681; font-size:15px; background:#161b22; border:1px dashed #30363d; border-radius:10px; }

QProgressBar { border:1px solid #30363d; border-radius:7px; text-align:center; background:#0d1117; min-height:20px; }
QProgressBar::chunk { background:#1f6feb; border-radius:6px; }

/* Navigation rail */
QFrame#sidebar { background:#010409; border-right:1px solid #21262d; }
QFrame#sidebar QToolButton { background:transparent; border:none; border-radius:8px; color:#8b949e;
  padding:8px 2px; font-size:11px; }
QFrame#sidebar QToolButton:hover { background:#161b22; color:#e6edf3; }
QFrame#sidebar QToolButton:checked { background:#1c2129; color:#e6edf3; font-weight:600; }
QFrame#sidebar QToolButton#simToggle:checked { background:#3b2e05; color:#d29922; }
QFrame#sidebar QToolButton:focus { border:1px solid #2f81f7; }
QLabel#brand { color:#e6edf3; font-size:12px; font-weight:800; padding:4px 0; }

/* Page header */
QFrame#pageHeader { background:transparent; border-bottom:1px solid #21262d; }
QLabel#pageTitle { font-size:20px; font-weight:700; }
QLabel#pill { border-radius:11px; padding:3px 12px; font-weight:600; font-size:12px;
  background:#161b22; border:1px solid #30363d; color:#8b949e; }
QLabel#pill[state="ok"] { color:#3fb950; border-color:#238636; background:#0f2417; }
QLabel#pill[state="sim"], QLabel#pill[state="busy"] { color:#d29922; border-color:#9e6a03; background:#2a2105; }

QDockWidget { color:#8b949e; }
QDockWidget::title { background:#0d1117; padding:6px 10px; border-top:1px solid #21262d; }
QStatusBar { background:#010409; border-top:1px solid #21262d; color:#8b949e; }
QStatusBar QLabel { padding:0 8px; }
QMenuBar { background:#010409; }
QMenuBar::item:selected, QMenu::item:selected { background:#1f6feb; }
QMenu { background:#161b22; border:1px solid #30363d; }
QSplitter::handle { background:#0d1117; }
QSplitter::handle:hover { background:#30363d; }
"""

# 24x24 stroke icons (Lucide-style outlines, drawn for this app).
_ICONS = {
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z"/>'
              '<circle cx="12" cy="13" r="3.5"/>',
    "mono": '<path d="M10 4 3 19h14z"/><path d="M1 12.5l5.5-.5"/><path d="M14 10l7-3M14.6 12h7.4M15.2 14l6.3 3"/>',
    "scan": '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/>',
    "hdx": '<path d="M3 20h18"/><path d="M3 17c3 0 4-1 5.5-6S10.5 4 12 4s2 2 3.5 7 2.5 6 5.5 6"/>',
    "log": '<path d="M4 6h16M4 12h16M4 18h10"/>',
    "sim": '<path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3"/><path d="M7 15h10"/>',
    "help": '<circle cx="12" cy="12" r="9.5"/><path d="M9.2 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4"/><path d="M12 17.5h.01"/>',
}


def _pixmap(name: str, color: str, size: int, dot: str | None) -> QPixmap:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    scale = 2  # sharp on high-DPI screens
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF(0, 0, size * scale, size * scale))
    if dot is not None:  # device status badge, top right
        r = size * scale * 0.18
        c = size * scale - r - 1
        p.setPen(QColor("#010409"))
        p.setBrush(QColor(dot))
        p.drawEllipse(QRectF(c - r, r * 0.2, 2 * r, 2 * r))
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name: str, dot: str | None = None, size: int = 22) -> QIcon:
    """Muted outline normally, bright when the button is checked; optional colored status dot."""
    ic = QIcon()
    ic.addPixmap(_pixmap(name, MUTED, size, dot), QIcon.Normal, QIcon.Off)
    ic.addPixmap(_pixmap(name, TEXT, size, dot), QIcon.Normal, QIcon.On)
    ic.addPixmap(_pixmap(name, TEXT, size, dot), QIcon.Active, QIcon.Off)
    ic.addPixmap(_pixmap(name, FAINT, size, dot), QIcon.Disabled, QIcon.Off)
    return ic


def apply_plot_defaults():
    """Set plot colors before creating any PlotWidgets."""
    pg.setConfigOptions(antialias=True, background=SURFACE, foreground=MUTED)
