"""System tray for the Qt UI — QSystemTrayIcon with generated state icons."""
from __future__ import annotations

from collections.abc import Callable
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
    def __init__(
        self,
        parent: Any,
        *,
        close: Callable[[], object],
        notifications_enabled: Callable[[], bool],
    ) -> None:
        self._parent = parent
        self._close = close
        self._notifications_enabled = notifications_enabled
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._active = False
        self._generation = 0
        self._activation_callback: Callable[..., None] | None = None
        self._show_callback: Callable[..., None] | None = None
        self._quit_callback: Callable[..., None] | None = None
        self._icon = QSystemTrayIcon(parent)
        self._icon.setIcon(_state_icon("pickaxe"))
        self._menu = QMenu()
        self._show_action = self._menu.addAction(_("gui", "tray", "show"))
        self._quit_action = self._menu.addAction(_("gui", "tray", "quit"))
        self._icon.setContextMenu(self._menu)
        self._icon.setToolTip("TwitchDropsMiner")

    @property
    def active(self) -> bool:
        return self._active

    def _accepts(self, generation: int | None) -> bool:
        return self._active and (
            generation is None or generation == self._generation
        )

    def _on_activated(self, reason, *, generation: int | None = None) -> None:
        if (
            self._accepts(generation)
            and reason == QSystemTrayIcon.ActivationReason.Trigger
        ):
            self.restore(generation=generation)

    def _on_show_triggered(
        self,
        _checked: bool = False,
        *,
        generation: int,
    ) -> None:
        self.restore(generation=generation)

    def _on_quit_triggered(
        self,
        _checked: bool = False,
        *,
        generation: int,
    ) -> None:
        self.quit(generation=generation)

    def start(self) -> None:
        if self._active:
            return
        self._generation += 1
        generation = self._generation
        self._active = True
        self._activation_callback = lambda reason: self._on_activated(
            reason,
            generation=generation,
        )
        self._show_callback = lambda checked=False: self._on_show_triggered(
            checked,
            generation=generation,
        )
        self._quit_callback = lambda checked=False: self._on_quit_triggered(
            checked,
            generation=generation,
        )
        self._icon.activated.connect(self._activation_callback)
        self._show_action.triggered.connect(self._show_callback)
        self._quit_action.triggered.connect(self._quit_callback)
        if self.available:
            self._icon.show()

    @staticmethod
    def _disconnect(signal, callback: Callable[..., None] | None) -> None:
        if callback is None:
            return
        try:
            signal.disconnect(callback)
        except (RuntimeError, TypeError):
            # A partially failed start may leave only some signals connected.
            pass

    def stop(self) -> None:
        # Deactivate first: queued callbacks retain their activation token and
        # must be stale before signal disconnection or icon teardown begins.
        if self._active:
            self._active = False
            self._generation += 1
        self._disconnect(self._icon.activated, self._activation_callback)
        self._disconnect(self._show_action.triggered, self._show_callback)
        self._disconnect(self._quit_action.triggered, self._quit_callback)
        self._activation_callback = None
        self._show_callback = None
        self._quit_callback = None
        self._icon.hide()

    def restore(self, *, generation: int | None = None) -> None:
        if not self._accepts(generation):
            return
        self._parent.show()
        self._parent.raise_()
        self._parent.activateWindow()

    def quit(self, *, generation: int | None = None) -> None:
        if self._accepts(generation):
            self._close()

    def change_icon(self, state: str) -> None:
        self._icon.setIcon(_state_icon(state))

    def notify(
        self,
        message: str,
        title: str,
        duration: int = 5000,
        *,
        severity: Literal["info", "warning", "error"] = "info",
        generation: int | None = None,
    ) -> bool:
        if not (
            self._accepts(generation)
            and self.available
            and QSystemTrayIcon.supportsMessages()
            and self._notifications_enabled()
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
        self._icon.setToolTip(self.get_title(drop))
