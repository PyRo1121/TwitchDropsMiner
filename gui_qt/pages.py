"""Reusable Qt pages for the TwitchDropsMiner presentation layer."""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import qtawesome as qta
from PySide6.QtCore import QUrl, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from translate import _
from game_metadata import SteamMetadata

from .widgets import (
    Badge,
    Card,
    EmptyState,
    PageIntro,
    Progress,
    ProgressRing,
    SectionTitle,
    SegmentedProgress,
)

if TYPE_CHECKING:
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop


class LoginPanel(Card):
    on_submit = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("loginPanel")
        header = QHBoxLayout()
        icon = QLabel()
        icon.setObjectName("loginIcon")
        icon.setPixmap(qta.icon("ph.sign-in", color="#b69cff").pixmap(QSize(18, 18)))
        header.addWidget(icon)
        header.addWidget(SectionTitle(_("gui", "login", "name")))
        header.addStretch(1)
        self.status = QLabel(_("gui", "login", "logged_out"))
        self.status.setObjectName("muted")
        header.addWidget(self.status)
        self.body().addLayout(header)

        fields = QHBoxLayout()
        fields.setSpacing(8)
        self.username = QLineEdit(placeholderText=_("gui", "login", "username"))
        self.password = QLineEdit(placeholderText=_("gui", "login", "password"))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.token = QLineEdit(placeholderText=_("gui", "login", "twofa_code"))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        for field in (self.username, self.password, self.token):
            field.setMinimumWidth(140)
            fields.addWidget(field, 1)
        self.submit_button = QPushButton(_("gui", "login", "button"))
        self.submit_button.setObjectName("primary")
        self.submit_button.setIcon(qta.icon("ph.sign-in", color="#0b0d14"))
        self.submit_button.clicked.connect(self.on_submit.emit)
        fields.addWidget(self.submit_button)
        self.body().addLayout(fields)
        self.setVisible(False)

    def set_busy(self, busy: bool) -> None:
        self.submit_button.setEnabled(not busy)
        self.submit_button.setText(_("gui", "login", "logging_in") if busy else _("gui", "login", "button"))

    def set_status(self, status: str, user_id: int | None) -> None:
        self.status.setText(status if user_id is None else f"{status} · {user_id}")
        self.setVisible(user_id is None)


