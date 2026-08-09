"""Qt presentation layer for TwitchDropsMiner.

This module owns windows, pages, and toolkit state. Twitch networking remains
in ``twitch.py``; the backend talks to this object through the existing GUI
callback contract.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, cast

import qtawesome as qta
from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QTimer,
    QSize,
    Qt,
    Signal,
)
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
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from translate import _
from constants import OUTPUT_FORMATTER, State, _resource_path as resource_path
from game_metadata import SteamMetadata, SteamMetadataProvider
from utils import cancel_tasks, format_duration
from notifications import Alert, NotificationCenter
from session_history import HistoryEvent
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
from .widgets import Badge, Card, IconButton, Metric, SignalPulse, StatusDot

if TYPE_CHECKING:
    from inventory import TimedDrop
    from twitch import Twitch
    from game import Game

logger = logging.getLogger("TwitchDrops")


class _QtLogEmitter(QObject):
    message = Signal(str)


class _QtLogHandler(logging.Handler):
    def __init__(self, console: QtConsole) -> None:
        super().__init__()
        self._emitter = _QtLogEmitter()
        self._emitter.message.connect(console.print)

    def emit(self, record: logging.LogRecord) -> None:
        self._emitter.message.emit(self.format(record))


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
        self._notifications = NotificationCenter()
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
        self._current_status_text = _("gui", "status", "idle")
        apply_theme(self._app, self._theme)
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
        self._animation_page: QWidget | None = None
        self._command_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._command_shortcut.activated.connect(self._focus_command)

        self.pages: dict[str, QWidget] = {}
        self._build_pages()
        self._wire_backend()
        self._refresh_semantic_colors()
        self._status_changed(self._current_status_text)
        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(1000)
        self._metrics_timer.timeout.connect(self._refresh_dashboard_metrics)
        self._metrics_timer.start()
        self._refresh_dashboard_metrics()
        self._log_handler = _QtLogHandler(self.output)
        self._log_handler.setFormatter(OUTPUT_FORMATTER)
        logger.addHandler(self._log_handler)
        if logger.getEffectiveLevel() < logging.ERROR:
            self.print(
                _("gui", "text", "logging_level").format(
                    level=logging.getLevelName(logger.getEffectiveLevel())
                )
            )
        self.tray = QtTray(self, self)
        self._tray_requested = bool(
            twitch.settings.tray or twitch.settings.autostart_tray
        )
        self.tray.start()
        if self._tray_requested and self.tray.available:
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
        if self._watching_id is not None:
            channel = getattr(self._twitch, "channels", {}).get(self._watching_id)
            if channel is not None:
                self.hero.set_channel(channel.name, channel.viewers)

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
        name = QLabel(_("gui", "text", "brand_name"))
        name.setObjectName("brandName")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_layout.addWidget(name)
        sub = QLabel(_("gui", "text", "brand_subtitle"))
        sub.setObjectName("brandSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_layout.addWidget(sub)
        layout.addWidget(brand)

        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_icons = {
            "overview": "ph.house-fill",
            "channels": "ph.broadcast-fill",
            "drops": "ph.drop-fill",
            "history": "ph.clock-counter-clockwise",
            "settings": "ph.sliders-horizontal-fill",
            "help": "ph.question-fill",
        }
        navigation = (
            ("overview", _("gui", "text", "overview")),
            ("channels", _("gui", "channels", "name")),
            ("drops", _("gui", "text", "inventory_title")),
            ("history", _("gui", "text", "history")),
            ("settings", _("gui", "tabs", "settings")),
            (
                "help",
                _("gui", "text", "help_about").format(
                    help=_("gui", "tabs", "help")
                ).replace("&", "&&"),
            ),
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
        self._sidebar_status = StatusDot(
            "#a1a9bd",
            _("gui", "text", "starting"),
        )
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
        kicker = QLabel(_("gui", "text", "control_center"))
        kicker.setObjectName("pageKicker")
        context.addWidget(kicker)
        self._page_context = QLabel(_("gui", "text", "deck_title"))
        self._page_context.setObjectName("pageTitle")
        context.addWidget(self._page_context)
        layout.addLayout(context)
        layout.addStretch(1)

        command = QLineEdit()
        command.setObjectName("command")
        command.setPlaceholderText(_("gui", "text", "search_placeholder"))
        command.setClearButtonEnabled(True)
        command.setMaximumWidth(310)
        command.returnPressed.connect(self._submit_command)
        completer = QCompleter(
            [
                _("gui", "text", "overview"),
                _("gui", "channels", "name"),
                _("gui", "text", "inventory_title"),
                _("gui", "text", "history"),
                _("gui", "tabs", "settings"),
                _("gui", "tabs", "help"),
                _("gui", "text", "event_log_command"),
            ],
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
        self._topbar_status = StatusDot(
            "#a1a9bd",
            _("gui", "text", "idle"),
        )
        status_layout.addWidget(self._topbar_status)
        layout.addWidget(status_frame)

        account = IconButton(
            "ph.user-circle-fill",
            _("gui", "text", "account"),
            color="#a1a9bd",
        )
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
        aliases.update(
            {
                _("gui", "text", "overview").lower(): "overview",
                _("gui", "channels", "name").lower(): "channels",
                _("gui", "text", "inventory_title").lower(): "drops",
                _("gui", "text", "event_log_command").lower(): "activity",
                _("gui", "text", "history").lower(): "history",
                _("gui", "text", "history_title").lower(): "history",
                _("gui", "tabs", "settings").lower(): "settings",
                _("gui", "tabs", "help").lower(): "help",
            }
        )
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
        eyebrow = QLabel(_("gui", "text", "overview_kicker"))
        eyebrow.setObjectName("pageKicker")
        title.addWidget(eyebrow)
        title_label = QLabel(_("gui", "text", "deck_title"))
        title_label.setObjectName("h1")
        title.addWidget(title_label)
        subtitle = QLabel(_("gui", "text", "overview_subtitle"))
        subtitle.setObjectName("muted")
        title.addWidget(subtitle)
        intro.addLayout(title)
        intro.addStretch(1)
        self._health = StatusDot(
            "#a1a9bd",
            _("gui", "text", "starting"),
        )
        intro.addWidget(self._health, 0, Qt.AlignmentFlag.AlignTop)
        overview_layout.addLayout(intro)

        self.status_label = QLabel(_("gui", "text", "starting"))
        self.status_label.setObjectName("muted")
        self.websocket_label = QLabel(_("gui", "text", "websocket_waiting"))
        self.websocket_label.setObjectName("subtle")
        signal_strip = QFrame()
        signal_strip.setObjectName("metricStrip")
        signal_layout = QHBoxLayout(signal_strip)
        signal_layout.setContentsMargins(0, 10, 0, 0)
        signal_layout.setSpacing(26)
        signal_layout.addWidget(self.status_label)
        signal_layout.addWidget(self.websocket_label)
        hint = QLabel(_("gui", "text", "claims_automatic"))
        hint.setObjectName("subtle")
        signal_layout.addWidget(hint)
        signal_layout.addStretch(1)
        signal_layout.addWidget(
            QLabel(_("gui", "text", "jump_anywhere")),
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        overview_layout.addWidget(signal_strip)

        diagnostic = QFrame()
        diagnostic.setObjectName("statusRibbon")
        diagnostic_layout = QHBoxLayout(diagnostic)
        diagnostic_layout.setContentsMargins(12, 8, 12, 8)
        diagnostic_layout.setSpacing(10)
        self._signal_pulse = SignalPulse()
        diagnostic_layout.addWidget(self._signal_pulse)
        diagnostic_tag = QLabel(_("gui", "text", "state_readout"))
        diagnostic_tag.setObjectName("eyebrow")
        diagnostic_layout.addWidget(diagnostic_tag)
        self.diagnostic_label = QLabel(_("gui", "text", "warming_up"))
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
        self.campaign_metric = Metric(_("gui", "text", "campaign_metric"), "0.0%")
        self.drop_metric = Metric(_("gui", "text", "drop_metric"), "—")
        self.watch_metric = Metric(
            _("gui", "text", "session_watch_metric"),
            "00:00:00",
        )
        self.live_metric = Metric(_("gui", "text", "live_channels_metric"), "0/0")
        self.campaigns_metric = Metric(_("gui", "text", "campaigns_metric"), "0")
        self.claimed_metric = Metric(_("gui", "text", "claimed_drops_metric"), "0")
        self.claimed_metric.setProperty("lastMetric", True)
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
        quick_title = QLabel(_("gui", "text", "quick_steering"))
        quick_title.setObjectName("eyebrow")
        quick_header.addWidget(quick_title)
        quick_header.addStretch(1)
        quick_header.addWidget(
            QLabel(_("gui", "text", "keep_moving")),
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        quick_body.addLayout(quick_header)
        actions = QHBoxLayout()
        for label, target, icon in (
            (_("gui", "text", "view_channels"), "channels", "ph.broadcast-fill"),
            (_("gui", "text", "browse_drops"), "drops", "ph.drop-fill"),
            (
                _("gui", "text", "open_preferences"),
                "settings",
                "ph.sliders-horizontal-fill",
            ),
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
        self._add_page(overview_scroll)

        channels = ChannelsPage()
        self.pages["channels"] = channels
        self._add_page(channels)

        self.inventory_page = InventoryPage(self._twitch.settings)
        self.pages["drops"] = self.inventory_page
        self.pages["inventory"] = self.inventory_page
        self._add_page(self.inventory_page)

        activity = ActivityPage()
        self.pages["activity"] = activity
        self._add_page(activity)

        history = HistoryPage()
        history.set_clear_callback(self._clear_history)
        self.pages["history"] = history
        self._history_page = history
        self._add_page(history)

        settings = QWidget()
        self.pages["settings"] = settings
        self._add_page(settings)

        help_page = QWidget()
        self.pages["help"] = help_page
        self._add_page(help_page)

        self.full_activity = activity
        self.settings_page = settings
        self.help_page = help_page

    def _add_page(self, page: QWidget) -> None:
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
            return _("gui", "text", "game_intel_unavailable")
        if not metadata.available:
            return _("gui", "text", "game_intel_no_match")
        parts: list[str] = []
        if metadata.players is not None:
            parts.append(
                _("gui", "text", "players_playing").format(
                    players=f"{metadata.players:,}"
                )
            )
        if metadata.price is not None:
            parts.append(
                _("gui", "text", "price_us").format(price=metadata.price)
            )
        elif metadata.free_to_play:
            parts.append(_("gui", "text", "free_to_play"))
        if metadata.discount_percent:
            parts.append(f"-{metadata.discount_percent}%")
        if not parts:
            parts.append(_("gui", "text", "steam_listing_found"))
        return _("gui", "text", "steam_intel").format(
            details="  ·  ".join(parts)
        )

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

    def _status_changed(self, text: str) -> None:
        self._current_status_text = text
        display = text or _("gui", "status", "idle")
        lowered = display.lower()

        def translated_prefix(*path: str) -> str:
            value = _(*path)
            return value.split("{", 1)[0].strip().lower()

        def matches(terms: tuple[str, ...]) -> bool:
            return any(term and term in lowered for term in terms)

        error_terms = (
            "error",
            "failed",
            "captcha",
            "terminated",
            translated_prefix("status", "terminated"),
            translated_prefix("gui", "status", "terminated"),
        )
        switch_terms = (
            "switch",
            "goes online",
            "goes offline",
            translated_prefix("gui", "status", "switching"),
            translated_prefix("status", "goes_online"),
            translated_prefix("status", "goes_offline"),
        )
        scan_terms = (
            "maint",
            "reload",
            "gather",
            "fetch",
            "cleanup",
            translated_prefix("gui", "status", "cleanup"),
            translated_prefix("gui", "status", "gathering"),
            translated_prefix("gui", "status", "fetching_inventory"),
            translated_prefix("gui", "status", "inventory_retry"),
            translated_prefix("gui", "status", "fetching_campaigns"),
            translated_prefix("gui", "status", "adding_campaigns"),
        )
        idle_attention_terms = (
            "no available",
            "no active",
            translated_prefix("status", "no_channel"),
            translated_prefix("status", "no_campaign"),
        )
        active_terms = (
            "watch",
            "active",
            "connected",
            translated_prefix("status", "watching"),
            translated_prefix("gui", "websocket", "connected"),
        )
        idle_terms = (
            "idle",
            translated_prefix("gui", "status", "idle"),
        )

        palette = self._theme.p
        if matches(error_terms):
            color = palette.error
            explanation = _("gui", "text", "status_error_detail")
        elif matches(switch_terms):
            color = palette.amber
            explanation = _("gui", "text", "status_switching_detail")
        elif matches(scan_terms):
            color = palette.amber
            explanation = _("gui", "text", "status_scanning_detail")
        elif matches(idle_attention_terms):
            color = palette.amber
            explanation = _("gui", "text", "status_idle_detail")
        elif matches(active_terms):
            color = palette.green
            explanation = _("gui", "text", "status_watching_detail")
        elif matches(idle_terms):
            color = palette.idle
            explanation = _("gui", "text", "status_idle_detail")
        else:
            color = palette.idle
            explanation = _("gui", "text", "status_waiting_detail")

        compact = (
            _("gui", "text", "status_live")
            if color == palette.green
            else _("gui", "text", "status_error")
            if color == palette.error
            else _("gui", "text", "status_attention")
            if color == palette.amber
            else _("gui", "text", "idle")
        )
        for widget in self._status_widgets:
            widget.set_state(color, compact)
            widget.setToolTip(display)
        self._signal_pulse.set_state(color, color != palette.idle)
        self.diagnostic_label.setText(explanation)

    def _reload_inventory(self) -> None:
        self._twitch.change_state(State.INVENTORY_FETCH)
        self.print(_("gui", "text", "reload_requested"))

    def _navigate(self, key: str) -> None:
        self._active_page = key
        titles = {
            "overview": _("gui", "text", "deck_title"),
            "channels": _("gui", "text", "live_channels_metric"),
            "drops": _("gui", "text", "inventory_title"),
            "inventory": _("gui", "text", "inventory_title"),
            "activity": _("gui", "text", "event_log"),
            "history": _("gui", "text", "history_title"),
            "settings": _("gui", "text", "preferences"),
            "help": _("gui", "text", "help_about").format(
                help=_("gui", "tabs", "help")
            ),
        }
        palette = self._theme.p
        for name, button in self._nav_buttons.items():
            active = name == key
            button.setChecked(active)
            button.setIcon(qta.icon(self._nav_icons[name], color=palette.accent if active else palette.muted))
        self._page_context.setText(
            titles.get(key, _("gui", "text", "deck_title"))
        )
        page = self.pages["drops"] if key == "inventory" else self.pages[key]
        if self._page_animation is not None:
            self._page_animation.stop()
            self._page_animation.deleteLater()
            self._page_animation = None
        if self._animation_page is not None:
            self._animation_page.setGraphicsEffect(cast(Any, None))
            self._animation_page = None
        self.stack.setCurrentWidget(page)
        effect = QGraphicsOpacityEffect(page)
        effect.setOpacity(0.92)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(140)
        animation.setStartValue(0.92)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        def finish_animation() -> None:
            page.setGraphicsEffect(cast(Any, None))
            if self._page_animation is animation:
                self._page_animation = None
                self._animation_page = None
            animation.deleteLater()

        animation.finished.connect(finish_animation)
        self._page_animation = animation
        self._animation_page = page
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

        work_task = asyncio.ensure_future(awaitable)
        close_task = asyncio.create_task(self._close_requested.wait())
        try:
            done, _ = await asyncio.wait(
                (work_task, close_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work_task in done:
                return await work_task
            if close_task in done:
                raise ExitRequest()
            return await work_task
        finally:
            await cancel_tasks((work_task, close_task))

    def prevent_close(self) -> None:
        self._close_requested.clear()

    def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self._metrics_timer.stop()
        self.progress.stop_timer()
        self.inventory_page.stop()
        self._cancel_metadata_task()
        if self._page_animation is not None:
            self._page_animation.stop()
            self._page_animation.deleteLater()
            self._page_animation = None
        if self._animation_page is not None:
            self._animation_page.setGraphicsEffect(cast(Any, None))
            self._animation_page = None
        await self._tasks.cancel_and_wait()
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

    def set_authenticated(self, authenticated: bool) -> None:
        self.help.set_authenticated(authenticated)

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
        self.tray.update_title(None)

    def print(self, message: str) -> None:
        self.output.print(message)

    def _clear_history(self) -> None:
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
        history_page = getattr(self, "_history_page", None)
        history = getattr(self._twitch, "history", None)
        if history_page is not None and history is not None:
            history_page.set_sessions(history.sessions)

    def on_history_event(self, event: HistoryEvent) -> None:
        alert = self._notifications.handle(event)
        if alert is None:
            return
        duration = 10000 if alert.severity != "info" else 7000
        shown = self.tray.notify(
            alert.message,
            alert.title,
            duration,
            severity=alert.severity,
        )
        self.tray.change_icon("error" if alert.severity == "error" else "maint")
        self.diagnostic_label.setText(alert.message)
        if not shown:
            self.print(
                _("notifications", "attention").format(
                    title=alert.title,
                    message=alert.message,
                )
            )

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

    def apply_theme(self, dark: bool) -> None:
        self._theme_dark = dark
        self._theme = make_theme(dark)
        apply_theme(self._app, self._theme)
        palette = self._theme.p
        if hasattr(self, "hero"):
            self._apply_ring_theme()
            self._refresh_semantic_colors()
            self._status_changed(self._current_status_text)
        active = getattr(self, "_active_page", "overview")
        for name, button in self._nav_buttons.items():
            button.setIcon(qta.icon(self._nav_icons[name], color=palette.accent if name == active else palette.muted))
        self.inventory.refresh()
