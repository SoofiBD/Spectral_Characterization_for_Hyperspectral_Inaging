"""Shared input widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import (QAbstractSpinBox, QComboBox, QDoubleSpinBox, QLabel, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

SIDE_WIDTH = 340  # control column width shared by all device tabs


def set_role(widget: QWidget, name: str):
    """Switch a widget's stylesheet role (objectName) and re-apply the style."""
    widget.setObjectName(name)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_state(widget: QWidget, state: str):
    """Set the dynamic `state` property used by the stylesheet (ok / sim / off / busy / warn)."""
    widget.setProperty("state", state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def side_column(*widgets: QWidget, header: QWidget | None = None) -> QWidget:
    """Fixed-width control column: optional always-visible header above a scrolling stack of cards."""
    inner = QWidget()
    inner.setObjectName("sideInner")
    lay = QVBoxLayout(inner)
    lay.setContentsMargins(0, 0, 4, 0)
    lay.setSpacing(10)
    for w in widgets:
        lay.addWidget(w)
        # Wide value ranges inflate the minimum size hint; let the column decide the width instead.
        for field in w.findChildren(QAbstractSpinBox) + w.findChildren(QComboBox):
            field.setMinimumWidth(70)
    lay.addStretch(1)
    scroll = QScrollArea()
    scroll.setWidget(inner)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    column = QWidget()
    column.setFixedWidth(SIDE_WIDTH)
    col = QVBoxLayout(column)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(10)
    if header is not None:
        col.addWidget(header)
    col.addWidget(scroll, 1)
    return column


def hint(text: str) -> QLabel:
    """Secondary explanatory text."""
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def _setup(box: QAbstractSpinBox):
    # Out-of-range input is clamped instead of silently reverting to the old value
    box.setCorrectionMode(QAbstractSpinBox.CorrectToNearestValue)
    box.setKeyboardTracking(False)  # emit valueChanged once editing is finished


class NumberSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that accepts both '.' and ',' as decimal separator (independent of the
    system locale), clamps out-of-range values and selects its text when focused."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _setup(self)

    def _normalize(self, text: str) -> str:
        return text.replace(",", ".")

    def validate(self, text: str, pos: int):
        t = self._normalize(text)
        for affix in (self.prefix(), self.suffix()):
            if affix:
                t = t.replace(affix.strip(), "")
        t = t.strip()
        if t in ("", "-", "+", ".", "-.", "+."):
            return QValidator.Intermediate, text, pos
        try:
            float(t)
        except ValueError:
            return QValidator.Invalid, text, pos
        # Allow partial numbers while typing; clamp to the range when editing finishes.
        return QValidator.Acceptable if self.minimum() <= float(t) <= self.maximum() \
            else QValidator.Intermediate, text, pos

    def valueFromText(self, text: str) -> float:
        t = self._normalize(text)
        for affix in (self.prefix(), self.suffix()):
            if affix:
                t = t.replace(affix.strip(), "")
        try:
            return float(t.strip())
        except ValueError:
            return self.value()

    def textFromValue(self, value: float) -> str:
        return f"{value:.{self.decimals()}f}"

    def fixup(self, text: str) -> str:
        v = min(max(self.valueFromText(text), self.minimum()), self.maximum())
        return self.textFromValue(v)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


class IntSpinBox(QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _setup(self)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


def committed(box: QAbstractSpinBox):
    """Value of one spin box including text typed but not yet confirmed."""
    commit_spinboxes(box)
    return box.value()


def commit_spinboxes(root: QWidget):
    """Commit pending spin-box edits before a button handler reads their values."""
    boxes = root.findChildren(QAbstractSpinBox)
    if isinstance(root, QAbstractSpinBox):
        boxes.append(root)
    for box in boxes:
        if not isinstance(box, (QDoubleSpinBox, QSpinBox)):
            box.interpretText()
            continue
        try:
            v = box.valueFromText(box.cleanText())
        except (ValueError, TypeError):
            continue
        box.setValue(min(max(v, box.minimum()), box.maximum()))
