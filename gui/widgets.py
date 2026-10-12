"""Shared input widgets."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox, QSpinBox, QWidget


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
        # Never "Invalid" for a number: a value outside the range while typing (e.g. "4" on the
        # way to "400") is Intermediate and gets clamped when editing finishes.
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
    """Apply text that is typed but not yet confirmed (no Enter / focus change) in every
    spin box under `root`, so values read right after a button click are what the user sees."""
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
