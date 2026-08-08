"""Qt presentation layer for TwitchDropsMiner.

This module owns windows, pages, and toolkit state. Twitch networking remains
in ``twitch.py``; the backend talks to this object through the existing GUI
callback contract.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import TYPE_CHECKING, Any, cast

import qtawesome as qta
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, QSize, Qt
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from translate import _
from constants import OUTPUT_FORMATTER, State, _resource_path as resource_path
from game_metadata import SteamMetadata, SteamMetadataProvider
from utils import format_duration
from .image_cache import QtImageCache
from .pages import ActivityPage, ChannelsPage, HeroCard, HistoryPage, InventoryPage, LoginPanel
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
from .theme import apply_theme, make_theme
from .tasks import QtTaskRegistry
from .tray import QtTray
from .widgets import Card, IconButton, Metric, SignalPulse, StatusDot

if TYPE_CHECKING:
    from inventory import TimedDrop
    from twitch import Twitch
    from game import Game

logger = logging.getLogger("TwitchDrops")


class _QtLogHandler(logging.Handler):
    def __init__(self, console: QtConsole) -> None:
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        self._console.print(self.format(record))


class QtGUIManager(QMainWindow):
    def __init__(self, twitch: Twitch) -> None:
        super().__init__()
        self._twitch = twitch
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("QtGUIManager requires an existing QApplication")
        self._app: QApplication = cast(QApplication, app)
        self._app.setQuitOnLastWindowClosed(False)
        self._close_requested = asyncio.Event()
        self._closing = False
        self._started = False
        self._theme_dark = bool(twitch.settings.dark_mode)
        self._theme = make_theme(self._theme_dark)
        self._tasks = QtTaskRegistry()
        self._image_cache = QtImageCache(twitch, tasks=self._tasks)
        self._steam_metadata = SteamMetadataProvider(twitch)
        self._metadata_task: asyncio.Task[Any] | None = None
        self._metadata_generation = 0
        self._context_key: tuple[str, str] | None = None
        self._watching_id: int | None = None
        self._watching_since: float | None = None
        self._watched_seconds = 0.0
        self.watch_metric: Metric
        self.live_metric: Metric
        self.campaigns_metric: Metric
        self.claimed_metric: Metric
        self.diagnostic_label: QLabel
        apply_theme(self._app, self._theme)
        self._app.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft
            if _.current == "العربية"
            else Qt.LayoutDirection.LeftToRight
        )
        self.setWindowTitle("Twitch Drops Miner")
        brand_icon = QIcon(str(resource_path("gui_qt/assets/drop_deck_brand.png")))
        if brand_icon.isNull():
            brand_icon = QIcon(str(resource_path("icons/pickaxe.ico")))
        self.setWindowIcon(brand_icon)
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        workspace = QWidget()
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        workspace_layout.addWidget(self._build_topbar())
        self.stack = QStackedWidget()
        workspace_layout.addWidget(self.stack, 1)
        root.addWidget(workspace, 1)
        self.setCentralWidget(central)
        self._page_animation: QPropertyAnimation | None = None
        self._command_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._command_shortcut.activated.connect(self._focus_command)

        self.pages: dict[str, QWidget] = {}
        self._build_pages()
        self._wire_backend()
        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(1000)
        self._metrics_timer.timeout.connect(self._refresh_dashboard_metrics)
        self._metrics_timer.start()
        self._refresh_dashboard_metrics()
        self._log_handler = _QtLogHandler(self.output)
        self._log_handler.setFormatter(OUTPUT_FORMATTER)
        logger.addHandler(self._log_handler)
        if logger.getEffectiveLevel() < logging.ERROR:
            self.print(f"Logging level: {logging.getLevelName(logger.getEffectiveLevel())}")
        self.tray = QtTray(self, self)
        self._tray_requested = bool(
            twitch.settings.tray or twitch.settings.autostart_tray
        )
        self.tray.start()
        if self._tray_requested and self.tray.available and sys.platform != "darwin":
            self.hide()
        else:
            # A tray-only launch must still expose a window when the platform
            # has no system tray (for example, a minimal desktop or CI).
            self.show()
        self._navigate("overview")

    def _on_channel_watching(self, channel: Any) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id != self._watching_id:
            self._on_channel_cleared()
            self._watching_id = channel_id
            self._watching_since = time.monotonic()
        self.hero.set_channel(getattr(channel, "name", None), getattr(channel, "viewers", None))

    def _on_channel_cleared(self) -> None:
        if self._watching_since is not None:
            self._watched_seconds += time.monotonic() - self._watching_since
        self._watching_id = None
        self._watching_since = None
        self.hero.set_channel(None)

    def _refresh_dashboard_metrics(self) -> None:
        watched = self._watched_seconds
        if self._watching_since is not None:
            watched += time.monotonic() - self._watching_since
        self.watch_metric.set_value(format_duration(watched))
        monitored, live = self.channels.counts()
        self.live_metric.set_value(f"{live}/{monitored}")
        campaigns = getattr(self._twitch, "inventory", [])
        self.campaigns_metric.set_value(str(len(campaigns)))
        claimed = sum(getattr(campaign, "claimed_drops", 0) for campaign in campaigns)
        self.claimed_metric.set_value(str(claimed))

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 18, 12, 14)
        layout.setSpacing(6)

        brand = QFrame()
        brand.setObjectName("brand")
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(4, 0, 4, 18)
        brand_layout.setSpacing(7)
        mark = QLabel()
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(52, 52)
        brand_symbol = QPixmap(str(resource_path("gui_qt/assets/drop_deck_brand.png")))
        if brand_symbol.isNull():
            mark.setPixmap(qta.icon("ph.broadcast-tower-fill", color="#0b0d14").pixmap(QSize(24, 24)))
        else:
            mark.setPixmap(
                brand_symbol.scaled(
                    QSize(44, 44),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        name = QLabel("TDM")
        name.setObjectName("brandName")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_layout.addWidget(name)
        sub = QLabel("DROP DECK")
        sub.setObjectName("brandSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_layout.addWidget(sub)
        layout.addWidget(brand)

        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_icons = {
            "overview": "ph.house-fill",
            "channels": "ph.broadcast-fill",
            "drops": "ph.drop-fill",
            "activity": "ph.activity",
            "history": "ph.clock-counter-clockwise",
            "settings": "ph.sliders-horizontal-fill",
            "help": "ph.question-fill",
        }
        navigation = (
            ("overview", "Overview"),
            ("channels", _("gui", "channels", "name")),
            ("drops", "Drops"),
            ("history", "History"),
            ("settings", _("gui", "tabs", "settings")),
            ("help", f'{_("gui", "tabs", "help")} && About'),
        )
        for key, label in navigation:
            button = QPushButton(label)
            button.setObjectName("nav")
            button.setIconSize(QSize(18, 18))
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(label.replace("&&", "&"))
            button.clicked.connect(lambda _=False, value=key: self._navigate(value))
            self._nav_buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)
        self._sidebar_status = StatusDot("#a1a9bd", "Starting")
        layout.addWidget(self._sidebar_status)
        return sidebar

    def _build_topbar(self) -> QFrame:
        topbar = QFrame()
        topbar.setObjectName("topbar")
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(26, 14, 26, 14)
        layout.setSpacing(12)
        context = QVBoxLayout()
        context.setSpacing(1)
        kicker = QLabel("CONTROL CENTER")
        kicker.setObjectName("pageKicker")
        context.addWidget(kicker)
        self._page_context = QLabel("Drop deck")
        self._page_context.setObjectName("pageTitle")
        context.addWidget(self._page_context)
        layout.addLayout(context)
        layout.addStretch(1)

        command = QLineEdit()
        command.setObjectName("command")
        command.setPlaceholderText("Search  ·  Ctrl K")
        command.setClearButtonEnabled(True)
        command.setMaximumWidth(310)
        command.returnPressed.connect(self._submit_command)
        completer = QCompleter(
            ["Overview", "Channels", "Drops", "History", "Settings", "Help", "Event log"],
            command,
        )
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.activated.connect(lambda _value: self._submit_command())
        command.setCompleter(completer)
        self._command = command
        layout.addWidget(command)

        status_frame = QFrame()
        status_frame.setObjectName("statusChip")
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(10, 2, 10, 2)
        self._topbar_status = StatusDot("#a1a9bd", "Idle")
        status_layout.addWidget(self._topbar_status)
        layout.addWidget(status_frame)

        account = IconButton("ph.user-circle-fill", "Account", color="#a1a9bd")
        account.setFixedSize(36, 36)
        account.clicked.connect(lambda: self._navigate("settings"))
        layout.addWidget(account)
        return topbar

    def _focus_command(self) -> None:
        self._command.setFocus()
        self._command.selectAll()

    def _submit_command(self) -> None:
        query = self._command.text().strip().lower()
        aliases = {
            "home": "overview",
            "overview": "overview",
            "channels": "channels",
            "drops": "drops",
            "inventory": "drops",
            "activity": "activity",
            "event log": "activity",
            "history": "history",
            "session history": "history",
            "settings": "settings",
            "help": "help",
            "about": "help",
        }
        target = aliases.get(query)
        if target is not None:
            self._navigate(target)
            self._command.clear()
        else:
            self._command.selectAll()

    def _build_pages(self) -> None:
        overview = QWidget()
        overview.setObjectName("page")
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(28, 24, 28, 24)
        overview_layout.setSpacing(16)

        intro = QHBoxLayout()
        intro.setSpacing(18)
        title = QVBoxLayout()
        title.setSpacing(3)
        eyebrow = QLabel("OVERVIEW  /  LIVE CONTROL")
        eyebrow.setObjectName("pageKicker")
        title.addWidget(eyebrow)
        title_label = QLabel("Drop deck")
        title_label.setObjectName("h1")
        title.addWidget(title_label)
        subtitle = QLabel("The quiet control surface for everything you are farming.")
        subtitle.setObjectName("muted")
        title.addWidget(subtitle)
        intro.addLayout(title)
        intro.addStretch(1)
        self._health = StatusDot("#a1a9bd", "Starting")
        intro.addWidget(self._health, 0, Qt.AlignmentFlag.AlignTop)
        overview_layout.addLayout(intro)

        self.status_label = QLabel("Starting")
        self.status_label.setObjectName("muted")
        self.websocket_label = QLabel()
        self.websocket_label.setVisible(False)
        signal_strip = QFrame()
        signal_strip.setObjectName("metricStrip")
        signal_layout = QHBoxLayout(signal_strip)
        signal_layout.setContentsMargins(0, 10, 0, 0)
        signal_layout.setSpacing(26)
        signal_layout.addWidget(self.status_label)
        hint = QLabel("Claims run automatically while the miner is live.")
        hint.setObjectName("subtle")
        signal_layout.addWidget(hint)
        signal_layout.addStretch(1)
        signal_layout.addWidget(QLabel("Ctrl K  ·  jump anywhere"), 0, Qt.AlignmentFlag.AlignRight)
        overview_layout.addWidget(signal_strip)

        diagnostic = QFrame()
        diagnostic.setObjectName("statusRibbon")
        diagnostic_layout = QHBoxLayout(diagnostic)
        diagnostic_layout.setContentsMargins(12, 8, 12, 8)
        diagnostic_layout.setSpacing(10)
        self._signal_pulse = SignalPulse()
        diagnostic_layout.addWidget(self._signal_pulse)
        diagnostic_tag = QLabel("STATE READOUT")
        diagnostic_tag.setObjectName("eyebrow")
        diagnostic_layout.addWidget(diagnostic_tag)
        self.diagnostic_label = QLabel("Warming up the drop deck…")
        self.diagnostic_label.setObjectName("muted")
        self.diagnostic_label.setWordWrap(True)
        diagnostic_layout.addWidget(self.diagnostic_label, 1)
        overview_layout.addWidget(diagnostic)

        self.login_panel = LoginPanel()
        overview_layout.addWidget(self.login_panel)

        self.hero = HeroCard()
        overview_layout.addWidget(self.hero)

        metric_rail = QFrame()
        metric_rail.setObjectName("metricRail")
        metric_layout = QHBoxLayout(metric_rail)
        metric_layout.setContentsMargins(0, 8, 0, 8)
        metric_layout.setSpacing(0)
        self.campaign_metric = Metric("Campaign", "0.0%")
        self.drop_metric = Metric("Drop", "—")
        self.watch_metric = Metric("Session watch", "00:00:00")
        self.live_metric = Metric("Live channels", "0/0")
        self.campaigns_metric = Metric("Campaigns", "0")
        self.claimed_metric = Metric("Claimed drops", "0")
        for metric in (
            self.campaign_metric,
            self.drop_metric,
            self.watch_metric,
            self.live_metric,
            self.campaigns_metric,
            self.claimed_metric,
        ):
            metric_layout.addWidget(metric, 1)
        overview_layout.addWidget(metric_rail)

        self.overview_activity = ActivityPage(compact=True)
        quick_actions = Card()
        quick_actions.setObjectName("quickCard")
        quick_body = quick_actions.body()
        quick_header = QHBoxLayout()
        quick_title = QLabel("QUICK STEERING")
        quick_title.setObjectName("eyebrow")
        quick_header.addWidget(quick_title)
        quick_header.addStretch(1)
        quick_header.addWidget(QLabel("Keep the miner moving"), 0, Qt.AlignmentFlag.AlignRight)
        quick_body.addLayout(quick_header)
        actions = QHBoxLayout()
        for label, target, icon in (
            ("View channels", "channels", "ph.broadcast-fill"),
            ("Browse drops", "drops", "ph.drop-fill"),
            ("Open preferences", "settings", "ph.sliders-horizontal-fill"),
        ):
            action = QPushButton(label)
            action.setObjectName("ghost")
            action.setIcon(qta.icon(icon, color="#b69cff"))
            action.clicked.connect(lambda _=False, value=target: self._navigate(value))
            actions.addWidget(action)
        actions.addStretch(1)
        quick_body.addLayout(actions)
        overview_layout.addWidget(quick_actions)
        overview_layout.addStretch(1)
        overview_scroll = QScrollArea()
        overview_scroll.setObjectName("pageScroll")
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        overview_scroll.setWidget(overview)
        self.pages["overview"] = overview_scroll
        self._add_page("overview", overview_scroll)

        channels = ChannelsPage()
        self.pages["channels"] = channels
        self._add_page("channels", channels)

        self.inventory_page = InventoryPage(self._twitch.settings)
        self.pages["drops"] = self.inventory_page
        self.pages["inventory"] = self.inventory_page
        self._add_page("drops", self.inventory_page)

        activity = ActivityPage()
        self.pages["activity"] = activity
        self._add_page("activity", activity)

        history = HistoryPage()
        self.pages["history"] = history
        self._history_page = history
        self._add_page("history", history)

        settings = QWidget()
        self.pages["settings"] = settings
        self._add_page("settings", settings)

        help_page = QWidget()
        self.pages["help"] = help_page
        self._add_page("help", help_page)

        self.full_activity = activity
        self.settings_page = settings
        self.help_page = help_page

    def _add_page(self, key: str, page: QWidget) -> None:
        if self.stack.indexOf(page) < 0:
            self.stack.addWidget(page)

    def _wire_backend(self) -> None:
        self.status = QtStatusBar(self.status_label, self._status_changed)
        self.websockets = QtWebsocketStatus(self.websocket_label)
        self.login = QtLoginForm(self.login_panel, self)
        self.progress = QtCampaignProgress(self.hero)
        self._apply_ring_theme()
        self._status_widgets = (self._health, self._sidebar_status, self._topbar_status)
        self.hero.campaign_changed.connect(self._hero_changed)
        self.output = QtConsole([self.overview_activity.log, self.full_activity.log])
        channels_page = cast(ChannelsPage, self.pages["channels"])
        self.channels = QtChannelList(
            channels_page,
            on_watching=self._on_channel_watching,
            on_cleared=self._on_channel_cleared,
            on_changed=self._refresh_dashboard_metrics,
        )
        channels_page.set_switch_callback(self._twitch.state_change(State.CHANNEL_SWITCH))
        self.inventory = QtInventory(self.inventory_page, self._image_cache)
        # Preserve the backend's established attribute name: twitch.py and
        # inventory.py call self.gui.inv.* directly.
        self.inv = self.inventory
        self.settings = QtSettings(self, self.settings_page)
        self.help = QtHelp(self, self.help_page)
        self.inventory_page.set_refresh_callback(self._reload_inventory)

    def _hero_changed(self, progress: float, campaign: str, drop_percent: str, drop_rewards: str) -> None:
        self.campaign_metric.set_value(f"{progress:.1%}")
        self.campaign_metric.setToolTip(campaign)
        self.drop_metric.set_value(drop_percent)
        self.drop_metric.setToolTip(drop_rewards)
        self._refresh_dashboard_metrics()

    @staticmethod
    def _metadata_text(metadata: SteamMetadata) -> str:
        if metadata.error:
            return "GAME INTEL  ·  Steam signal unavailable  ·  search links ready"
        if not metadata.available:
            return "GAME INTEL  ·  no exact Steam match  ·  search links ready"
        parts: list[str] = []
        if metadata.players is not None:
            parts.append(f"{metadata.players:,} playing")
        if metadata.price is not None:
            parts.append(f"{metadata.price} US")
        elif metadata.free_to_play:
            parts.append("Free to play")
        if metadata.discount_percent:
            parts.append(f"-{metadata.discount_percent}%")
        if not parts:
            parts.append("Steam listing found")
        return "STEAM INTEL  ·  " + "  ·  ".join(parts)

    def _queue_game_context(self, drop: TimedDrop) -> None:
        campaign = drop.campaign
        game = campaign.game
        game_name = str(game.name)
        image_url = str(getattr(campaign, "image_url", ""))
        context_key = (game_name, image_url)
        if context_key == self._context_key:
            return
        self._context_key = context_key
        self._metadata_generation += 1
        self._cancel_metadata_task()
        baseline = SteamMetadata(game_name)
        slug = str(getattr(game, "slug", ""))
        twitch_url = f"https://www.twitch.tv/directory/category/{slug}" if slug else ""
        self.hero.set_links(
            steam=baseline.store_url,
            steamdb=baseline.steamdb_url,
            twitch=twitch_url,
        )
        self.hero.set_intel("GAME INTEL  ·  looking up Steam signal…")
        generation = self._metadata_generation
        self._metadata_task = self._tasks.create(
            self._load_game_context(game_name, image_url, twitch_url, generation)
        )

    def _cancel_metadata_task(self) -> None:
        if self._metadata_task is not None:
            self._metadata_task.cancel()
            self._metadata_task = None

    async def _load_game_context(
        self, game_name: str, image_url: str, twitch_url: str, generation: int
    ) -> None:
        try:
            image_task = (
                self._image_cache.get(image_url, (128, 172))
                if image_url
                else asyncio.sleep(0, result=None)
            )
            image, metadata = await asyncio.gather(
                image_task,
                self._steam_metadata.get(game_name),
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            logger.debug("Game context unavailable: %s", type(exc).__name__)
            return
        if generation != self._metadata_generation:
            return
        if image is not None:
            self.hero.set_art(image)
        self.hero.set_links(
            steam=metadata.store_url,
            steamdb=metadata.steamdb_url,
            twitch=twitch_url,
        )
        self.hero.set_intel(self._metadata_text(metadata))

    @staticmethod
    def _status_explanation(text: str) -> str:
        lowered = text.lower()
        if any(term in lowered for term in ("error", "failed", "captcha", "terminated")):
            return "The miner needs attention. Open Event log for the exact request or login detail."
        if any(term in lowered for term in ("gather", "fetch", "cleanup", "reload")):
            return "Scanning campaign inventory and live channels. This usually clears in a few seconds."
        if any(term in lowered for term in ("switch", "goes online", "goes offline")):
            return "Re-evaluating the watch queue so the next minute lands on the best eligible channel."
        if any(term in lowered for term in ("no available", "no active", "idle")):
            return "Nothing eligible is live right now. The miner is standing by and will resume automatically."
        if any(term in lowered for term in ("watch", "active", "connected")):
            return "A live channel is selected; Twitch watch minutes are being sent automatically."
        return "The deck is connected and waiting for the next backend signal."

    def _status_changed(self, text: str) -> None:
        lowered = text.lower()

        def translated_prefix(*path: str) -> str:
            value = _("status", *path)
            return value.split("{", 1)[0].strip().lower()

        error_terms = (
            "error",
            "failed",
            "captcha",
            "terminated",
            translated_prefix("terminated"),
        )
        warning_terms = (
            "maint",
            "reload",
            "gather",
            "switch",
            *(
                translated_prefix(key)
                for key in (
                    "no_channel",
                    "no_campaign",
                    "goes_online",
                    "goes_offline",
                )
            ),
        )
        active_terms = (
            "watch",
            "active",
            "connected",
            translated_prefix("watching"),
        )
        palette = self._theme.p
        if any(value and value in lowered for value in error_terms):
            color = palette.error
        elif any(value and value in lowered for value in warning_terms):
            color = palette.amber
        elif any(value and value in lowered for value in active_terms):
            color = palette.green
        else:
            color = palette.idle
        display = text or _("gui", "status", "idle")
        compact = "Live" if color == palette.green else "Error" if color == palette.error else "Attention" if color == palette.amber else "Idle"
        for widget in self._status_widgets:
            widget.set_state(color, compact)
            widget.setToolTip(display)
        self._signal_pulse.set_state(color, color != palette.idle)
        self.diagnostic_label.setText(self._status_explanation(display))

    def _reload_inventory(self) -> None:
        self._twitch.change_state(State.INVENTORY_FETCH)
        self.print("Campaign reload requested.")

    def _navigate(self, key: str) -> None:
        self._active_page = key
        titles = {
            "overview": "Drop deck",
            "channels": "Live channels",
            "drops": "Drops",
            "inventory": "Drops",
            "activity": "Event log",
            "history": "Session history",
            "settings": "Preferences",
            "help": "Help & about",
        }
        palette = self._theme.p
        for name, button in self._nav_buttons.items():
            active = name == key
            button.setChecked(active)
            button.setIcon(qta.icon(self._nav_icons[name], color=palette.accent if active else palette.muted))
        self._page_context.setText(titles.get(key, "Drop deck"))
        page = self.pages["drops"] if key == "inventory" else self.pages[key]
        self.stack.setCurrentWidget(page)
        effect = QGraphicsOpacityEffect(page)
        effect.setOpacity(0.92)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(140)
        animation.setStartValue(0.92)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: page.setGraphicsEffect(cast(Any, None)))
        self._page_animation = animation
        animation.start()
        if key in ("drops", "inventory"):
            self.inventory.refresh()

    @property
    def running(self) -> bool:
        return self._started

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self) -> None:
        await self._close_requested.wait()

    async def coro_unless_closed(self, awaitable: Any) -> Any:
        from exceptions import ExitRequest

        tasks = [
            asyncio.ensure_future(awaitable),
            asyncio.ensure_future(self._close_requested.wait()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._close_requested.is_set():
            raise ExitRequest()
        return await next(iter(done))

    def prevent_close(self) -> None:
        self._close_requested.clear()

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        self.progress.stop_timer()
        self._cancel_metadata_task()
        self._tasks.cancel_all()
        self._steam_metadata.save()
        self._image_cache.save()

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
            self._metrics_timer.stop()
            self._cancel_metadata_task()
            self._tasks.cancel_all()
            logger.removeHandler(self._log_handler)
            self.tray.stop()
            try:
                self._steam_metadata.save(force=True)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("Unable to save Steam metadata: %s", type(exc).__name__)
            try:
                self._image_cache.save(force=True)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("Unable to save image cache: %s", type(exc).__name__)
            super().close()
        finally:
            self._closing = False

    def save(self, *, force: bool = False) -> None:
        self._steam_metadata.save(force=force)
        self._image_cache.save(force=force)

    def grab_attention(self, *, sound: bool = True) -> None:
        self.tray.restore()
        if sound:
            QApplication.beep()

    def set_games(self, games: set[Game]) -> None:
        self.settings.set_games(games)

    def display_drop(self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False) -> None:
        self.progress.display(drop, countdown=countdown, subone=subone)
        self._queue_game_context(drop)
        self.tray.update_title(drop)

    def clear_drop(self) -> None:
        self.progress.display(None)
        self._context_key = None
        self._metadata_generation += 1
        self._cancel_metadata_task()
        self.hero.clear()
        self.tray.update_title(None)

    def print(self, message: str) -> None:
        self.output.print(message)

    def history_changed(self) -> None:
        history_page = getattr(self, "_history_page", None)
        history = getattr(self._twitch, "history", None)
        if history_page is not None and history is not None:
            history_page.set_sessions(history.sessions)

    def _apply_ring_theme(self) -> None:
        palette = self._theme.p
        self.hero.ring.set_colors(
            track=palette.surface3,
            progress=palette.accent,
            text=palette.text,
            caption=palette.muted,
        )
        self.hero.set_backdrop_color(palette.bg)

    def apply_theme(self, dark: bool) -> None:
        self._theme_dark = dark
        self._theme = make_theme(dark)
        apply_theme(self._app, self._theme)
        palette = self._theme.p
        if hasattr(self, "hero"):
            self._apply_ring_theme()
        active = getattr(self, "_active_page", "overview")
        for name, button in self._nav_buttons.items():
            button.setIcon(qta.icon(self._nav_icons[name], color=palette.accent if name == active else palette.muted))
        self.inventory.refresh()
