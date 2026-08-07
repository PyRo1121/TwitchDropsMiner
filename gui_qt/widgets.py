"""Reusable, theme-aware Qt widgets for the TwitchDropsMiner Qt UI."""
from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .theme import Theme


class Card(QFrame):
    """Layered surface card (QS# #card)."""

    def __init__(self, parent: QWidget | None = None, *, elevated: bool = False):
        super().__init__(parent)
        self.setObjectName("surface2" if elevated else "card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(16, 14, 16, 14)
        self._lay.setSpacing(8)

    def body(self) -> QVBoxLayout:
        return self._lay


class IconButton(QPushButton):
    """Compact icon action with an accessible label and tooltip."""

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        parent: QWidget | None = None,
        *,
        color: str = "#b69cff",
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_color = color
        self.setIcon(qta.icon(icon_name, color=color))
        self.setIconSize(QSize(20, 20))
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("iconButton")

    def set_icon_color(self, color: str) -> None:
        self._icon_color = color
        self.setIcon(qta.icon(self._icon_name, color=color))


class ProgressRing(QWidget):
    """Minimal progress ring used for the live farming hero."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._label = "0%"
        self._caption = "campaign"
        self._track_color = "#292d42"
        self._progress_color = "#b69cff"
        self._text_color = "#eef1f8"
        self._caption_color = "#a1a9bd"
        self.setFixedSize(146, 146)

    def set_colors(
        self, *, track: str, progress: str, text: str, caption: str
    ) -> None:
        self._track_color = track
        self._progress_color = progress
        self._text_color = text
        self._caption_color = caption
        self.update()

    def set_progress(self, value: float, label: str, caption: str = "campaign") -> None:
        self._value = max(0.0, min(1.0, value))
        self._label = label
        self._caption = caption
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(13, 13, self.width() - 26, self.height() - 26)
        track = QPen(QColor(self._track_color), 10)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(bounds, 90 * 16, -360 * 16)
        progress = QPen(QColor(self._progress_color), 10)
        progress.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(progress)
        painter.drawArc(bounds, 90 * 16, round(-360 * self._value * 16))
        painter.setPen(QColor(self._text_color))
        painter.setFont(painter.font())
        font = painter.font()
        font.setPointSize(18)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect().adjusted(0, -5, 0, 8), Qt.AlignmentFlag.AlignCenter, self._label)
        font.setPointSize(8)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(self._caption_color))
        painter.drawText(self.rect().adjusted(0, 34, 0, 0), Qt.AlignmentFlag.AlignCenter, self._caption.upper())


class StatusDot(QWidget):
    """Color + always-paired text label (never color-only signal)."""

    def __init__(self, color: str, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)
        self._set_dot_color(color)
        self._label = QLabel(text)
        self._label.setObjectName("muted")
        lay.addWidget(self._dot)
        lay.addWidget(self._label)
        lay.addStretch(1)

    def _set_dot_color(self, color: str) -> None:
        self._dot.setStyleSheet(f"background:{color}; border-radius:4px;")

    def set_state(self, color: str, text: str) -> None:
        self._set_dot_color(color)
        self._label.setText(text)
        self._label.setToolTip(text)


class Badge(QLabel):
    """Uppercase pill with semantic tint."""

    def __init__(self, text: str, fg: str, bg: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border-radius:0;"
            " font-size:9px; font-weight:700; letter-spacing:1px; padding:2px 8px;"
        )
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)


class Progress(QProgressBar):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setValue(0)
        self.setTextVisible(False)
        self.setFixedHeight(6)


class SignalPulse(QWidget):
    """Compact telemetry glyph used in the status readout."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = "#a1a9bd"
        self._active = False
        self.setFixedSize(58, 18)

    def set_state(self, color: str, active: bool) -> None:
        self._color = color
        self._active = active
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self._color)
        if not self._active:
            color.setAlpha(105)
        painter.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        heights = (2, 4, 7, 4, 9, 5, 3, 6, 2)
        for index, height in enumerate(heights):
            x = 3 + index * 6
            painter.drawLine(x, 9 - height // 2, x, 9 + height // 2)
        painter.end()


class SegmentedProgress(QWidget):
    """A quiet segmented track for hero progress instead of a generic bar."""

    def __init__(self, parent: QWidget | None = None, *, segments: int = 18):
        super().__init__(parent)
        self._value = 0.0
        self._segments = max(4, segments)
        self.setFixedHeight(9)

    def set_value(self, value: float) -> None:
        self._value = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gap = 4
        width = max(1, (self.width() - gap * (self._segments - 1)) / self._segments)
        active = round(self._segments * self._value)
        for index in range(self._segments):
            x = round(index * (width + gap))
            color = "#b69cff" if index < active else "#292d42"
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, 1, round(width), 7, 2, 2)


class Metric(QWidget):
    """Stat tile: muted micro label over a strong value."""

    def __init__(self, label: str, value: str, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        cap = QLabel(label.upper())
        cap.setObjectName("subtle")
        cap.setStyleSheet("font-size:8pt; letter-spacing:1px;")
        self._value = QLabel(value)
        self._value.setStyleSheet("font-size:15pt; font-weight:700;")
        lay.addWidget(cap)
        lay.addWidget(self._value)
        lay.addStretch(1)

    def set_value(self, value: str) -> None:
        self._value.setText(value)


class SectionTitle(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("h2")


class PageIntro(QWidget):
    """Consistent editorial header for secondary pages."""

    def __init__(self, kicker: str, title: str, subtitle: str, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        label = QLabel(kicker)
        label.setObjectName("pageKicker")
        layout.addWidget(label)
        heading = QLabel(title)
        heading.setObjectName("h1")
        layout.addWidget(heading)
        description = QLabel(subtitle)
        description.setObjectName("muted")
        layout.addWidget(description)


class EmptyState(QWidget):
    action = Signal()

    def __init__(self, title: str, desc: str, cta: str, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addStretch(1)
        t = QLabel(title)
        t.setObjectName("h2")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = QLabel(desc)
        d.setObjectName("muted")
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d.setWordWrap(True)
        d.setMaximumWidth(420)
        but = QPushButton(cta)
        but.setObjectName("primary")
        but.setCursor(Qt.CursorShape.PointingHandCursor)
        but.clicked.connect(self.action.emit)
        lay.addWidget(t, 1, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(d, 1, Qt.AlignmentFlag.AlignHCenter)
        box = QHBoxLayout()
        box.addStretch(1)
        box.addWidget(but)
        box.addStretch(1)
        lay.addLayout(box)
        lay.addStretch(1)
