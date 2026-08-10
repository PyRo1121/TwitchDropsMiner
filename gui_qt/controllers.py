"""Focused presentation controllers used by :mod:`gui_qt.manager`.

These controllers own volatile dashboard, status, and incident presentation
state.  Keeping that state beside the widgets it drives leaves the public GUI
manager as a backend adapter and lifecycle composition root.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QWidget

from notifications import NotificationCenter
from session_history import HistoryEvent
from translate import _
from utils import format_duration

from .pages import HeroCard
from .subs import QtChannelList
from .theme import Theme
from .tray import QtTray
from .widgets import Metric, SignalPulse, StatusDot


class QtDashboardController:
    """Own live dashboard counters and watched-channel presentation state."""

    def __init__(
        self,
        source: Any,
        channels: QtChannelList,
        hero: HeroCard,
        *,
        campaign_metric: Metric,
        drop_metric: Metric,
        watch_metric: Metric,
        live_metric: Metric,
        campaigns_metric: Metric,
        claimed_metric: Metric,
        parent: QWidget,
    ) -> None:
        self._source = source
        self._channels = channels
        self._hero = hero
        self._campaign_metric = campaign_metric
        self._drop_metric = drop_metric
        self._watch_metric = watch_metric
        self._live_metric = live_metric
        self._campaigns_metric = campaigns_metric
        self._claimed_metric = claimed_metric
        self._watching_id: int | None = None
        self._watching_since: float | None = None
        self._watched_seconds = 0.0
        self.timer = QTimer(parent)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        hero.campaign_changed.connect(self.campaign_changed)
        self.refresh()

    def start(self) -> None:
        if not self.timer.isActive():
            self.timer.start()
        self.refresh()

    def stop(self) -> None:
        self.timer.stop()

    def channel_watching(self, channel: Any) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id != self._watching_id:
            self.channel_cleared()
            self._watching_id = channel_id
            self._watching_since = time.monotonic()
        self._hero.set_channel(
            getattr(channel, "name", None),
            getattr(channel, "viewers", None),
        )

    def channel_cleared(self) -> None:
        if self._watching_since is not None:
            self._watched_seconds += time.monotonic() - self._watching_since
        self._watching_id = None
        self._watching_since = None
        self._hero.set_channel(None)

    def refresh(self) -> None:
        watched = self._watched_seconds
        if self._watching_since is not None:
            watched += time.monotonic() - self._watching_since
        self._watch_metric.set_value(format_duration(watched))
        monitored, live = self._channels.counts()
        self._live_metric.set_value(f"{live}/{monitored}")
        campaigns = getattr(self._source, "inventory", [])
        self._campaigns_metric.set_value(str(len(campaigns)))
        claimed = sum(
            getattr(campaign, "claimed_drops", 0) for campaign in campaigns
        )
        self._claimed_metric.set_value(str(claimed))
        if self._watching_id is not None:
            channel = getattr(self._source, "channels", {}).get(self._watching_id)
            if channel is not None:
                self._hero.set_channel(channel.name, channel.viewers)

    def campaign_changed(
        self,
        progress: float,
        campaign: str,
        drop_percent: str,
        drop_rewards: str,
    ) -> None:
        self._campaign_metric.set_value(f"{progress:.1%}")
        self._campaign_metric.setToolTip(campaign)
        self._drop_metric.set_value(drop_percent)
        self._drop_metric.setToolTip(drop_rewards)
        self.refresh()


class QtStatusController:
    """Translate backend status text into the dashboard's semantic state."""

    def __init__(
        self,
        widgets: tuple[StatusDot, ...],
        pulse: SignalPulse,
        diagnostic_label: QLabel,
        theme: Callable[[], Theme],
    ) -> None:
        self._widgets = widgets
        self._pulse = pulse
        self._diagnostic_label = diagnostic_label
        self._theme = theme
        self.current_text = _("gui", "status", "idle")

    def update(self, text: str) -> None:
        self.current_text = text
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

        palette = self._theme().p
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
        for widget in self._widgets:
            widget.set_state(color, compact)
            widget.setToolTip(display)
        self._pulse.set_state(color, color != palette.idle)
        self._diagnostic_label.setText(explanation)
        self._diagnostic_label.setAccessibleDescription(explanation)

    def refresh_theme(self) -> None:
        self.update(self.current_text)


class QtNotificationController:
    """Render history incident transitions without owning session history."""

    def __init__(
        self,
        tray: QtTray,
        diagnostic_label: QLabel,
        output: Callable[[str], None],
    ) -> None:
        self.center = NotificationCenter()
        self._tray = tray
        self._diagnostic_label = diagnostic_label
        self._output = output

    def handle(self, event: HistoryEvent) -> None:
        alert = self.center.handle(event)
        if alert is None:
            return
        duration = 10000 if alert.severity != "info" else 7000
        shown = self._tray.notify(
            alert.message,
            alert.title,
            duration,
            severity=alert.severity,
        )
        self._tray.change_icon(
            "error" if alert.severity == "error" else "maint"
        )
        self._diagnostic_label.setText(alert.message)
        self._diagnostic_label.setAccessibleDescription(alert.message)
        if not shown:
            self._output(
                _("notifications", "attention").format(
                    title=alert.title,
                    message=alert.message,
                )
            )
