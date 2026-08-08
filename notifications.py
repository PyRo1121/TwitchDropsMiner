"""Actionable notification policy for structured session events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from session_history import HistoryEvent

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
            return "campaign.deadline:" + str(event.data.get("campaign", "unknown"))
        if event.kind == "claim.unconfirmed":
            return "claim.unconfirmed:" + self._claim_context(event)
        return None

    @staticmethod
    def _claim_context(event: HistoryEvent) -> str:
        game = event.data.get("game", "unknown")
        reward = event.data.get("reward", "unknown")
        return f"{game}:{reward}"

    def _build_alert(self, key: str, event: HistoryEvent) -> Alert | None:
        if event.kind == "auth.required":
            return Alert(
                key,
                "Twitch authentication required",
                "Open Twitch Drops Miner and sign in to resume farming.",
                "warning",
            )
        if event.kind == "inventory.sync_failed":
            return Alert(
                key,
                "Inventory refresh failed",
                "Twitch inventory could not be refreshed; farming state may be stale.",
                "warning",
            )
        if event.kind == "watch.unavailable":
            return Alert(
                key,
                "No eligible live channel",
                "The miner is standing by until an eligible channel becomes available.",
                "info",
            )
        if event.kind == "session.failed":
            return Alert(
                key,
                "Miner stopped unexpectedly",
                "Open the event log to inspect the failure and restart the miner.",
                "error",
            )
        if event.kind == "campaign.deadline":
            return Alert(
                key,
                "Drop campaign ending soon",
                "An unfinished campaign is approaching its deadline.",
                "warning",
            )
        if event.kind == "claim.unconfirmed":
            return Alert(
                key,
                "Drop claim needs attention",
                "A claim could not be confirmed after retrying; it will be reconciled again.",
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
            prefix = "claim.unconfirmed:" + self._claim_context(event)
            self._active.pop(prefix, None)
            self._claim_attempts.pop(prefix, None)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
