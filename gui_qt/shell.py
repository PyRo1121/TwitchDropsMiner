"""Window shell and navigation for the Qt presentation layer.

The shell owns widget construction, page registration, command navigation, and
page-transition cleanup.  It deliberately knows nothing about Twitch runtime
state; :class:`QtGUIManager` supplies the few application callbacks it needs.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import qtawesome as qta
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
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

from constants import _resource_path as resource_path
from translate import _

from .pages import (
    ActivityPage,
    ChannelsPage,
    HeroCard,
    HistoryPage,
    InventoryPage,
    LoginPanel,
)
from .theme import Theme
from .widgets import Card, IconButton, Metric, SignalPulse, StatusDot


class QtWindowShell:
    """Build and control the toolkit-only application shell."""

    def __init__(
        self,
        window: QMainWindow,
        settings: Any,
        theme: Theme,
        *,
        clear_history: Callable[[], None],
        reload_inventory: Callable[[], None],
        switch_channel: Callable[[], None],
    ) -> None:
        self._window = window
        self._theme = theme
        self._page_animation: QPropertyAnimation | None = None
        self._animation_page: QWidget | None = None
        self._active_page = "overview"

        central = QWidget(window)
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
        window.setCentralWidget(central)

        self._command_shortcut = QShortcut(QKeySequence("Ctrl+K"), window)
        self._command_shortcut.activated.connect(self.focus_command)

        self.pages: dict[str, QWidget] = {}
        self._build_pages(
            settings,
            clear_history=clear_history,
            reload_inventory=reload_inventory,
            switch_channel=switch_channel,
        )
        self._select_page("overview", animate=False, focus_page=False)

    @property
    def page_animation(self) -> QPropertyAnimation | None:
        return self._page_animation

    @property
    def animation_page(self) -> QWidget | None:
        return self._animation_page

    @property
    def active_page(self) -> str:
        return self._active_page

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
        brand_symbol = QPixmap(
            str(resource_path("gui_qt/assets/drop_deck_brand.png"))
        )
        if brand_symbol.isNull():
            mark.setPixmap(
                qta.icon("ph.broadcast-tower-fill", color="#0b0d14").pixmap(
                    QSize(24, 24)
                )
            )
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

        self.nav_buttons: dict[str, QPushButton] = {}
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
                _("gui", "text", "help_about")
                .format(help=_("gui", "tabs", "help"))
                .replace("&", "&&"),
            ),
        )
        for key, label in navigation:
            button = QPushButton(label)
            button.setObjectName("nav")
            button.setIconSize(QSize(18, 18))
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(label.replace("&&", "&"))
            button.clicked.connect(
                lambda _=False, value=key: self.navigate(value)
            )
            self.nav_buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)
        self.sidebar_status = StatusDot(
            "#a1a9bd",
            _("gui", "text", "starting"),
        )
        layout.addWidget(self.sidebar_status)
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
        self.page_context = QLabel(_("gui", "text", "deck_title"))
        self.page_context.setObjectName("pageTitle")
        self.page_context.setAccessibleName(_("gui", "text", "deck_title"))
        context.addWidget(self.page_context)
        layout.addLayout(context)
        layout.addStretch(1)

        command = QLineEdit()
        command.setObjectName("command")
        command.setPlaceholderText(_("gui", "text", "search_placeholder"))
        command.setClearButtonEnabled(True)
        command.setMaximumWidth(310)
        command.returnPressed.connect(self.submit_command)
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
        completer.activated.connect(lambda _value: self.submit_command())
        command.setCompleter(completer)
        self.command = command
        layout.addWidget(command)

        status_frame = QFrame()
        status_frame.setObjectName("statusChip")
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(10, 2, 10, 2)
        self.topbar_status = StatusDot(
            "#a1a9bd",
            _("gui", "text", "idle"),
        )
        status_layout.addWidget(self.topbar_status)
        layout.addWidget(status_frame)

        account = IconButton(
            "ph.user-circle-fill",
            _("gui", "text", "account"),
            color="#a1a9bd",
        )
        account.setFixedSize(36, 36)
        account.clicked.connect(lambda: self.navigate("settings"))
        layout.addWidget(account)
        return topbar

    def _build_pages(
        self,
        settings: Any,
        *,
        clear_history: Callable[[], None],
        reload_inventory: Callable[[], None],
        switch_channel: Callable[[], None],
    ) -> None:
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
        self.health = StatusDot(
            "#a1a9bd",
            _("gui", "text", "starting"),
        )
        intro.addWidget(self.health, 0, Qt.AlignmentFlag.AlignTop)
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
        self.signal_pulse = SignalPulse()
        diagnostic_layout.addWidget(self.signal_pulse)
        diagnostic_tag = QLabel(_("gui", "text", "state_readout"))
        diagnostic_tag.setObjectName("eyebrow")
        diagnostic_layout.addWidget(diagnostic_tag)
        self.diagnostic_label = QLabel(_("gui", "text", "warming_up"))
        self.diagnostic_label.setObjectName("muted")
        self.diagnostic_label.setWordWrap(True)
        self.diagnostic_label.setAccessibleName(_("gui", "text", "state_readout"))
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
        self.campaign_metric = Metric(
            _("gui", "text", "campaign_metric"), "0.0%"
        )
        self.drop_metric = Metric(_("gui", "text", "drop_metric"), "—")
        self.watch_metric = Metric(
            _("gui", "text", "session_watch_metric"),
            "00:00:00",
        )
        self.live_metric = Metric(
            _("gui", "text", "live_channels_metric"), "0/0"
        )
        self.campaigns_metric = Metric(
            _("gui", "text", "campaigns_metric"), "0"
        )
        self.claimed_metric = Metric(
            _("gui", "text", "claimed_drops_metric"), "0"
        )
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
            action.clicked.connect(
                lambda _=False, value=target: self.navigate(value)
            )
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
        channels.set_switch_callback(switch_channel)
        self.pages["channels"] = channels
        self._add_page(channels)

        self.inventory_page = InventoryPage(settings)
        self.inventory_page.set_refresh_callback(reload_inventory)
        self.pages["drops"] = self.inventory_page
        self.pages["inventory"] = self.inventory_page
        self._add_page(self.inventory_page)

        activity = ActivityPage()
        self.pages["activity"] = activity
        self._add_page(activity)

        history = HistoryPage()
        history.set_clear_callback(clear_history)
        self.pages["history"] = history
        self.history_page = history
        self._add_page(history)

        self.settings_page = QWidget()
        self.pages["settings"] = self.settings_page
        self._add_page(self.settings_page)

        self.help_page = QWidget()
        self.pages["help"] = self.help_page
        self._add_page(self.help_page)

        self.channels_page = channels
        self.full_activity = activity
        self._apply_page_accessibility()

    def _add_page(self, page: QWidget) -> None:
        if self.stack.indexOf(page) < 0:
            self.stack.addWidget(page)

    def _titles(self) -> dict[str, str]:
        return {
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

    def _apply_page_accessibility(self) -> None:
        titles = self._titles()
        seen: set[int] = set()
        for key, page in self.pages.items():
            identity = id(page)
            if identity in seen:
                continue
            seen.add(identity)
            page.setAccessibleName(titles[key])
            page.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def focus_command(self) -> None:
        self.command.setFocus()
        self.command.selectAll()

    def submit_command(self) -> None:
        query = self.command.text().strip().lower()
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
        if target is None:
            self.command.selectAll()
            return
        self.command.clear()
        self.navigate(target)

    def navigate(self, key: str) -> None:
        self._select_page(key, animate=True, focus_page=True)

    def _select_page(
        self,
        key: str,
        *,
        animate: bool,
        focus_page: bool,
    ) -> None:
        titles = self._titles()
        if key not in titles:
            raise KeyError(key)
        self._active_page = key
        palette = self._theme.p
        active_nav = "drops" if key == "inventory" else key
        for name, button in self.nav_buttons.items():
            active = name == active_nav
            button.setChecked(active)
            button.setIcon(
                qta.icon(
                    self._nav_icons[name],
                    color=palette.accent if active else palette.muted,
                )
            )
        title = titles[key]
        self.page_context.setText(title)
        self.page_context.setAccessibleName(title)
        page = self.pages["drops"] if key == "inventory" else self.pages[key]
        self._clear_animation()
        self.stack.setCurrentWidget(page)
        if animate:
            self._animate_page(page)
        if key in ("drops", "inventory"):
            self.inventory_page.refresh()
        if focus_page:
            page.setFocus(Qt.FocusReason.TabFocusReason)

    def _animate_page(self, page: QWidget) -> None:
        effect = QGraphicsOpacityEffect(page)
        effect.setOpacity(0.92)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self._window)
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

    def _clear_animation(self) -> None:
        if self._page_animation is not None:
            self._page_animation.stop()
            self._page_animation.deleteLater()
            self._page_animation = None
        if self._animation_page is not None:
            self._animation_page.setGraphicsEffect(cast(Any, None))
            self._animation_page = None

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        palette = theme.p
        active = "drops" if self._active_page == "inventory" else self._active_page
        for name, button in self.nav_buttons.items():
            button.setIcon(
                qta.icon(
                    self._nav_icons[name],
                    color=palette.accent if name == active else palette.muted,
                )
            )

    def stop(self) -> None:
        """Synchronously quiesce transient shell activity."""
        self._clear_animation()