class HeroCard(Card):
    """Artwork-led focal surface for the one game currently being farmed."""

    campaign_changed = Signal(float, str, str, str)

    def __init__(self) -> None:
        super().__init__(elevated=True)
        self.setObjectName("heroCard")
        self._art_pixmap: QPixmap | None = None
        self._backdrop_color = QColor("#0b0d14")
        content = QHBoxLayout()
        content.setContentsMargins(2, 2, 2, 2)
        content.setSpacing(20)

        self.game_art = QLabel("ART")
        self.game_art.setObjectName("gameArt")
        self.game_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.game_art.setFixedSize(128, 172)
        self.game_art.setScaledContents(False)
        content.addWidget(self.game_art, 0, Qt.AlignmentFlag.AlignTop)

        left = QVBoxLayout()
        left.setContentsMargins(0, 2, 0, 0)
        left.setSpacing(6)
        self.eyebrow = QLabel("LIVE TARGET  /  FARMING NOW")
        self.eyebrow.setObjectName("eyebrow")
        self.game = QLabel("Idle")
        self.game.setObjectName("heroGame")
        self.campaign = QLabel("No active campaign")
        self.campaign.setObjectName("heroCaption")
        self.campaign.setWordWrap(True)
        self.channel = QLabel("NO ACTIVE WATCH")
        self.channel.setObjectName("heroChannel")
        self.intel = QLabel("GAME INTEL  ·  waiting for a target")
        self.intel.setObjectName("heroIntel")
        left.addWidget(self.eyebrow)
        left.addWidget(self.game)
        left.addWidget(self.campaign)
        left.addSpacing(5)
        left.addWidget(self.channel)
        left.addWidget(self.intel)

        links = QHBoxLayout()
        links.setSpacing(6)
        self._link_urls: dict[str, str] = {}
        self.steam_button = self._link_button("STEAM", "steam")
        self.steamdb_button = self._link_button("STEAMDB", "steamdb")
        self.twitch_button = self._link_button("TWITCH", "twitch")
        for button in (self.steam_button, self.steamdb_button, self.twitch_button):
            links.addWidget(button)
        links.addStretch(1)
        left.addLayout(links)
        self.badge = Badge("IDLE", "#a1a9bd", "rgba(161,169,189,0.14)")
        left.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignLeft)
        left.addStretch(1)
        content.addLayout(left, 1)
        self.ring = ProgressRing()
        content.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignCenter)
        self.body().addLayout(content)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(24)
        campaign_block = QVBoxLayout()
        campaign_block.setSpacing(5)
        campaign_head = QHBoxLayout()
        campaign_head.addWidget(self._eyebrow("CAMPAIGN"))
        campaign_head.addStretch(1)
        self.campaign_percent = QLabel("0.0%")
        self.campaign_percent.setObjectName("heroPercent")
        campaign_head.addWidget(self.campaign_percent)
        campaign_block.addLayout(campaign_head)
        self.campaign_progress = SegmentedProgress()
        campaign_block.addWidget(self.campaign_progress)
        self.campaign_remaining = QLabel("Campaign remaining: —")
        self.campaign_remaining.setObjectName("subtle")
        campaign_block.addWidget(self.campaign_remaining)

        drop_block = QVBoxLayout()
        drop_block.setSpacing(5)
        drop_head = QHBoxLayout()
        drop_head.addWidget(self._eyebrow("CURRENT DROP"))
        drop_head.addStretch(1)
        self.drop_percent = QLabel("0.0%")
        self.drop_percent.setObjectName("heroPercent")
        drop_head.addWidget(self.drop_percent)
        drop_block.addLayout(drop_head)
        self.drop_rewards = QLabel("—")
        self.drop_rewards.setObjectName("heroCaption")
        drop_block.addWidget(self.drop_rewards)
        self.drop_progress = SegmentedProgress()
        drop_block.addWidget(self.drop_progress)
        self.drop_remaining = QLabel("Drop remaining: —")
        self.drop_remaining.setObjectName("subtle")
        drop_block.addWidget(self.drop_remaining)
        progress_row.addLayout(campaign_block, 1)
        progress_row.addLayout(drop_block, 1)
        self.body().addLayout(progress_row)

    @staticmethod
    def _eyebrow(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("eyebrow")
        return label

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        if self._art_pixmap is None or self._art_pixmap.isNull():
            return
        target = self.rect().adjusted(1, 1, -1, -1)
        scaled = self._art_pixmap.scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        source_x = max(0, (scaled.width() - target.width()) // 2)
        source_y = max(0, (scaled.height() - target.height()) // 2)
        painter = QPainter(self)
        painter.setOpacity(0.28)
        painter.drawPixmap(target, scaled, scaled.rect().adjusted(
            source_x, source_y, -max(0, scaled.width() - target.width() - source_x),
            -max(0, scaled.height() - target.height() - source_y),
        ))
        overlay = QLinearGradient(0, 0, self.width(), 0)
        base = self._backdrop_color
        overlay.setColorAt(0.0, QColor(base.red(), base.green(), base.blue(), 245))
        overlay.setColorAt(0.55, QColor(base.red(), base.green(), base.blue(), 225))
        overlay.setColorAt(1.0, QColor(base.red(), base.green(), base.blue(), 130))
        painter.setOpacity(1.0)
        painter.fillRect(target, overlay)
        painter.end()

    def _link_button(self, label: str, key: str) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName("heroLink")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setEnabled(False)
        button.clicked.connect(lambda _=False, value=key: self._open_link(value))
        return button

    def _open_link(self, key: str) -> None:
        url = self._link_urls.get(key)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def set_links(self, *, steam: str, steamdb: str, twitch: str) -> None:
        self._link_urls = {"steam": steam, "steamdb": steamdb, "twitch": twitch}
        self.steam_button.setEnabled(bool(steam))
        self.steamdb_button.setEnabled(bool(steamdb))
        self.twitch_button.setEnabled(bool(twitch))

    def set_art(self, pixmap: QPixmap | None) -> None:
        self._art_pixmap = None if pixmap is None or pixmap.isNull() else pixmap
        if self._art_pixmap is None:
            self.game_art.setPixmap(QPixmap())
            self.game_art.setText("ART")
        else:
            self.game_art.setText("")
            self.game_art.setPixmap(self._art_pixmap)
        self.update()

    def set_channel(self, name: str | None, viewers: int | None = None) -> None:
        if not name:
            self.channel.setText("NO ACTIVE WATCH")
            return
        suffix = f"  ·  {viewers:,} viewers" if viewers is not None else ""
        self.channel.setText(f"WATCHING  /  @{name}{suffix}")

    def set_intel(self, text: str) -> None:
        self.intel.setText(text)

    def set_backdrop_color(self, color: str) -> None:
        self._backdrop_color = QColor(color)
        self.update()

    def set_campaign(
        self,
        *,
        game: str,
        name: str,
        progress: float,
        progress_text: str,
        drop_rewards: str,
        drop_progress: float,
        drop_percent: str,
    ) -> None:
        self.game.setText(game)
        self.campaign.setText(name)
        self.campaign_progress.set_value(progress)
        self.campaign_percent.setText(progress_text)
        self.ring.set_progress(progress, f"{progress:.0%}")
        self.badge.setText(f"{progress:.1%}")
        self.drop_rewards.setText(drop_rewards or "—")
        self.drop_progress.set_value(drop_progress)
        self.drop_percent.setText(drop_percent)
        self.campaign_changed.emit(progress, name, drop_percent, drop_rewards or "—")

    def set_remaining(self, *, drop: str, campaign: str) -> None:
        self.drop_remaining.setText(f"Drop remaining: {drop}")
        self.campaign_remaining.setText(f"Campaign remaining: {campaign}")

    def clear(self) -> None:
        self.game.setText("Idle")
        self.campaign.setText("No active campaign")
        self.set_art(None)
        self.set_channel(None)
        self.set_intel("GAME INTEL  ·  waiting for a target")
        self.set_links(steam="", steamdb="", twitch="")
        self.campaign_progress.set_value(0)
        self.campaign_percent.setText("0.0%")
        self.ring.set_progress(0, "0%")
        self.campaign_remaining.setText("Campaign remaining: —")
        self.drop_rewards.setText("—")
        self.drop_progress.set_value(0)
        self.drop_percent.setText("0.0%")
        self.drop_remaining.setText("Drop remaining: —")
        self.badge.setText("IDLE")
        self.campaign_changed.emit(0.0, "No active campaign", "—", "No active drop")


class ChannelRow(Card):
    clicked = Signal(int)

    def __init__(
        self,
        channel: Channel,
        *,
        watching: bool = False,
        selected: bool = False,
    ) -> None:
        super().__init__()
        self.channel_id = channel.id
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("selectedChannelRow" if watching or selected else "channelRow")
        row = QHBoxLayout()
        avatar = QLabel(channel.name[:1].upper())
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setFixedSize(40, 40)
        avatar.setStyleSheet(
            "background:rgba(182,156,255,0.14);color:#b69cff;"
            "border-radius:0;font-size:14pt;font-weight:700;"
        )
        row.addWidget(avatar)
        copy = QVBoxLayout()
        name = QLabel(channel.name)
        name.setStyleSheet("font-size:11pt;font-weight:700;")
        meta = QLabel(self._meta(channel))
        meta.setObjectName("muted")
        copy.addWidget(name)
        copy.addWidget(meta)
        row.addLayout(copy, 1)
        if channel.pending_online:
            row.addWidget(Badge("ONLINE?", "#ffb86b", "rgba(255,184,107,0.14)"))
        elif channel.online:
            row.addWidget(Badge("LIVE", "#5fe1d3", "rgba(95,225,211,0.14)"))
        else:
            row.addWidget(Badge("OFFLINE", "#69728a", "rgba(105,114,138,0.14)"))
        if getattr(channel, "drops_enabled", False):
            row.addWidget(Badge("DROPS", "#8fc6ff", "rgba(143,198,255,0.14)"))
        if watching:
            row.addWidget(Badge("WATCHING", "#b69cff", "rgba(182,156,255,0.14)"))
        open_button = QPushButton("OPEN")
        open_button.setObjectName("rowAction")
        open_button.setToolTip(f"Open {channel.name} on Twitch")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        channel_url = str(getattr(channel, "url", ""))
        open_button.setEnabled(bool(channel_url))
        open_button.clicked.connect(
            lambda _=False, url=channel_url: QDesktopServices.openUrl(QUrl(url))
        )
        row.addWidget(open_button)
        self.body().addLayout(row)

    @staticmethod
    def _meta(channel: Channel) -> str:
        parts = [f"@{channel.name}"]
        if channel.game is not None:
            parts.append(str(channel.game.name))
        if channel.viewers is not None:
            parts.append(f"{channel.viewers:,} viewers")
        return " · ".join(parts)

    def mouseReleaseEvent(self, event: Any) -> None:
        self.clicked.emit(self.channel_id)
        super().mouseReleaseEvent(event)


class ChannelsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._channels: dict[int, Channel] = {}
        self._rows: dict[int, ChannelRow] = {}
        self._selected_id: int | None = None
        self._watching_ids: set[int] = set()
        self._switch_callback: Any = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(PageIntro(
            "CHANNELS / WATCH LIST",
            _("gui", "channels", "name"),
            "Choose what the miner should watch next.",
        ))
        self.count = QLabel("0 monitored")
        self.count.setObjectName("muted")
        self.switch_button = QPushButton(_("gui", "channels", "switch"))
        self.switch_button.setObjectName("primary")
        self.switch_button.setIcon(qta.icon("ph.arrow-right", color="#0b0d14"))
        self.switch_button.setEnabled(False)
        self.switch_button.clicked.connect(self._switch)
        header.addStretch(1)
        header.addWidget(self.count)
        header.addWidget(self.switch_button)
        layout.addLayout(header)
        self.search = QLineEdit()
        self.search.setObjectName("searchField")
        self.search.setPlaceholderText("Filter channels  ·  name or game")
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)
        self.empty = EmptyState("No channels yet", "The miner will populate monitored channels here.", "Dismiss")
        self.empty.action.connect(self.empty.hide)
        layout.addWidget(self.empty)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._container = QWidget()
        self._rows_layout = QVBoxLayout(self._container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)
        scroll.setWidget(self._container)
        layout.addWidget(scroll, 1)

    def display(self, channel: Channel, *, add: bool) -> None:
        if not add and channel.id not in self._channels:
            return
        self._channels[channel.id] = channel
        self._replace_row(channel)
        self.empty.setVisible(not self._channels)
        self._update_count()
        self._apply_filter()

    def _update_count(self) -> None:
        live = sum(bool(channel.online) for channel in self._channels.values())
        self.count.setText(f"{len(self._channels)} monitored  ·  {live} live")

    def _apply_filter(self) -> None:
        query = self.search.text().strip().lower()
        for channel_id, row in self._rows.items():
            channel = self._channels[channel_id]
            game = channel.game.name if channel.game is not None else ""
            row.setVisible(not query or query in channel.name.lower() or query in game.lower())

    def _replace_row(self, channel: Channel) -> None:
        old = self._rows.pop(channel.id, None)
        index = self._rows_layout.count() - 1
        if old is not None:
            index = self._rows_layout.indexOf(old)
            self._rows_layout.removeWidget(old)
            old.hide()
            old.setParent(None)
            old.deleteLater()
        row = ChannelRow(
            channel,
            watching=channel.id in self._watching_ids,
            selected=channel.id == self._selected_id,
        )
        row.clicked.connect(self._select)
        self._rows_layout.insertWidget(max(0, index), row)
        self._rows[channel.id] = row
        self._apply_filter()

    def _rerender(self) -> None:
        channels = list(self._channels.values())
        for row in self._rows.values():
            self._rows_layout.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        for channel in channels:
            self._replace_row(channel)

    def _select(self, channel_id: int) -> None:
        self._selected_id = channel_id
        self.switch_button.setEnabled(True)
        self._rerender()

    def set_switch_callback(self, callback: Any) -> None:
        self._switch_callback = callback

    def _switch(self) -> None:
        if self._selected_id is not None and self._switch_callback is not None:
            self._switch_callback()

    def remove(self, channel: Channel) -> None:
        self._channels.pop(channel.id, None)
        self._watching_ids.discard(channel.id)
        if self._selected_id == channel.id:
            self._selected_id = None
            self.switch_button.setEnabled(False)
        row = self._rows.pop(channel.id, None)
        if row is not None:
            self._rows_layout.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self.empty.setVisible(not self._channels)
        self._update_count()
        self._apply_filter()

    def set_watching(self, channel: Channel) -> None:
        self.set_watching_channels((channel,))

    def set_watching_channels(self, channels: Iterable[Channel]) -> None:
        self._watching_ids = {channel.id for channel in channels}
        self._rerender()

    def clear_watching(self) -> None:
        self._watching_ids.clear()
        self._rerender()

    def counts(self) -> tuple[int, int]:
        return len(self._channels), sum(bool(channel.online) for channel in self._channels.values())

    def selected(self) -> Channel | None:
        return self._channels.get(self._selected_id) if self._selected_id is not None else None

    def clear_selection(self) -> None:
        self._selected_id = None
        self.switch_button.setEnabled(False)
        self._rerender()

    def clear_all(self) -> None:
        for row in self._rows.values():
            self._rows_layout.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._channels.clear()
        self._selected_id = None
        self._watching_ids.clear()
        self.switch_button.setEnabled(False)
        self.empty.setVisible(True)
        self._update_count()
        self._apply_filter()


class ActivityLog(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(2000)
        self.setStyleSheet("font-family:'Courier New',monospace;font-size:10px;")

    def append_message(self, message: str) -> None:
        self.appendPlainText(message)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class ActivityPage(QWidget):
    def __init__(self, *, compact: bool = False) -> None:
        super().__init__()
        self.setObjectName("activityShell")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14) if compact else layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        title = QLabel("LATEST SIGNALS" if compact else "Event log")
        title.setObjectName("eyebrow" if compact else "h2")
        heading.addWidget(title)
        heading.addStretch(1)
        hint = QLabel("diagnostics")
        hint.setObjectName("subtle")
        heading.addWidget(hint)
        layout.addLayout(heading)
        self.log = ActivityLog()
        self.log.setObjectName("activityLog")
        if compact:
            self.log.setMaximumHeight(150)
        layout.addWidget(self.log)


class DropRow(Card):
    def __init__(self, drop: TimedDrop) -> None:
        super().__init__()
        self.setObjectName("dropRow")
        self.drop = drop
        self._benefits = QHBoxLayout()
        self._benefits.setSpacing(6)
        self._status = QLabel()
        self._status.setObjectName("muted")
        self._progress = Progress()
        self._progress.setFixedHeight(5)
        title = QLabel(drop.rewards_text() or drop.name)
        title.setStyleSheet("font-weight:600;")
        self.body().addWidget(title)
        self.body().addLayout(self._benefits)
        self.body().addWidget(self._progress)
        self.body().addWidget(self._status)
        self.refresh()

    async def load_images(self, cache: Any) -> None:
        for benefit in self.drop.benefits:
            image = QLabel()
            image.setFixedSize(56, 56)
            image.setScaledContents(False)
            image.setPixmap(await cache.get(benefit.image_url, (56, 56)))
            image.setToolTip(benefit.name)
            self._benefits.addWidget(image)

    def refresh(self) -> None:
        drop = self.drop
        self._progress.set_fraction(drop.progress)
        if drop.is_claimed:
            self._status.setText(_("gui", "inventory", "status", "claimed"))
            self._status.setStyleSheet("color:#5fe1d3;")
        elif drop.can_claim:
            self._status.setText(_("gui", "inventory", "status", "ready_to_claim"))
            self._status.setStyleSheet("color:#ffb86b;")
        elif drop.current_minutes or drop.can_earn():
            self._status.setText(
                _("gui", "inventory", "percent_progress").format(
                    percent=f"{drop.progress:.1%}", minutes=drop.required_minutes
                )
            )
            self._status.setStyleSheet("")
        elif drop.required_minutes > 0:
            self._status.setText(
                _("gui", "inventory", "starts").format(time=f"{drop.required_minutes} minutes")
            )
            self._status.setStyleSheet("")
        else:
            self._status.setText(_("gui", "inventory", "status", "not_linked"))
            self._status.setStyleSheet("")


class CampaignCard(Card):
    def __init__(self, campaign: DropsCampaign) -> None:
        super().__init__()
        self.setObjectName("campaignRow")
        self.campaign = campaign
        self._drop_rows: dict[str, DropRow] = {}
        root = QHBoxLayout()
        self.image = QLabel()
        self.image.setFixedSize(108, 144)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setText("IMAGE")
        self.image.setObjectName("imagePlaceholder")
        root.addWidget(self.image, 0, Qt.AlignmentFlag.AlignTop)
        content = QVBoxLayout()
        content.setSpacing(5)
        heading = QHBoxLayout()
        self.game = QLabel(campaign.game.name)
        self.game.setObjectName("h2")
        heading.addWidget(self.game)
        self.status = Badge("", "#a1a9bd", "rgba(161,169,189,0.14)")
        heading.addWidget(self.status)
        heading.addStretch(1)
        content.addLayout(heading)
        self.name = QLabel(campaign.name)
        self.name.setObjectName("muted")
        content.addWidget(self.name)
        self.timeline = QLabel()
        self.timeline.setObjectName("subtle")
        content.addWidget(self.timeline)
        self.link = QPushButton("Link this campaign on Twitch")
        self.link.setObjectName("ghost")
        self.link.clicked.connect(self._open_link)
        content.addWidget(self.link, 0, Qt.AlignmentFlag.AlignLeft)
        external_links = QHBoxLayout()
        steam = SteamMetadata(campaign.game.name)
        self.steam = QPushButton("Steam")
        self.steam.setObjectName("rowAction")
        self.steam.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(steam.store_url)))
        self.steamdb = QPushButton("SteamDB")
        self.steamdb.setObjectName("rowAction")
        self.steamdb.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(steam.steamdb_url)))
        external_links.addWidget(self.steam)
        external_links.addWidget(self.steamdb)
        external_links.addStretch(1)
        content.addLayout(external_links)
        self.allowed = QLabel()
        self.allowed.setObjectName("subtle")
        self.allowed.setWordWrap(True)
        self.allowed.setVisible(False)
        self.progress = Progress()
        content.addWidget(self.progress)
        self.summary = QLabel()
        self.summary.setObjectName("muted")
        content.addWidget(self.summary)
        self._drops_layout = QVBoxLayout()
        self._drops_layout.setSpacing(0)
        for drop in campaign.drops:
            row = DropRow(drop)
            self._drop_rows[drop.id] = row
            self._drops_layout.addWidget(row)
        content.addLayout(self._drops_layout)
        content.addStretch(1)
        root.addLayout(content, 1)
        self.body().addLayout(root)
        self.refresh()

    def _open_link(self) -> None:
        QDesktopServices.openUrl(QUrl(str(self.campaign.link_url)))

    async def load_images(self, cache: Any) -> None:
        self.image.setPixmap(await cache.get(self.campaign.image_url, (108, 144)))
        self.image.setText("")
        for row in self._drop_rows.values():
            await row.load_images(cache)

    def refresh(self) -> None:
        c = self.campaign
        if c.active:
            label, color, bg = _("gui", "inventory", "status", "active"), "#5fe1d3", "rgba(95,225,211,0.14)"
        elif c.upcoming:
            label, color, bg = _("gui", "inventory", "status", "upcoming"), "#ffb86b", "rgba(255,184,107,0.14)"
        else:
            label, color, bg = _("gui", "inventory", "status", "expired"), "#ff6f91", "rgba(255,111,145,0.14)"
        self.status.setText(label)
        self.status.setStyleSheet(f"background:{bg};color:{color};border-radius:0;padding:2px 8px;")
        self.timeline.setText(self._timeline(c))
        self.link.setVisible(not c.linked)
        if c.allowed_channels:
            names = ", ".join(channel.name for channel in c.allowed_channels[:5])
            if len(c.allowed_channels) > 5:
                names += f" + {len(c.allowed_channels) - 5} more"
            self.allowed.setText(f'{_("gui", "inventory", "allowed_channels")} {names}')
        else:
            self.allowed.setText(
                f'{_("gui", "inventory", "allowed_channels")} {_("gui", "inventory", "all_channels")}'
            )
        self.progress.set_fraction(c.progress)
        self.summary.setText(f"{c.progress:.1%} · {c.claimed_drops}/{c.total_drops} claimed")
        for row in self._drop_rows.values():
            row.refresh()

    @staticmethod
    def _timeline(campaign: DropsCampaign) -> str:
        prefix = "Starts in" if campaign.upcoming else "Ends in"
        return f"{prefix} {max(0, campaign.remaining_minutes)} min"

    def update_drop(self, drop: TimedDrop) -> None:
        row = self._drop_rows.get(drop.id)
        if row is not None:
            row.refresh()
        self.refresh()


