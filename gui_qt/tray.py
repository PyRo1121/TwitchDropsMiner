"""System tray for the Qt UI — QSystemTrayIcon with generated state icons."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from translate import _

from .theme import DARK

if TYPE_CHECKING:
    from inventory import TimedDrop


def _state_icon(state: str) -> QIcon:
    color = {
        "pickaxe": DARK.accent,
        "active": DARK.green,
        "idle": DARK.idle,
        "error": DARK.error,
        "maint": DARK.amber,
    }.get(state, DARK.accent)
    pm = QPixmap(22, 22)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    # pickaxe-ish shape: rounded square + notch
    p.drawRoundedRect(2, 2, 18, 18, 5, 5)
    p.setBrush(QColor("#0a0e16"))
    p.drawRoundedRect(6, 6, 10, 10, 3, 3)
    p.end()
    return QIcon(pm)


class QtTray:
    def __init__(self, manager: Any, parent: Any):
        self._manager = manager
        self._parent = parent
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._icon = QSystemTrayIcon(parent)
        self._icon.setIcon(_state_icon("pickaxe"))
        menu = QMenu()
        show = menu.addAction(_("gui", "tray", "show"))
        show.triggered.connect(self.restore)
        quit_ = menu.addAction(_("gui", "tray", "quit"))
        quit_.triggered.connect(self.quit)
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._on_activated)
        self._icon.setToolTip("TwitchDropsMiner")
        self._shown = False
        self._title = ""

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.restore()

    def start(self) -> None:
        if not self.available:
            return
        self._icon.show()
        self._shown = True

    def stop(self) -> None:
        self._icon.hide()
        self._shown = False

    def restore(self) -> None:
        self._parent.show()
        self._parent.raise_()
        self._parent.activateWindow()

    def quit(self) -> None:
        self._manager.close()

    def change_icon(self, state: str) -> None:
        self._icon.setIcon(_state_icon(state))

    def notify(
        self,
        message: str,
        title: str,
        duration: int = 5000,
        *,
        severity: Literal["info", "warning", "error"] = "info",
    ) -> bool:
        if not (
            self.available
            and QSystemTrayIcon.supportsMessages()
            and getattr(self._manager._twitch.settings, "tray_notifications", True)
        ):
            return False
        message_icon = {
            "info": QSystemTrayIcon.MessageIcon.Information,
            "warning": QSystemTrayIcon.MessageIcon.Warning,
            "error": QSystemTrayIcon.MessageIcon.Critical,
        }[severity]
        self._icon.showMessage(title, message, message_icon, duration)
        return True

    def get_title(self, drop: TimedDrop | None) -> str:
        if drop is None:
            return "TwitchDropsMiner"
        campaign = drop.campaign
        text = f"{campaign.game.name}: {drop.rewards_text()}\n"
        text += f"{drop.progress:6.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        return text

    def update_title(self, drop: TimedDrop | None) -> None:
        title = self.get_title(drop)
        self._title = title
        self._icon.setToolTip(title)
