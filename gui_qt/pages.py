"""Reusable Qt pages for the TwitchDropsMiner presentation layer."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import qtawesome as qta
from PySide6.QtCore import QTimer, QUrl, QSize, Qt, Signal
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
from utils import cancel_tasks, format_duration, webopen

from .contracts import ImageCache
from .widgets import (
    Badge,
    BadgeRole,
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
    from session_history import SessionRecord


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

        authorization = QHBoxLayout()
        authorization.setSpacing(8)
        explanation = QLabel(_("gui", "text", "device_authorization"))
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        authorization.addWidget(explanation, 1)
        self.submit_button = QPushButton(_("gui", "login", "button"))
        self.submit_button.setObjectName("primary")
        self.submit_button.setIcon(qta.icon("ph.sign-in", color="#0b0d14"))
        self.submit_button.clicked.connect(self.on_submit.emit)
        authorization.addWidget(self.submit_button)
        self.body().addLayout(authorization)
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

        self.game_art = QLabel(_("gui", "text", "art_placeholder"))
        self.game_art.setObjectName("gameArt")
        self.game_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.game_art.setFixedSize(128, 172)
        self.game_art.setScaledContents(False)
        content.addWidget(self.game_art, 0, Qt.AlignmentFlag.AlignTop)

        left = QVBoxLayout()
        left.setContentsMargins(0, 2, 0, 0)
        left.setSpacing(6)
        self.eyebrow = QLabel(_("gui", "text", "live_target"))
        self.eyebrow.setObjectName("eyebrow")
        self.game = QLabel(_("gui", "text", "idle"))
        self.game.setObjectName("heroGame")
        self.campaign = QLabel(_("gui", "text", "no_active_campaign"))
        self.campaign.setObjectName("heroCaption")
        self.campaign.setWordWrap(True)
        self.channel = QLabel(_("gui", "text", "no_active_watch"))
        self.channel.setObjectName("heroChannel")
        self.intel = QLabel(_("gui", "text", "game_intel_waiting"))
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
        self.badge = Badge(_("gui", "text", "idle_badge"), "idle")
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
        campaign_head.addWidget(
            self._eyebrow(_("gui", "progress", "campaign").rstrip(":").upper())
        )
        campaign_head.addStretch(1)
        self.campaign_percent = QLabel("0.0%")
        self.campaign_percent.setObjectName("heroPercent")
        campaign_head.addWidget(self.campaign_percent)
        campaign_block.addLayout(campaign_head)
        self.campaign_progress = SegmentedProgress()
        self.campaign_progress.setAccessibleName(
            _("gui", "progress", "campaign").rstrip(":")
        )
        campaign_block.addWidget(self.campaign_progress)
        self.campaign_remaining = QLabel(
            _("gui", "text", "campaign_remaining").format(time="—")
        )
        self.campaign_remaining.setObjectName("subtle")
        campaign_block.addWidget(self.campaign_remaining)

        drop_block = QVBoxLayout()
        drop_block.setSpacing(5)
        drop_head = QHBoxLayout()
        drop_head.addWidget(
            self._eyebrow(_("gui", "progress", "drop").rstrip(":").upper())
        )
        drop_head.addStretch(1)
        self.drop_percent = QLabel("0.0%")
        self.drop_percent.setObjectName("heroPercent")
        drop_head.addWidget(self.drop_percent)
        drop_block.addLayout(drop_head)
        self.drop_rewards = QLabel("—")
        self.drop_rewards.setObjectName("heroCaption")
        drop_block.addWidget(self.drop_rewards)
        self.drop_progress = SegmentedProgress()
        self.drop_progress.setAccessibleName(
            _("gui", "progress", "drop").rstrip(":")
        )
        drop_block.addWidget(self.drop_progress)
        self.drop_remaining = QLabel(
            _("gui", "text", "drop_remaining").format(time="—")
        )
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
            self.game_art.setText(_("gui", "text", "art_placeholder"))
        else:
            self.game_art.setText("")
            self.game_art.setPixmap(self._art_pixmap)
        self.update()

    def set_channel(self, name: str | None, viewers: int | None = None) -> None:
        if not name:
            self.channel.setText(_("gui", "text", "no_active_watch"))
            return
        viewer_label = _("gui", "channels", "headings", "viewers").lower()
        suffix = (
            f"  ·  {viewers:,} {viewer_label}"
            if viewers is not None
            else ""
        )
        self.channel.setText(
            _("gui", "text", "watching_channel").format(
                name=name,
                suffix=suffix,
            )
        )

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
        self.ring.set_progress(
            progress,
            f"{progress:.0%}",
            _("gui", "progress", "campaign").rstrip(":"),
        )
        self.badge.setText(f"{progress:.1%}")
        self.badge.set_role("accent")
        self.drop_rewards.setText(drop_rewards or "—")
        self.drop_progress.set_value(drop_progress)
        self.drop_percent.setText(drop_percent)
        self.campaign_changed.emit(progress, name, drop_percent, drop_rewards or "—")

    def set_remaining(self, *, drop: str, campaign: str) -> None:
        self.drop_remaining.setText(
            _("gui", "text", "drop_remaining").format(time=drop)
        )
        self.campaign_remaining.setText(
            _("gui", "text", "campaign_remaining").format(time=campaign)
        )

    def clear(self) -> None:
        self.game.setText(_("gui", "text", "idle"))
        self.campaign.setText(_("gui", "text", "no_active_campaign"))
        self.set_art(None)
        self.set_channel(None)
        self.set_intel(_("gui", "text", "game_intel_waiting"))
        self.set_links(steam="", steamdb="", twitch="")
        self.campaign_progress.set_value(0)
        self.campaign_percent.setText("0.0%")
        self.ring.set_progress(
            0,
            "0%",
            _("gui", "progress", "campaign").rstrip(":"),
        )
        self.campaign_remaining.setText(
            _("gui", "text", "campaign_remaining").format(time="—")
        )
        self.drop_rewards.setText("—")
        self.drop_progress.set_value(0)
        self.drop_percent.setText("0.0%")
        self.drop_remaining.setText(
            _("gui", "text", "drop_remaining").format(time="—")
        )
        self.badge.setText(_("gui", "text", "idle_badge"))
        self.badge.set_role("idle")
        self.campaign_changed.emit(
            0.0,
            _("gui", "text", "no_active_campaign"),
            "—",
            _("gui", "text", "no_active_drop"),
        )


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
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(
            _("gui", "text", "select_channel").format(channel=channel.name)
        )
        self.setAccessibleDescription(self._meta(channel))
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
            row.addWidget(
                Badge(_("gui", "text", "pending_badge"), "warning")
            )
        elif channel.online:
            row.addWidget(
                Badge(_("gui", "text", "live_badge"), "success")
            )
        else:
            row.addWidget(
                Badge(_("gui", "text", "offline_badge"), "idle")
            )
        if getattr(channel, "drops_enabled", False):
            row.addWidget(
                Badge(_("gui", "text", "drops_badge"), "info")
            )
        if watching:
            row.addWidget(
                Badge(_("gui", "text", "watching_badge"), "accent")
            )
        open_button = QPushButton(_("gui", "text", "open"))
        open_button.setObjectName("rowAction")
        open_button.setToolTip(
            _("gui", "text", "open_channel").format(channel=channel.name)
        )
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
            viewers = _("gui", "channels", "headings", "viewers").lower()
            parts.append(f"{channel.viewers:,} {viewers}")
        return " · ".join(parts)

    def mouseReleaseEvent(self, event: Any) -> None:
        self.clicked.emit(self.channel_id)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.channel_id)
            event.accept()
            return
        super().keyPressEvent(event)


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
            _("gui", "text", "channels_kicker"),
            _("gui", "channels", "name"),
            _("gui", "text", "channels_subtitle"),
        ))
        self.count = QLabel(
            _("gui", "text", "monitored").format(count=0)
        )
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
        self.search.setPlaceholderText(_("gui", "text", "filter_channels"))
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)
        self.empty = EmptyState(
            _("gui", "text", "no_channels"),
            _("gui", "text", "no_channels_body"),
            _("gui", "text", "dismiss"),
        )
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
        self.count.setText(
            _("gui", "text", "monitored_live").format(
                count=len(self._channels),
                live=live,
            )
        )

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
        title = QLabel(
            _("gui", "text", "latest_signals")
            if compact
            else _("gui", "text", "event_log")
        )
        title.setObjectName("eyebrow" if compact else "h2")
        heading.addWidget(title)
        heading.addStretch(1)
        hint = QLabel(_("gui", "text", "diagnostics"))
        hint.setObjectName("subtle")
        heading.addWidget(hint)
        layout.addLayout(heading)
        self.log = ActivityLog()
        self.log.setObjectName("activityLog")
        if compact:
            self.log.setMaximumHeight(150)
        layout.addWidget(self.log)


def _history_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def _history_time(value: str | None) -> str:
    parsed = _history_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed is not None else "—"


class SessionCard(Card):
    def __init__(self, session: SessionRecord) -> None:
        super().__init__()
        self.setObjectName("sessionCard")
        status_values: dict[str, tuple[str, BadgeRole]] = {
            "running": (_("gui", "text", "session_running"), "success"),
            "stopped": (_("gui", "text", "session_stopped"), "idle"),
            "failed": (_("gui", "text", "session_failed"), "error"),
            "interrupted": (_("gui", "text", "session_interrupted"), "warning"),
        }
        default_status: tuple[str, BadgeRole] = (
            session.status.upper(),
            "idle",
        )
        label, role = status_values.get(session.status, default_status)
        header = QHBoxLayout()
        title = QLabel(
            _("gui", "text", "session_title").format(
                time=_history_time(session.started_at)
            )
        )
        title.setObjectName("h2")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(Badge(label, role))
        self.body().addLayout(header)

        ended = (
            _("gui", "text", "session_in_progress")
            if session.ended_at is None
            else _history_time(session.ended_at)
        )
        started = _history_datetime(session.started_at)
        finished = _history_datetime(session.ended_at)
        duration = "—"
        if started is not None:
            end = finished or datetime.now(started.tzinfo)
            duration = format_duration(max(0, (end - started).total_seconds()))
        summary = session.summary
        summary_label = QLabel(
            _("gui", "text", "session_summary").format(
                start=_history_time(session.started_at),
                end=ended,
                duration=duration,
                claims=summary.get("claims_succeeded", 0),
                syncs=summary.get("inventory_syncs", 0),
            )
        )
        summary_label.setObjectName("muted")
        self.body().addWidget(summary_label)

        if session.events:
            event_text = " · ".join(
                event.kind.replace(".", " ") for event in session.events[-5:]
            )
            events_label = QLabel(event_text)
            events_label.setObjectName("subtle")
            events_label.setWordWrap(True)
            self.body().addWidget(events_label)


class HistoryPage(QWidget):
    clear_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("historyShell")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(PageIntro(
            _("gui", "text", "history_kicker"),
            _("gui", "text", "history_title"),
            _("gui", "text", "history_subtitle"),
        ))
        header.addStretch(1)
        clear_button = QPushButton(_("gui", "text", "clear_completed"))
        clear_button.setObjectName("ghost")
        clear_button.clicked.connect(self.clear_requested.emit)
        header.addWidget(clear_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self.empty = EmptyState(
            _("gui", "text", "no_sessions"),
            _("gui", "text", "no_sessions_body"),
            _("gui", "text", "dismiss"),
        )
        self.empty.action.connect(self.empty.hide)
        layout.addWidget(self.empty)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        self._cards_layout = QVBoxLayout(container)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(10)
        self._cards_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

    def set_clear_callback(self, callback: Any) -> None:
        self.clear_requested.connect(callback)

    def set_sessions(self, sessions: Iterable[SessionRecord]) -> None:
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        session_list = list(sessions)
        for session in session_list:
            self._cards_layout.insertWidget(
                self._cards_layout.count() - 1,
                SessionCard(session),
            )
        self.empty.setVisible(not session_list)


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

    async def load_images(self, cache: ImageCache) -> None:
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
                _("gui", "inventory", "percent_progress").format(
                    percent="0.0%",
                    minutes=drop.required_minutes,
                )
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
        self.image.setText(_("gui", "text", "image_placeholder"))
        self.image.setObjectName("imagePlaceholder")
        root.addWidget(self.image, 0, Qt.AlignmentFlag.AlignTop)
        content = QVBoxLayout()
        content.setSpacing(5)
        heading = QHBoxLayout()
        self.game = QLabel(campaign.game.name)
        self.game.setObjectName("h2")
        heading.addWidget(self.game)
        self.status = Badge("", "idle")
        heading.addWidget(self.status)
        heading.addStretch(1)
        content.addLayout(heading)
        self.name = QLabel(campaign.name)
        self.name.setObjectName("muted")
        content.addWidget(self.name)
        self.timeline = QLabel()
        self.timeline.setObjectName("subtle")
        content.addWidget(self.timeline)
        self.link = QPushButton(_("gui", "text", "link_campaign"))
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
        content.addWidget(self.allowed)
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
        try:
            webopen(str(self.campaign.link_url))
        except ValueError:
            return

    async def load_images(self, cache: ImageCache) -> None:
        self.image.setPixmap(await cache.get(self.campaign.image_url, (108, 144)))
        self.image.setText("")
        for row in self._drop_rows.values():
            await row.load_images(cache)

    def refresh(self) -> None:
        c = self.campaign
        if c.active:
            label, role = _("gui", "inventory", "status", "active"), "success"
        elif c.upcoming:
            label, role = _("gui", "inventory", "status", "upcoming"), "warning"
        else:
            label, role = _("gui", "inventory", "status", "expired"), "error"
        self.status.setText(label)
        self.status.set_role(role)
        self.timeline.setText(self._timeline(c))
        self.link.setVisible(not c.linked)
        if c.allowed_channels:
            names = ", ".join(channel.name for channel in c.allowed_channels[:5])
            if len(c.allowed_channels) > 5:
                names += " + " + _(
                    "gui", "inventory", "and_more"
                ).format(amount=len(c.allowed_channels) - 5)
            self.allowed.setText(f'{_("gui", "inventory", "allowed_channels")} {names}')
        else:
            self.allowed.setText(
                f'{_("gui", "inventory", "allowed_channels")} {_("gui", "inventory", "all_channels")}'
            )
        self.progress.set_fraction(c.progress)
        self.summary.setText(
            _("gui", "text", "claimed_summary").format(
                progress=f"{c.progress:.1%}",
                claimed=c.claimed_drops,
                total=c.total_drops,
            )
        )
        for row in self._drop_rows.values():
            row.refresh()

    @staticmethod
    def _timeline(campaign: DropsCampaign) -> str:
        target = campaign.starts_at if campaign.upcoming else campaign.ends_at
        key = "starts" if campaign.upcoming else "ends"
        local_time = target.astimezone().strftime("%Y-%m-%d %H:%M")
        return _("gui", "inventory", key).format(time=local_time)


class _StagedCampaignPresentation:
    def __init__(
        self,
        page: InventoryPage,
        staged: dict[str, CampaignCard],
    ) -> None:
        self._page = page
        self._staged = staged
        self._previous = page._campaigns
        self._committed = False
        self._finished = False

    def commit(self) -> None:
        if self._finished or self._committed:
            raise RuntimeError("Campaign presentation is not staged")
        layout = self._page._cards_layout
        for card in self._previous.values():
            layout.removeWidget(card)
        inserted: list[CampaignCard] = []
        try:
            for card in self._staged.values():
                layout.insertWidget(layout.count() - 1, card)
                inserted.append(card)
            self._page._campaigns = self._staged
            self._page.empty.setVisible(not self._staged)
            self._page.refresh()
        except Exception:
            for card in inserted:
                layout.removeWidget(card)
            self._page._campaigns = self._previous
            for card in self._previous.values():
                layout.insertWidget(layout.count() - 1, card)
            self._page.empty.setVisible(not self._previous)
            self._page.refresh()
            raise
        self._committed = True

    def rollback(self) -> None:
        if self._finished:
            return
        layout = self._page._cards_layout
        if self._committed:
            for card in self._staged.values():
                layout.removeWidget(card)
            self._page._campaigns = self._previous
            for card in self._previous.values():
                layout.insertWidget(layout.count() - 1, card)
            self._page.empty.setVisible(not self._previous)
            self._page.refresh()
        for card in self._staged.values():
            card.deleteLater()
        self._finished = True

    def finalize(self) -> None:
        if self._finished or not self._committed:
            raise RuntimeError("Campaign presentation was not committed")
        for card in self._previous.values():
            card.deleteLater()
        self._previous = {}
        self._finished = True


class InventoryPage(QWidget):
    def __init__(self, settings: Any) -> None:
        super().__init__()
        self._settings = settings
        self._campaigns: dict[str, CampaignCard] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(30_000)
        self._refresh_timer.timeout.connect(self.refresh)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(PageIntro(
            _("gui", "text", "inventory_kicker"),
            _("gui", "text", "inventory_title"),
            _("gui", "text", "inventory_subtitle"),
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
        self.empty = EmptyState(
            _("gui", "text", "no_campaigns"),
            _("gui", "text", "no_campaigns_body"),
            _("gui", "text", "dismiss"),
        )
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

    def start(self) -> None:
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def stop(self) -> None:
        self._refresh_timer.stop()

    async def stage_campaigns(
        self,
        campaigns: Iterable[DropsCampaign],
        cache: ImageCache,
    ) -> _StagedCampaignPresentation:
        staged: dict[str, CampaignCard] = {}
        for campaign in campaigns:
            if campaign.id in staged:
                raise ValueError(f"Duplicate campaign ID: {campaign.id}")
            staged[campaign.id] = CampaignCard(campaign)

        load_tasks = [
            asyncio.create_task(card.load_images(cache))
            for card in staged.values()
        ]
        loaded = False
        try:
            await asyncio.gather(*load_tasks)
            loaded = True
        finally:
            await cancel_tasks(load_tasks)
            if not loaded:
                for card in staged.values():
                    card.deleteLater()
        return _StagedCampaignPresentation(self, staged)

    async def replace_campaigns(
        self,
        campaigns: Iterable[DropsCampaign],
        cache: ImageCache,
    ) -> None:
        presentation = await self.stage_campaigns(campaigns, cache)
        try:
            presentation.commit()
        except BaseException:
            presentation.rollback()
            raise
        presentation.finalize()

    def _clear_campaigns(self) -> None:
        for card in self._campaigns.values():
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._campaigns.clear()

    def update_drop(self, drop: TimedDrop) -> None:
        if drop.campaign.id in self._campaigns:
            self.refresh()


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
