from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from session_history import SessionHistory


class SessionHistoryTests(unittest.TestCase):
    def make_history(self, directory: str, **kwargs: int) -> SessionHistory:
        return SessionHistory(Path(directory) / "session_history.json", **kwargs)

    def test_session_and_events_are_persisted_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.make_history(directory)
            started = history.start(at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            history.record(
                "auth.restored",
                at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
                data={"source": "cookie"},
            )
            history.record(
                "claim.succeeded",
                at=datetime(2026, 1, 1, 0, 1, 30, tzinfo=timezone.utc),
            )
            finished = history.finish(
                at=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
                reason="user_requested",
            )

            self.assertIsNotNone(finished)
            assert finished is not None
            self.assertEqual(finished.id, started.id)
            self.assertEqual(finished.status, "stopped")
            self.assertEqual(finished.summary["claims_succeeded"], 1)
            self.assertEqual([event.kind for event in finished.events], [
                "auth.restored",
                "claim.succeeded",
                "session.stopped",
            ])

            path = Path(directory) / "session_history.json"
            self.assertTrue(path.exists())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = SessionHistory(path)
            self.assertEqual(loaded.sessions[0].id, started.id)
            self.assertEqual(loaded.sessions[0].events[0].data["source"], "cookie")
            self.assertFalse(path.with_name("session_history.json.new").exists())

    def test_new_start_marks_previous_running_session_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            first = SessionHistory(path)
            first.start(at=datetime(2026, 1, 1, tzinfo=timezone.utc))

            second = SessionHistory(path)
            current = second.start(at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))

            previous = second.sessions[1]
            self.assertEqual(previous.status, "interrupted")
            self.assertEqual(previous.ended_at, "2026-01-01T01:00:00.000Z")
            self.assertEqual(previous.events[-1].kind, "session.interrupted")
            self.assertEqual(second.current, current)
            second.finish(at=datetime(2026, 1, 1, 2, tzinfo=timezone.utc))

    def test_session_and_event_limits_keep_history_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.make_history(directory, max_sessions=2, max_events_per_session=2)
            first = history.start()
            history.record("first")
            history.record("second")
            history.record("third")
            history.finish()
            second = history.start()
            history.record("fourth")
            history.finish()
            third = history.start()
            history.finish()

            self.assertEqual(len(history.sessions), 2)
            self.assertEqual(history.sessions[0].id, third.id)
            self.assertEqual(history.sessions[1].id, second.id)
            self.assertLessEqual(len(history.sessions[1].events), 2)
            self.assertNotIn(first.id, {session.id for session in history.sessions})

    def test_retention_removes_old_completed_sessions_but_keeps_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.make_history(directory, retention_days=30)
            old = history.start(at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            history.finish(at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
            current = history.start(at=datetime(2026, 2, 15, tzinfo=timezone.utc))

            self.assertEqual([session.id for session in history.sessions], [current.id])
            self.assertNotIn(old.id, {session.id for session in history.sessions})

    def test_clear_without_current_explicitly_detaches_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.make_history(directory)
            history.start()

            history.clear(keep_current=False)

            self.assertIsNone(history.current)
            self.assertEqual(history.sessions, ())
            with self.assertRaises(RuntimeError):
                history.record("after.clear")

    def test_clear_keeps_current_session_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.make_history(directory)
            old = history.start()
            history.finish()
            current = history.start()

            history.clear()

            self.assertEqual([session.id for session in history.sessions], [current.id])
            self.assertNotIn(old.id, {session.id for session in history.sessions})

    def test_loaded_event_lists_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            events = [
                {
                    "at": f"2026-01-01T00:00:0{index}.000Z",
                    "kind": f"event-{index}",
                    "severity": "info",
                    "data": {},
                }
                for index in range(5)
            ]
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sessions": [
                            {
                                "id": "session",
                                "started_at": "2026-01-01T00:00:00.000Z",
                                "ended_at": "2026-01-01T00:01:00.000Z",
                                "status": "stopped",
                                "summary": {},
                                "events": events,
                            }
                        ],
                    }
                ),
                encoding="utf8",
            )

            history = SessionHistory(path, max_events_per_session=2)

            self.assertEqual(
                [event.kind for event in history.sessions[0].events],
                ["event-3", "event-4"],
            )

    def test_interleaved_instances_preserve_both_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            first = SessionHistory(path)
            first_session = first.start(
                at=datetime(2026, 1, 1, tzinfo=timezone.utc)
            )
            second = SessionHistory(path)
            second_session = second.start(
                at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
            )

            first.record(
                "first.owner.event",
                at=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
            )

            loaded = SessionHistory(path)
            by_id = {session.id: session for session in loaded.sessions}
            self.assertEqual(set(by_id), {first_session.id, second_session.id})
            self.assertIn(
                "first.owner.event",
                [event.kind for event in by_id[first_session.id].events],
            )

    def test_merge_preserves_duplicate_same_millisecond_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            first = SessionHistory(path)
            first.start(at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            event_at = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
            first.record("duplicate", at=event_at)
            first.record("duplicate", at=event_at)

            second = SessionHistory(path)
            self.assertEqual(
                [event.kind for event in second.sessions[0].events].count("duplicate"),
                2,
            )
            first.record("merge.trigger", at=datetime(2026, 1, 1, 2, tzinfo=timezone.utc))

            loaded = SessionHistory(path)
            self.assertEqual(
                [event.kind for event in loaded.sessions[0].events].count("duplicate"),
                2,
            )

    def test_summary_survives_event_retention_and_external_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            first = SessionHistory(path, max_events_per_session=2)
            first.start()
            for _ in range(3):
                first.record("claim.succeeded")
            SessionHistory(path, max_events_per_session=2).sessions
            first.record("merge.trigger")

            loaded = SessionHistory(path, max_events_per_session=2)
            self.assertEqual(loaded.sessions[0].summary["claims_succeeded"], 3)

    def test_invalid_write_values_leave_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.make_history(directory)
            history.start()
            path = Path(directory) / "session_history.json"
            baseline = path.read_bytes()

            with self.assertRaises(ValueError):
                history.record(
                    "invalid",
                    severity=cast(Any, "critical"),
                )
            self.assertEqual(path.read_bytes(), baseline)

            with self.assertRaises(ValueError):
                history.finish(cast(Any, "interrupted"))
            self.assertEqual(path.read_bytes(), baseline)

    def test_unsupported_version_is_preserved_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            original = b'{"version": 99, "sessions": [{"future": true}]}\n'
            path.write_bytes(original)

            history = SessionHistory(path)
            history.start()
            history.record("new-event")
            history.finish()

            self.assertEqual(path.read_bytes(), original)

    def test_invalid_history_does_not_prevent_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            path.write_text("not json", encoding="utf8")

            history = SessionHistory(path)
            session = history.start()

            self.assertEqual(history.current, session)
            self.assertEqual(len(history.sessions), 1)
            try:
                saved = json.loads(path.read_text(encoding="utf8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.fail(f"History was not rewritten as valid JSON: {exc}")
            self.assertIsInstance(saved, dict)
            quarantined = list(path.parent.glob("session_history.json.invalid-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf8"), "not json")
            history.finish()


if __name__ == "__main__":
    unittest.main()
