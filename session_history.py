"""Bounded, structured history for Twitch Drops Miner process sessions."""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from constants import HISTORY_PATH
from utils import atomic_write

logger = logging.getLogger("TwitchDrops.history")

Scalar = str | int | float | bool | None
Severity = Literal["info", "warning", "error"]
SessionStatus = Literal["running", "stopped", "failed", "interrupted"]
_VALID_SEVERITIES = {"info", "warning", "error"}
_VALID_STATUSES = {"running", "stopped", "failed", "interrupted"}
_SUMMARY_EVENTS = {
    "inventory.synced": "inventory_syncs",
    "claim.succeeded": "claims_succeeded",
    "claim.unconfirmed": "claims_unconfirmed",
}


@dataclass
class HistoryEvent:
    at: str
    kind: str
    severity: Severity = "info"
    data: Mapping[str, Scalar] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "severity": self.severity,
            "data": dict(self.data),
        }

    @classmethod
    def from_json(cls, value: object) -> HistoryEvent | None:
        if not isinstance(value, Mapping):
            return None
        at = value.get("at")
        kind = value.get("kind")
        severity = value.get("severity", "info")
        data = value.get("data", {})
        if (
            not isinstance(at, str)
            or not at
            or not isinstance(kind, str)
            or not kind
            or not isinstance(severity, str)
            or severity not in _VALID_SEVERITIES
            or not isinstance(data, Mapping)
        ):
            return None
        safe_data: dict[str, Scalar] = {}
        for key, item in data.items():
            if not isinstance(key, str) or not _is_scalar(item):
                return None
            safe_data[key] = item
        return cls(at, kind, severity, safe_data)  # type: ignore[arg-type]


