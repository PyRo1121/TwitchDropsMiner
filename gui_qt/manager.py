"""Qt backend adapter and presentation composition root.

``QtGUIManager`` implements the backend GUI contract while focused modules own
widget construction, dashboard state, async enrichment, and runtime teardown.
Construction builds an inactive widget tree; ``start`` and ``stop`` are the
symmetric boundary for timers, logging, tray resources, and owned tasks.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from PySide6.QtCore import QPropertyAnimation, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QWidget

from constants import State, _resource_path as resource_path
from game_metadata import SteamMetadataProvider
from session_history import HistoryEvent
from translate import _
from utils import cancel_tasks

from .controllers import (
    QtDashboardController,
    QtNotificationController,
    QtStatusController,
)
from .game_context import QtGameContextController
from .image_cache import QtImageCache
from .pages import ChannelsPage
from .shell import QtWindowShell
from .subs import (
    QtCampaignProgress,
    QtChannelList,
    QtConsole,
    QtHelp,
    QtInventory,
    QtLoginForm,
    QtSettings,
    QtStatusBar,
    QtWebsocketStatus,
)
from .tasks import (
    QtPresentationRuntime,
    QtTaskRegistry,
)
from .theme import apply_theme, make_theme
from .tray import QtTray
from .widgets import Badge

if TYPE_CHECKING:
    from game import Game
    from inventory import TimedDrop
    from twitch import Twitch

_T = TypeVar("_T")
logger = logging.getLogger("TwitchDrops")


class QtGUIManager(QMainWindow):
    """Public Qt implementation of the backend's GUI port."""

    def __init__(self, twitch: Twitch) -> None:
        super().__init__()
        self._twitch = twitch
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("QtGUIManager requires an existing QApplication")
        self._app = cast(QApplication, app)
        self._app.setQuitOnLastWindowClosed(False)
        self._close_requested = asyncio.Event()
        self._closing = False
        self._theme_dark = bool(twitch.settings.dark_mode)
        self._theme = make_theme(self._theme_dark)
        self._tasks = QtTaskRegistry()
        self._image_cache = QtImageCache(twitch, tasks=self._tasks)
        self._steam_metadata = SteamMetadataProvider(twitch)

        self._app.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft
            if _.current == "العربية"
            else Qt.LayoutDirection.LeftToRight
        )
        self.setWindowTitle(_("gui", "text", "app_name"))
        brand_icon = QIcon(str(resource_path("gui_qt/assets/drop_deck_brand.png")))
        if brand_icon.isNull():
            brand_icon = QIcon(str(resource_path("icons/pickaxe.ico")))
        self.setWindowIcon(brand_icon)
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)

        self._shell = QtWindowShell(
            self,
            twitch.settings,
            self._theme,
            can_interact=lambda: self.accepting_actions,
            clear_history=self._clear_history,
            reload_inventory=self._reload_inventory,
            switch_channel=self._switch_channel,
        )
        self._expose_shell_contract()
        self._wire_backend()

        self._dashboard = QtDashboardController(
            twitch,
            self.channels,
            self.hero,
            campaign_metric=self.campaign_metric,
            drop_metric=self.drop_metric,
            watch_metric=self.watch_metric,
            live_metric=self.live_metric,
            campaigns_metric=self.campaigns_metric,
            claimed_metric=self.claimed_metric,
            parent=self,
        )
        self._metrics_timer = self._dashboard.timer
        self._status_controller = QtStatusController(
            (self._health, self._sidebar_status, self._topbar_status),
            self._signal_pulse,
            self.diagnostic_label,
            lambda: self._theme,
        )
        self.tray = QtTray(
            self,
            close=self.close,
            notifications_enabled=lambda: bool(
                getattr(self._twitch.settings, "tray_notifications", True)
            ),
        )
        self._notification_controller = QtNotificationController(
            self.tray,
            self.diagnostic_label,
            self.print,
        )
        # Retain the named center for tests and diagnostics that inspect active
        # notification policy, while the controller owns rendering decisions.
        self._notifications = self._notification_controller.center
        self._game_context = QtGameContextController(
            self.hero,
            self._image_cache,
            self._steam_metadata,
            self._tasks,
        )
        self._tray_requested = bool(
            twitch.settings.tray or twitch.settings.autostart_tray
        )
        self._runtime = QtPresentationRuntime(
            self,
            tasks=self._tasks,
            dashboard=self._dashboard,
            progress=self.progress,
            inventory_page=self.inventory_page,
            game_context=self._game_context,
            shell=self._shell,
            tray=self.tray,
            metadata=self._steam_metadata,
            image_cache=self._image_cache,
            console=self.output,
            tray_requested=self._tray_requested,
            apply_application_theme=self._apply_application_theme,
        )
        self._log_handler = self._runtime.log_handler

        self._apply_ring_theme()
        self._refresh_semantic_colors()
        self._status_changed(_("gui", "status", "idle"))
        if logger.getEffectiveLevel() < logging.ERROR:
            self.print(
                _("gui", "text", "logging_level").format(
                    level=logging.getLevelName(logger.getEffectiveLevel())
                )
            )

    def _expose_shell_contract(self) -> None:
        """Expose the widgets consumed by the established GUI adapters."""
        shell = self._shell
        self.stack = shell.stack
        self.pages = shell.pages
        self._nav_buttons = shell.nav_buttons
        self._command = shell.command
        self._page_context = shell.page_context
        self._sidebar_status = shell.sidebar_status
        self._topbar_status = shell.topbar_status
        self._health = shell.health
        self._signal_pulse = shell.signal_pulse
        self.status_label = shell.status_label
        self.websocket_label = shell.websocket_label
        self.diagnostic_label = shell.diagnostic_label
        self.login_panel = shell.login_panel
        self.hero = shell.hero
        self.campaign_metric = shell.campaign_metric
        self.drop_metric = shell.drop_metric
        self.watch_metric = shell.watch_metric
        self.live_metric = shell.live_metric
        self.campaigns_metric = shell.campaigns_metric
        self.claimed_metric = shell.claimed_metric
        self.overview_activity = shell.overview_activity
        self.full_activity = shell.full_activity
        self.inventory_page = shell.inventory_page
        self._history_page = shell.history_page
        self.settings_page = shell.settings_page
        self.help_page = shell.help_page

    def _wire_backend(self) -> None:
        self.status = QtStatusBar(self.status_label, self._status_changed)
        self.websockets = QtWebsocketStatus(self.websocket_label)
        self.login = QtLoginForm(self.login_panel, self)
        self.progress = QtCampaignProgress(self.hero)
        self.output = QtConsole(
            [self.overview_activity.log, self.full_activity.log]
        )
        channels_page = cast(ChannelsPage, self.pages["channels"])
        self.channels = QtChannelList(
            channels_page,
            on_watching=self._on_channel_watching,
            on_cleared=self._on_channel_cleared,
            on_changed=self._refresh_dashboard_metrics,
        )
        self.inventory = QtInventory(self.inventory_page, self._image_cache)
        # ``inv`` is the backend contract's canonical inventory attribute.
        self.inv = self.inventory
        self.settings = QtSettings(self, self.settings_page)
        self.help = QtHelp(self, self.help_page)

    @property
    def _page_animation(self) -> QPropertyAnimation | None:
        return self._shell.page_animation

    @property
    def _animation_page(self) -> QWidget | None:
        return self._shell.animation_page

    def _on_channel_watching(self, channel: Any) -> None:
        self._dashboard.channel_watching(channel)

    def _on_channel_cleared(self) -> None:
        self._dashboard.channel_cleared()

    def _refresh_dashboard_metrics(self) -> None:
        self._dashboard.refresh()

    def _status_changed(self, text: str) -> None:
        self._status_controller.update(text)

    def _focus_command(self) -> None:
        self._shell.focus_command()

    def _submit_command(self) -> None:
        self._shell.submit_command()

    def _navigate(self, key: str) -> None:
        self._shell.navigate(key)

    def _reload_inventory(self) -> None:
        if not self.accepting_actions:
            return
        self._twitch.change_state(State.INVENTORY_FETCH)
        self.print(_("gui", "text", "reload_requested"))

    def _switch_channel(self) -> None:
        if self.accepting_actions:
            self._twitch.change_state(State.CHANNEL_SWITCH)

    @property
    def accepting_actions(self) -> bool:
        runtime = getattr(self, "_runtime", None)
        return (
            self._tasks.accepting
            if runtime is None
            else runtime.accepting_actions
        )

    @property
    def running(self) -> bool:
        return self._runtime.started

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self) -> None:
        await self._close_requested.wait()

    async def coro_unless_closed(self, awaitable: Awaitable[_T]) -> _T:
        from exceptions import ExitRequest

        work_task = asyncio.ensure_future(awaitable)
        close_task = asyncio.create_task(self._close_requested.wait())
        try:
            done, _ = await asyncio.wait(
                (work_task, close_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work_task in done and not work_task.cancelled():
                exception = work_task.exception()
                if exception is not None:
                    raise exception
            if close_task in done:
                raise ExitRequest()
            return await work_task
        finally:
            await cancel_tasks((work_task, close_task))

    def prevent_close(self) -> None:
        self._close_requested.clear()

    def start(self) -> None:
        self._runtime.start()

    async def stop(self) -> None:
        await self._runtime.stop()

    def close(self) -> bool:
        self._close_requested.set()
        self._twitch.close()
        return True

    def closeEvent(self, event: Any) -> None:
        if not self._closing:
            self.close()
        event.accept()

    def close_window(self) -> None:
        self._closing = True
        try:
            self._runtime.close_sync()
            super().close()
        finally:
            self._closing = False

    def save(self, *, force: bool = False) -> None:
        self._runtime.save(force=force)

    def grab_attention(self, *, sound: bool = True) -> None:
        if not self.accepting_actions:
            return
        self.tray.restore()
        if sound:
            QApplication.beep()

    def set_authenticated(self, authenticated: bool) -> None:
        if self.accepting_actions:
            self.help.set_authenticated(authenticated)

    def set_games(self, games: set[Game]) -> None:
        if self.accepting_actions:
            self.settings.set_games(games)

    def display_drop(
        self,
        drop: TimedDrop,
        *,
        countdown: bool = True,
        subone: bool = False,
    ) -> None:
        if not self.accepting_actions:
            return
        self.progress.display(drop, countdown=countdown, subone=subone)
        self._game_context.display(drop)
        self.tray.update_title(drop)

    def clear_drop(self) -> None:
        if not self.accepting_actions:
            return
        self.progress.display(None)
        self._game_context.clear()
        self.tray.update_title(None)

    def print(self, message: str) -> None:
        self.output.print(message)

    def _clear_history(self) -> None:
        if not self.accepting_actions:
            return
        answer = QMessageBox.question(
            self,
            "Clear completed history",
            "Remove completed session records while keeping the current session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        history = getattr(self._twitch, "history", None)
        if history is not None:
            history.clear(keep_current=True)
            self.history_changed()

    def history_changed(self) -> None:
        history = getattr(self._twitch, "history", None)
        if history is not None:
            self._history_page.set_sessions(history.sessions)

    def on_history_event(self, event: HistoryEvent) -> None:
        self._notification_controller.handle(event)

    def _apply_ring_theme(self) -> None:
        palette = self._theme.p
        self.hero.ring.set_colors(
            track=palette.surface3,
            progress=palette.accent,
            text=palette.text,
            caption=palette.muted,
        )
        self.hero.set_backdrop_color(palette.bg)

    def _refresh_semantic_colors(self) -> None:
        palette = self._theme.p
        for badge in self.findChildren(Badge):
            badge.apply_palette(palette)

    def _apply_application_theme(self) -> None:
        apply_theme(self._app, self._theme)
        self._shell.apply_theme(self._theme)
        self._apply_ring_theme()
        self._refresh_semantic_colors()
        self._status_controller.refresh_theme()

    def apply_theme(self, dark: bool) -> None:
        self._theme_dark = dark
        self._theme = make_theme(dark)
        self._apply_application_theme()
        self.inventory.refresh()
