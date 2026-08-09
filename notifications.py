"""Actionable notification policy for structured session events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from session_history import HistoryEvent
from translate import _

AlertSeverity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class Alert:
    key: str
    title: str
    message: str
    severity: AlertSeverity


class NotificationCenter:
    """Turn meaningful history events into deduplicated actionable alerts."""

    _COOLDOWNS = {
        "auth.required": timedelta(minutes=30),
        "inventory.sync_failed": timedelta(minutes=30),
        "watch.unavailable": timedelta(hours=1),
        "claim.unconfirmed": timedelta(minutes=15),
        "session.failed": timedelta(minutes=30),
        "campaign.deadline": timedelta(hours=1),
    }

    def __init__(self) -> None:
        self._last_notified: dict[str, datetime] = {}
        self._active: dict[str, Alert] = {}
        self._claim_attempts: dict[str, int] = {}

    @property
    def active(self) -> tuple[Alert, ...]:
        return tuple(self._active.values())

    def handle(self, event: HistoryEvent) -> Alert | None:
        self._resolve(event)
        key = self._key_for(event)
        if key is None:
            return None
        if event.kind == "session.failed" and "auth.required" in self._active:
            return None
        if event.kind == "claim.unconfirmed":
            attempts = self._claim_attempts.get(key, 0) + 1
            self._claim_attempts[key] = attempts
            if attempts < 2:
                return None
        timestamp = _parse_timestamp(event.at)
        previous = self._last_notified.get(key)
        cooldown = self._COOLDOWNS.get(event.kind, timedelta(minutes=30))
        if previous is not None and timestamp - previous < cooldown:
            return None
        alert = self._build_alert(key, event)
        if alert is None:
            return None
        self._last_notified[key] = timestamp
        self._active[key] = alert
        return alert

    def _key_for(self, event: HistoryEvent) -> str | None:
        if event.kind == "auth.required":
            return "auth.required"
        if event.kind == "inventory.sync_failed":
            return "inventory.sync_failed"
        if event.kind == "watch.unavailable":
            return "watch.unavailable"
        if event.kind == "session.failed":
            return "session.failed"
        if event.kind == "campaign.deadline":
            campaign_id = event.data.get("campaign_id")
            return (
                f"campaign.deadline:{campaign_id}"
                if isinstance(campaign_id, str) and campaign_id
                else None
            )
        if event.kind == "claim.unconfirmed":
            drop_id = event.data.get("drop_id")
            return (
                f"claim.unconfirmed:{drop_id}"
                if isinstance(drop_id, str) and drop_id
                else None
            )
        return None

    def _build_alert(self, key: str, event: HistoryEvent) -> Alert | None:
        if event.kind == "auth.required":
            return Alert(
                key,
                _("notifications", "auth_required_title"),
                _("notifications", "auth_required_message"),
                "warning",
            )
        if event.kind == "inventory.sync_failed":
            return Alert(
                key,
                _("notifications", "inventory_failed_title"),
                _("notifications", "inventory_failed_message"),
                "warning",
            )
        if event.kind == "watch.unavailable":
            return Alert(
                key,
                _("notifications", "watch_unavailable_title"),
                _("notifications", "watch_unavailable_message"),
                "info",
            )
        if event.kind == "session.failed":
            return Alert(
                key,
                _("notifications", "session_failed_title"),
                _("notifications", "session_failed_message"),
                "error",
            )
        if event.kind == "campaign.deadline":
            return Alert(
                key,
                _("notifications", "campaign_deadline_title"),
                _("notifications", "campaign_deadline_message"),
                "warning",
            )
        if event.kind == "claim.unconfirmed":
            return Alert(
                key,
                _("notifications", "claim_unconfirmed_title"),
                _("notifications", "claim_unconfirmed_message"),
                "warning",
            )
        return None

    def _resolve(self, event: HistoryEvent) -> None:
        if event.kind == "auth.restored":
            self._active.pop("auth.required", None)
        elif event.kind in {"inventory.synced", "connection.recovered"}:
            self._active.pop("inventory.sync_failed", None)
        elif event.kind == "watch.started":
            self._active.pop("watch.unavailable", None)
        elif event.kind == "claim.succeeded":
            drop_id = event.data.get("drop_id")
            if isinstance(drop_id, str) and drop_id:
                key = f"claim.unconfirmed:{drop_id}"
                self._active.pop(key, None)
                self._claim_attempts.pop(key, None)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