@dataclass
class SessionRecord:
    id: str
    started_at: str
    ended_at: str | None = None
    status: SessionStatus = "running"
    summary: dict[str, int] = field(default_factory=dict)
    events: list[HistoryEvent] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "summary": dict(self.summary),
            "events": [event.to_json() for event in self.events],
        }

    @classmethod
    def from_json(cls, value: object) -> SessionRecord | None:
        if not isinstance(value, Mapping):
            return None
        session_id = value.get("id")
        started_at = value.get("started_at")
        ended_at = value.get("ended_at")
        status = value.get("status")
        summary = value.get("summary", {})
        events = value.get("events", [])
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(started_at, str)
            or not started_at
            or (ended_at is not None and not isinstance(ended_at, str))
            or not isinstance(status, str)
            or status not in _VALID_STATUSES
            or not isinstance(summary, Mapping)
            or not isinstance(events, list)
        ):
            return None
        safe_summary: dict[str, int] = {}
        for key, item in summary.items():
            if not isinstance(key, str) or not isinstance(item, int) or isinstance(item, bool):
                return None
            safe_summary[key] = item
        safe_events = [event for item in events if (event := HistoryEvent.from_json(item)) is not None]
        return cls(
            id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            status=status,  # type: ignore[arg-type]
            summary=safe_summary,
            events=safe_events,
        )


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class SessionHistory:
    """Persist a small, local history of meaningful application sessions."""

    VERSION = 1
    DEFAULT_MAX_SESSIONS = 100
    DEFAULT_MAX_EVENTS_PER_SESSION = 200

    def __init__(
        self,
        path: Path = HISTORY_PATH,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_events_per_session: int = DEFAULT_MAX_EVENTS_PER_SESSION,
        retention_days: int | None = None,
    ) -> None:
        if max_sessions < 1 or max_events_per_session < 1:
            raise ValueError("History limits must be positive")
        if retention_days is not None and retention_days < 1:
            raise ValueError("History retention must be positive")
        self.path = path
        self.max_sessions = max_sessions
        self.max_events_per_session = max_events_per_session
        self.retention_days = retention_days
        self._loaded = False
        self._sessions: list[SessionRecord] = []
        self._current: SessionRecord | None = None

    @property
    def sessions(self) -> tuple[SessionRecord, ...]:
        self._load()
        return tuple(self._sessions)

    @property
    def current(self) -> SessionRecord | None:
        return self._current

    def start(self, *, at: datetime | None = None) -> SessionRecord:
        self._load()
        stamp = _timestamp(at)
        for session in self._sessions:
            if session.status == "running":
                session.status = "interrupted"
                session.ended_at = stamp
                self._append_event(
                    session,
                    HistoryEvent(
                        stamp,
                        "session.interrupted",
                        "warning",
                        {"reason": "process_restarted"},
                    ),
                )
        session = SessionRecord(
            id=uuid4().hex,
            started_at=stamp,
            summary={
                "inventory_syncs": 0,
                "claims_succeeded": 0,
                "claims_unconfirmed": 0,
                "watch_seconds": 0,
            },
        )
        self._sessions.insert(0, session)
        self._current = session
        self._trim(now=at)
        self._save()
        return session

    def record(
        self,
        kind: str,
        *,
        severity: Severity = "info",
        data: Mapping[str, Scalar] | None = None,
        at: datetime | None = None,
    ) -> HistoryEvent:
        if self._current is None:
            raise RuntimeError("A session must be started before recording history")
        if not kind:
            raise ValueError("History event kind cannot be empty")
        event = HistoryEvent(
            at=_timestamp(at),
            kind=kind,
            severity=severity,
            data=dict(data or {}),
        )
        if any(not isinstance(key, str) or not _is_scalar(value) for key, value in event.data.items()):
            raise TypeError("History event data must contain scalar values")
        self._append_event(self._current, event)
        if (summary_key := _SUMMARY_EVENTS.get(kind)) is not None:
            self._current.summary[summary_key] = self._current.summary.get(summary_key, 0) + 1
        self._save()
        return event

    def increment(self, key: str, amount: int = 1) -> None:
        if self._current is None:
            raise RuntimeError("A session must be started before updating history")
        if not key or not isinstance(amount, int) or isinstance(amount, bool):
            raise ValueError("History summary updates must use a key and integer amount")
        self._current.summary[key] = self._current.summary.get(key, 0) + amount
        self._save()

    def set_retention_days(self, days: int) -> None:
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            raise ValueError("History retention must be positive")
        self.retention_days = days
        self._trim()
        self._save()

    def clear(self, *, keep_current: bool = True) -> None:
        self._load()
        current_id = self._current.id if keep_current and self._current is not None else None
        self._sessions = [
            session for session in self._sessions if current_id is not None and session.id == current_id
        ]
        self._save()

    def finish(
        self,
        status: Literal["stopped", "failed"] = "stopped",
        *,
        reason: str | None = None,
        at: datetime | None = None,
    ) -> SessionRecord | None:
        session = self._current
        if session is None:
            return None
        stamp = _timestamp(at)
        event_kind = "session.failed" if status == "failed" else "session.stopped"
        event_data: dict[str, Scalar] = {"reason": reason} if reason else {}
        self._append_event(session, HistoryEvent(stamp, event_kind, "error" if status == "failed" else "info", event_data))
        session.ended_at = stamp
        session.status = status
        self._current = None
        self._save()
        return session

    def _append_event(self, session: SessionRecord, event: HistoryEvent) -> None:
        session.events.append(event)
        if len(session.events) > self.max_events_per_session:
            del session.events[: -self.max_events_per_session]

    def _trim(self, *, now: datetime | None = None) -> None:
        del self._sessions[self.max_sessions :]
        if self.retention_days is None:
            return
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        cutoff = reference.astimezone(timezone.utc) - timedelta(days=self.retention_days)
        self._sessions = [
            session
            for session in self._sessions
            if session.status == "running"
            or (started := _parse_timestamp(session.started_at)) is None
            or started >= cutoff
        ]

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        candidates = (self.path.with_name(f"{self.path.name}.new"), self.path)
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                with candidate.open("r", encoding="utf8") as file:
                    value = json.load(file)
                if not isinstance(value, Mapping) or value.get("version") != self.VERSION:
                    raise ValueError("Unsupported history format")
                raw_sessions = value.get("sessions")
                if not isinstance(raw_sessions, list):
                    raise ValueError("History sessions must be a list")
                self._sessions = [
                    session
                    for item in raw_sessions
                    if (session := SessionRecord.from_json(item)) is not None
                ]
                self._trim()
                return
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning("Unable to load session history from %s: %s", candidate, type(exc).__name__)
        self._sessions = []

    def _save(self) -> None:
        payload = {
            "version": self.VERSION,
            "sessions": [session.to_json() for session in self._sessions],
        }

        def writer(path: Path) -> None:
            with path.open("w", encoding="utf8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")

        try:
            atomic_write(self.path, writer, mode=0o600)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Unable to persist session history: %s", type(exc).__name__)
