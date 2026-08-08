from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
            history.increment("claims_succeeded")
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

    def test_clear_keeps_current_session_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.make_history(directory)
            old = history.start()
            history.finish()
            current = history.start()

            history.clear()

            self.assertEqual([session.id for session in history.sessions], [current.id])
            self.assertNotIn(old.id, {session.id for session in history.sessions})

    def test_invalid_history_does_not_prevent_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_history.json"
            path.write_text("not json", encoding="utf8")

            history = SessionHistory(path)
            session = history.start()

            self.assertEqual(history.current, session)
            self.assertEqual(len(history.sessions), 1)
            self.assertIsInstance(json.loads(path.read_text(encoding="utf8")), dict)
            history.finish()


if __name__ == "__main__":
    unittest.main()