class InventoryPage(QWidget):
    def __init__(self, settings: Any) -> None:
        super().__init__()
        self._settings = settings
        self._campaigns: dict[str, CampaignCard] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(PageIntro(
            "DROPS / REWARD TRACKER",
            "Drops",
            "Campaigns are sorted by what can be claimed next.",
        ))
        self._refresh_button = QPushButton(_("gui", "inventory", "filter", "refresh"))
        self._refresh_button.setObjectName("ghost")
        self._refresh_button.setIcon(qta.icon("ph.arrows-clockwise", color="#b69cff"))
        header.addStretch(1)
        header.addWidget(self._refresh_button)
        layout.addLayout(header)
        filters = QHBoxLayout()
        self.not_linked = QCheckBox(_("gui", "inventory", "filter", "not_linked"))
        self.upcoming = QCheckBox(_("gui", "inventory", "filter", "upcoming"))
        self.expired = QCheckBox(_("gui", "inventory", "filter", "expired"))
        self.excluded = QCheckBox(_("gui", "inventory", "filter", "excluded"))
        self.finished = QCheckBox(_("gui", "inventory", "filter", "finished"))
        self.not_linked.setChecked(settings.priority_mode.name == "PRIORITY_ONLY")
        self.upcoming.setChecked(True)
        for box in (self.not_linked, self.upcoming, self.expired, self.excluded, self.finished):
            box.setObjectName("filterChip")
            box.stateChanged.connect(self.refresh)
            filters.addWidget(box)
        filters.addStretch(1)
        layout.addLayout(filters)
        self.empty = EmptyState("No campaigns", "Inventory will appear after Twitch data is loaded.", "Dismiss")
        self.empty.action.connect(self.empty.hide)
        layout.addWidget(self.empty)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._container = QWidget()
        self._cards_layout = QVBoxLayout(self._container)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(0)
        self._cards_layout.addStretch(1)
        scroll.setWidget(self._container)
        layout.addWidget(scroll, 1)

    def set_refresh_callback(self, callback: Any) -> None:
        self._refresh_button.clicked.connect(callback)

    async def add_campaign(self, campaign: DropsCampaign, cache: Any) -> None:
        if campaign.id in self._campaigns:
            self._campaigns[campaign.id].refresh()
            return
        card = CampaignCard(campaign)
        self._campaigns[campaign.id] = card
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
        self.empty.setVisible(False)
        await card.load_images(cache)
        self.refresh()

    def clear(self) -> None:
        for card in self._campaigns.values():
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._campaigns.clear()
        self.empty.setVisible(True)

    def update_drop(self, drop: TimedDrop) -> None:
        card = self._campaigns.get(drop.campaign.id)
        if card is not None:
            card.update_drop(drop)
            self.refresh()

    def update_progress(self, drop: TimedDrop, label: Any = None) -> None:
        self.update_drop(drop)

    def get_status(self, campaign: DropsCampaign) -> tuple[str, str]:
        if campaign.active:
            return _("gui", "inventory", "status", "active"), "#5fe1d3"
        if campaign.upcoming:
            return _("gui", "inventory", "status", "upcoming"), "#ffb86b"
        return _("gui", "inventory", "status", "expired"), "#ff6f91"

    def _visible(self, campaign: DropsCampaign) -> bool:
        priority_only = self._settings.priority_mode.name == "PRIORITY_ONLY"
        return (
            campaign.required_minutes > 0
            and (self.not_linked.isChecked() or campaign.eligible)
            and (
                campaign.active
                or self.upcoming.isChecked() and campaign.upcoming
                or self.expired.isChecked() and campaign.expired
            )
            and (
                self.excluded.isChecked()
                or (
                    campaign.game.name not in self._settings.exclude
                    and (not priority_only or campaign.game.name in self._settings.priority)
                )
            )
            and (self.finished.isChecked() or not campaign.finished)
        )

    def refresh(self) -> None:
        visible = False
        for card in self._campaigns.values():
            campaign = card.campaign
            card.refresh()
            is_visible = self._visible(campaign)
            card.setVisible(is_visible)
            visible = visible or is_visible
        self.empty.setVisible(not visible)
