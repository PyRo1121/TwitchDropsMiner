"""Adapters that preserve the backend's existing GUI callback contract."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

import qtawesome as qta
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from constants import PROJECT_URL, PriorityMode, State
from settings import parse_proxy
from translate import _
from utils import format_duration, webopen
from .autostart import AutostartError, AutostartManager
from .contracts import ImageCache, LoginManager
from .pages import (
    ActivityLog,
    ChannelsPage,
    HeroCard,
    InventoryPage,
    LoginPanel,
)
from .widgets import Card, PageIntro, SectionTitle

if TYPE_CHECKING:
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop
    from game import Game


class QtStatusBar:
    def __init__(self, label: QLabel, on_update: Callable[[str], None] | None = None) -> None:
        self._label = label
        self._on_update = on_update

    def update(self, text: str) -> None:
        self._label.setText(text)
        if self._on_update is not None:
            self._on_update(text)


class QtWebsocketStatus:
    def __init__(self, label: QLabel) -> None:
        self._label = label
        self._connections: dict[int, dict[str, Any]] = {}

    def update(
        self, idx: int, status: str | None = None, topics: int | None = None
    ) -> None:
        entry = self._connections.setdefault(idx, {"status": "", "topics": 0})
        if status is not None:
            entry["status"] = status
        if topics is not None:
            entry["topics"] = topics
        self._refresh()

    def remove(self, idx: int) -> None:
        self._connections.pop(idx, None)
        self._refresh()

    def _refresh(self) -> None:
        values = (
            _("gui", "text", "websocket_summary").format(
                index=idx,
                status=entry["status"],
                topics=entry["topics"],
            )
            for idx, entry in sorted(self._connections.items())
        )
        self._label.setText(
            "   ".join(values) or _("gui", "text", "websocket_waiting")
        )


class QtLoginForm:
    def __init__(self, card: LoginPanel, manager: LoginManager) -> None:
        self._card = card
        self._manager = manager
        self._confirm = asyncio.Event()
        self._card.on_submit.connect(self._on_submit)

    def _on_submit(self) -> None:
        self._confirm.set()

    async def wait_for_login_press(self) -> None:
        self._confirm.clear()
        self._card.set_busy(False)
        try:
            await self._manager.coro_unless_closed(self._confirm.wait())
        finally:
            self._card.set_busy(True)

    async def ask_enter_code(self, page_url: Any, user_code: str) -> None:
        self._card.set_status(
            _("gui", "text", "enter_device_code").format(code=user_code),
            None,
        )
        self._manager.grab_attention(sound=False)
        await self.wait_for_login_press()
        self._manager.print(
            _("gui", "text", "device_code").format(code=user_code)
        )
        webopen(page_url)

    def update(self, status: str, user_id: int | None) -> None:
        self._card.set_status(status, user_id)


class QtCampaignProgress:
    ALMOST_DONE_SECONDS = 10

    def __init__(self, hero: HeroCard) -> None:
        self._hero = hero
        self._drop: TimedDrop | None = None
        self._seconds = 0
        self._timer = QTimer(hero)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def start_timer(self) -> None:
        if not self._timer.isActive():
            self._update_time(60)
            if self._drop is not None and self._drop.remaining_minutes > 0:
                self._timer.start()

    def stop_timer(self) -> None:
        self._timer.stop()

    def minute_almost_done(self) -> bool:
        return not self._timer.isActive() or self._seconds <= self.ALMOST_DONE_SECONDS

    def _tick(self) -> None:
        self._seconds -= 1
        self._update_time()
        if self._seconds <= 0:
            self._timer.stop()

    def display(self, drop: TimedDrop | None, *, countdown: bool = True, subone: bool = False) -> None:
        self._drop = drop
        self.stop_timer()
        if drop is None:
            self._hero.clear()
            return
        campaign = drop.campaign
        self._hero.set_campaign(
            game=campaign.game.name,
            name=campaign.name,
            progress=campaign.progress,
            progress_text=f"{campaign.progress:.1%} ({campaign.claimed_drops}/{campaign.total_drops})",
            drop_rewards=drop.rewards_text(),
            drop_progress=drop.progress,
            drop_percent=f"{drop.progress:.1%}",
        )
        if countdown:
            self.start_timer()
        elif subone:
            self._update_time(0)
        else:
            self._update_time(60)

    def _update_time(self, seconds: int | None = None) -> None:
        if seconds is not None:
            self._seconds = seconds
        drop = self._drop
        drop_minutes = drop.remaining_minutes if drop is not None else 0
        campaign_minutes = drop.campaign.remaining_minutes if drop is not None else 0
        countdown_offset = self._seconds - 60
        self._hero.set_remaining(
            drop=format_duration(drop_minutes * 60 + countdown_offset, pad_hours=False),
            campaign=format_duration(
                campaign_minutes * 60 + countdown_offset, pad_hours=False
            ),
        )


class QtChannelList:
    def __init__(
        self,
        page: ChannelsPage,
        *,
        on_watching: Callable[[Channel], None] | None = None,
        on_cleared: Callable[[], None] | None = None,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        self._page = page
        self._on_watching = on_watching
        self._on_cleared = on_cleared
        self._on_changed = on_changed

    def _changed(self) -> None:
        if self._on_changed is not None:
            self._on_changed()

    def display(self, channel: Channel, *, add: bool = False) -> None:
        self._page.display(channel, add=add)
        self._changed()

    def remove(self, channel: Channel) -> None:
        self._page.remove(channel)
        self._changed()

    def set_watching(self, channel: Channel) -> None:
        self.set_watching_channels((channel,))

    def set_watching_channels(self, channels: Iterable[Channel]) -> None:
        channels = tuple(channels)
        self._page.set_watching_channels(channels)
        if self._on_watching is not None and channels:
            self._on_watching(channels[0])
        self._changed()

    def clear_watching(self) -> None:
        self._page.clear_watching()
        if self._on_cleared is not None:
            self._on_cleared()
        self._changed()

    def get_selection(self) -> Channel | None:
        return self._page.selected()

    def clear(self) -> None:
        self._page.clear_all()
        if self._on_cleared is not None:
            self._on_cleared()
        self._changed()

    def counts(self) -> tuple[int, int]:
        return self._page.counts()


class QtConsole:
    def __init__(self, logs: list[ActivityLog]) -> None:
        self._logs = logs

    def print(self, message: str) -> None:
        for log in self._logs:
            log.append_message(message)


class QtInventory:
    def __init__(self, page: InventoryPage, cache: ImageCache) -> None:
        self._page = page
        self._cache = cache

    async def replace_campaigns(
        self,
        campaigns: Iterable[DropsCampaign],
    ) -> None:
        await self._page.replace_campaigns(campaigns, self._cache)

    def update_drop(self, drop: TimedDrop) -> None:
        self._page.update_drop(drop)

    def refresh(self) -> None:
        self._page.refresh()


class _ButtonAdapter:
    def __init__(self, button: QPushButton) -> None:
        self.widget = button

    def config(self, *, state: str | None = None, **_: Any) -> None:
        if state is not None:
            self.widget.setEnabled(state != "disabled")


class QtHelp:
    def __init__(self, manager: Any, page: QWidget) -> None:
        self._manager = manager
        self._twitch = manager._twitch
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        layout.addWidget(PageIntro(
            _("gui", "text", "help_kicker"),
            _("gui", "text", "help_title").format(
                help=_("gui", "tabs", "help")
            ),
            _("gui", "text", "help_subtitle"),
        ))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        about = Card()
        about.body().addWidget(QLabel(_("gui", "text", "app_name")))
        about.body().addWidget(QLabel(_("gui", "text", "app_description")))
        repo = QPushButton(_("gui", "text", "open_repository"))
        repo.setObjectName("ghost")
        repo.clicked.connect(lambda: webopen(PROJECT_URL))
        about.body().addWidget(repo, 0, Qt.AlignmentFlag.AlignLeft)
        links = QHBoxLayout()
        for label, url in (
            (_("gui", "help", "links", "inventory"), "https://www.twitch.tv/drops/inventory"),
            (_("gui", "help", "links", "campaigns"), "https://www.twitch.tv/drops/campaigns"),
        ):
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.clicked.connect(lambda _=False, value=url: webopen(value))
            links.addWidget(button)
        links.addStretch(1)
        about.body().addLayout(links)
        diagnostics = QPushButton(_("gui", "text", "open_event_log"))
        diagnostics.setObjectName("ghost")
        diagnostics.setIcon(qta.icon("ph.activity", color="#b69cff"))
        diagnostics.clicked.connect(lambda: self._manager._navigate("activity"))
        about.body().addWidget(diagnostics, 0, Qt.AlignmentFlag.AlignLeft)
        content_layout.addWidget(about)

        for title_key, text_key in (
            ("how_it_works", "how_it_works_text"),
            ("getting_started", "getting_started_text"),
        ):
            card = Card()
            card.body().addWidget(SectionTitle(_("gui", "help", title_key)))
            description = QLabel(_("gui", "help", text_key))
            description.setWordWrap(True)
            card.body().addWidget(description)
            content_layout.addWidget(card)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        invalidate_row = QHBoxLayout()
        invalidate_row.addWidget(QLabel(_("gui", "help", "invalidate", "text")))
        invalidate = QPushButton(_("gui", "help", "invalidate", "button"))
        invalidate.setObjectName("danger")
        invalidate.clicked.connect(self.invalidate_token)
        invalidate_row.addWidget(invalidate)
        invalidate_row.addStretch(1)
        layout.addLayout(invalidate_row)
        self._invalidate_button = _ButtonAdapter(invalidate)
        self._invalidate_task: asyncio.Task[None] | None = None
        self.set_authenticated(False)

    def set_authenticated(self, authenticated: bool) -> None:
        self._invalidate_button.config(
            state="normal" if authenticated else "disabled"
        )

    def invalidate_token(self) -> None:
        if self._invalidate_task is not None and not self._invalidate_task.done():
            return
        self._invalidate_button.config(state="disabled")
        self._invalidate_task = self._manager._tasks.create(self._invalidate_token())

    async def _invalidate_token(self) -> None:
        auth_state = self._twitch._auth_state
        token = getattr(auth_state, "_access_token", None)
        try:
            if isinstance(token, str) and token:
                async with self._twitch.transport.request(
                    "POST",
                    "https://id.twitch.tv/oauth2/revoke",
                    data={
                        "client_id": self._twitch._client_type.CLIENT_ID,
                        "token": token,
                    },
                ) as response:
                    if response.status != 200:
                        self._manager.print(
                            _("gui", "text", "token_revoke_http").format(
                                status=response.status
                            )
                        )
        except Exception as exc:
            # Remote revocation is best-effort. Local logout must still erase
            # credentials and all account-derived state.
            self._manager.print(
                _("gui", "text", "token_revoke_error").format(error=exc)
            )
        finally:
            try:
                auth_state.invalidate(
                    delete_cookies=True,
                    delete_refresh_token=True,
                )
            finally:
                self._invalidate_task = None
                self._invalidate_button.config(state="disabled")
        self._twitch.change_state(State.RESTART)


class QtSettings:
    def __init__(self, manager: Any, page: QWidget) -> None:
        self._manager = manager
        self._twitch = manager._twitch
        self._settings = manager._twitch.settings
        self._autostart = AutostartManager(self._settings)
        self._game_names: set[str] = set()
        self._build(page)

    def _build(self, page: QWidget) -> None:
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        layout.addWidget(PageIntro(
            _("gui", "text", "settings_kicker"),
            _("gui", "tabs", "settings"),
            _("gui", "text", "settings_subtitle"),
        ))
        general = Card()
        form = QFormLayout()
        self.language = QComboBox()
        self.language.addItems(list(_.languages))
        self.language.setCurrentText(_.current)
        self.language.currentTextChanged.connect(self._language_changed)
        form.addRow(_("gui", "text", "language_restart"), self.language)
        self.proxy = QLineEdit(str(self._settings.proxy))
        self.proxy.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy.editingFinished.connect(self._proxy_changed)
        form.addRow(_("gui", "settings", "general", "proxy"), self.proxy)
        self.priority_mode = QComboBox()
        self._priority_modes = {
            PriorityMode.PRIORITY_ONLY: _("gui", "settings", "priority_modes", "priority_only"),
            PriorityMode.ENDING_SOONEST: _("gui", "settings", "priority_modes", "ending_soonest"),
            PriorityMode.LOW_AVBL_FIRST: _("gui", "settings", "priority_modes", "low_availability"),
        }
        self.priority_mode.addItems(list(self._priority_modes.values()))
        self.priority_mode.setCurrentText(
            self._priority_modes.get(
                self._settings.priority_mode,
                _("gui", "settings", "priority_modes", "priority_only"),
            )
        )
        self.priority_mode.currentTextChanged.connect(self._priority_mode_changed)
        form.addRow(_("gui", "settings", "general", "priority_mode"), self.priority_mode)
        self.autostart = QCheckBox()
        self.autostart.setChecked(self._autostart.is_enabled())
        self.autostart.toggled.connect(self._autostart_changed)
        form.addRow(_("gui", "settings", "general", "autostart"), self.autostart)
        self.tray = QCheckBox()
        self.tray.setChecked(bool(self._settings.autostart_tray))
        self.tray.toggled.connect(self._tray_changed)
        form.addRow(_("gui", "settings", "general", "tray"), self.tray)
        self.notifications = QCheckBox()
        self.notifications.setChecked(bool(self._settings.tray_notifications))
        self.notifications.toggled.connect(lambda value: setattr(self._settings, "tray_notifications", value))
        form.addRow(_("gui", "settings", "general", "tray_notifications"), self.notifications)
        self.history_retention = QComboBox()
        for days in (30, 90, 365):
            self.history_retention.addItem(
                _("gui", "text", "days").format(days=days),
                days,
            )
        retention_days = getattr(self._settings, "history_retention_days", 90)
        current_index = self.history_retention.findData(retention_days)
        self.history_retention.setCurrentIndex(current_index if current_index >= 0 else 1)
        self.history_retention.currentIndexChanged.connect(self._history_retention_changed)
        form.addRow(_("gui", "text", "history_retention"), self.history_retention)
        self.dark = QCheckBox()
        self.dark.setChecked(bool(self._settings.dark_mode))
        self.dark.toggled.connect(self._dark_changed)
        form.addRow(_("gui", "settings", "general", "dark_mode"), self.dark)
        general.body().addLayout(form)
        layout.addWidget(general)

        advanced = Card()
        advanced.body().addWidget(SectionTitle(_("gui", "settings", "advanced", "name")))
        self.badges = QCheckBox(_("gui", "settings", "advanced", "enable_badges_emotes"))
        self.badges.setChecked(bool(self._settings.enable_badges_emotes))
        self.badges.toggled.connect(lambda value: setattr(self._settings, "enable_badges_emotes", value))
        advanced.body().addWidget(self.badges)
        self.available_check = QCheckBox(_("gui", "settings", "advanced", "available_drops_check"))
        self.available_check.setChecked(bool(self._settings.available_drops_check))
        self.available_check.toggled.connect(lambda value: setattr(self._settings, "available_drops_check", value))
        advanced.body().addWidget(self.available_check)
        self.dual_watch = QCheckBox(
            _("gui", "settings", "advanced", "experimental_dual_watch")
        )
        self.dual_watch.setChecked(
            bool(getattr(self._settings, "experimental_dual_watch", False))
        )
        self.dual_watch.toggled.connect(
            lambda value: setattr(self._settings, "experimental_dual_watch", value)
        )
        advanced.body().addWidget(self.dual_watch)
        self.reload_status = QLabel()
        self.reload_status.setObjectName("muted")
        advanced.body().addWidget(self.reload_status)
        reload_button = QPushButton(_("gui", "settings", "reload"))
        reload_button.setObjectName("primary")
        reload_button.setIcon(qta.icon("ph.arrow-clockwise", color="#0b0d14"))
        reload_button.clicked.connect(self._reload)
        advanced.body().addWidget(reload_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(advanced)

        lists = QHBoxLayout()
        lists.addWidget(self._build_priority_card())
        lists.addWidget(self._build_exclude_card())
        layout.addLayout(lists)
        layout.addStretch(1)

    def _build_priority_card(self) -> QWidget:
        card = Card()
        card.body().addWidget(SectionTitle(_("gui", "settings", "priority")))
        row = QHBoxLayout()
        self.priority_entry = QComboBox()
        self.priority_entry.setEditable(True)
        add = QPushButton(_("gui", "text", "add"))
        add.clicked.connect(self._priority_add)
        row.addWidget(self.priority_entry, 1)
        row.addWidget(add)
        card.body().addLayout(row)
        self.priority_list = QListWidget()
        self.priority_list.addItems(self._settings.priority)
        card.body().addWidget(self.priority_list)
        buttons = QHBoxLayout()
        for label, callback in (
            ("↑", lambda: self._priority_move(-1)),
            ("↓", lambda: self._priority_move(1)),
            (_("gui", "text", "remove"), self._priority_remove),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        card.body().addLayout(buttons)
        return card

    def _build_exclude_card(self) -> QWidget:
        card = Card()
        card.body().addWidget(SectionTitle(_("gui", "settings", "exclude")))
        row = QHBoxLayout()
        self.exclude_entry = QComboBox()
        self.exclude_entry.setEditable(True)
        add = QPushButton(_("gui", "text", "add"))
        add.clicked.connect(self._exclude_add)
        row.addWidget(self.exclude_entry, 1)
        row.addWidget(add)
        card.body().addLayout(row)
        self.exclude_list = QListWidget()
        self.exclude_list.addItems(sorted(self._settings.exclude))
        card.body().addWidget(self.exclude_list)
        remove = QPushButton(_("gui", "text", "remove_selected"))
        remove.clicked.connect(self._exclude_remove)
        card.body().addWidget(remove)
        return card

    def _language_changed(self, language: str) -> None:
        self._settings.language = language

    def _proxy_changed(self) -> None:
        try:
            proxy = parse_proxy(self.proxy.text().strip())
            self._settings.proxy = proxy
        except ValueError:
            self.proxy.setStyleSheet("border:1px solid #ff6f91;")
            return
        self.proxy.setStyleSheet("")

    def _priority_mode_changed(self, label: str) -> None:
        for mode, name in self._priority_modes.items():
            if name == label:
                self._settings.priority_mode = mode
                self._watch_preferences_changed()
                return

    def _watch_preferences_changed(self) -> None:
        self._manager.inventory.refresh()
        self._twitch.change_state(State.GAMES_UPDATE)

    def _history_retention_changed(self, index: int) -> None:
        days = self.history_retention.itemData(index)
        if not isinstance(days, int):
            return
        self._settings.history_retention_days = days
        self._manager._twitch.history.set_retention_days(days)
        self._manager.history_changed()

    def _dark_changed(self, value: bool) -> None:
        self._settings.dark_mode = value
        self._manager.apply_theme(value)

    def _tray_changed(self, value: bool) -> None:
        self._settings.autostart_tray = value
        if self.autostart.isChecked():
            self._autostart_changed(True)

    def _autostart_changed(self, value: bool) -> None:
        try:
            self._autostart.set_enabled(value, tray=self.tray.isChecked())
            self.reload_status.setText(_("gui", "text", "startup_saved"))
        except AutostartError as exc:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not value)
            self.autostart.blockSignals(False)
            self.reload_status.setText(
                _("gui", "text", "startup_failed").format(error=exc)
            )

    def _reload(self) -> None:
        self._twitch.change_state(State.INVENTORY_FETCH)
        self.reload_status.setText(_("gui", "text", "reload_requested"))

    def set_games(self, games: set[Game]) -> None:
        self._game_names.update(game.name for game in games)
        self._update_choices()

    def _update_choices(self) -> None:
        priority = set(self._settings.priority)
        excluded = set(self._settings.exclude)
        self.priority_entry.clear()
        self.priority_entry.addItems(sorted(self._game_names - priority))
        self.exclude_entry.clear()
        self.exclude_entry.addItems(sorted(self._game_names - excluded))

    def _priority_add(self) -> None:
        name = self.priority_entry.currentText().strip()
        if not name or name in self._settings.priority:
            return
        self._settings.priority.append(name)
        self._settings.alter()
        self.priority_list.addItem(name)
        self._update_choices()
        self._watch_preferences_changed()

    def _priority_remove(self) -> None:
        row = self.priority_list.currentRow()
        if row < 0:
            return
        self.priority_list.takeItem(row)
        del self._settings.priority[row]
        self._settings.alter()
        self._update_choices()
        self._watch_preferences_changed()

    def _priority_move(self, amount: int) -> None:
        row = self.priority_list.currentRow()
        target = row + amount
        if row < 0 or target < 0 or target >= self.priority_list.count():
            return
        item = self.priority_list.takeItem(row)
        self.priority_list.insertItem(target, item)
        self.priority_list.setCurrentRow(target)
        value = self._settings.priority.pop(row)
        self._settings.priority.insert(target, value)
        self._settings.alter()
        self._watch_preferences_changed()

    def _exclude_add(self) -> None:
        name = self.exclude_entry.currentText().strip()
        if not name or name in self._settings.exclude:
            return
        self._settings.exclude.add(name)
        self._settings.alter()
        items = sorted(self._settings.exclude)
        self.exclude_list.clear()
        self.exclude_list.addItems(items)
        self._update_choices()
        self._watch_preferences_changed()

    def _exclude_remove(self) -> None:
        item = self.exclude_list.currentItem()
        if item is None:
            return
        self._settings.exclude.discard(item.text())
        self._settings.alter()
        self.exclude_list.takeItem(self.exclude_list.row(item))
        self._update_choices()
        self._watch_preferences_changed()
