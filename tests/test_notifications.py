from __future__ import annotations

import unittest

from notifications import NotificationCenter
from session_history import HistoryEvent


class NotificationCenterTests(unittest.TestCase):
    def event(self, kind: str, at: str, **data: str) -> HistoryEvent:
        return HistoryEvent(at=at, kind=kind, data=data)

    def test_claim_alert_waits_for_repeated_unconfirmed_attempt(self) -> None:
        center = NotificationCenter()
        first = self.event(
            "claim.unconfirmed",
            "2026-01-01T00:00:00.000Z",
            game="Game",
            reward="Reward",
        )
        second = self.event(
            "claim.unconfirmed",
            "2026-01-01T00:01:00.000Z",
            game="Game",
            reward="Reward",
        )

        self.assertIsNone(center.handle(first))
        alert = center.handle(second)
        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert.key, "claim.unconfirmed:Game:Reward")
        self.assertEqual(len(center.active), 1)

    def test_cooldown_prevents_repeated_inventory_alerts(self) -> None:
        center = NotificationCenter()
        first = center.handle(self.event("inventory.sync_failed", "2026-01-01T00:00:00.000Z"))
        second = center.handle(self.event("inventory.sync_failed", "2026-01-01T00:05:00.000Z"))
        later = center.handle(self.event("inventory.sync_failed", "2026-01-01T00:31:00.000Z"))

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(later)

    def test_recovery_resolves_active_alert(self) -> None:
        center = NotificationCenter()
        center.handle(self.event("auth.required", "2026-01-01T00:00:00.000Z"))
        self.assertEqual(len(center.active), 1)

        self.assertIsNone(center.handle(self.event("auth.restored", "2026-01-01T00:01:00.000Z")))
        self.assertEqual(center.active, ())

    def test_claim_success_resolves_matching_claim_alert(self) -> None:
        center = NotificationCenter()
        center.handle(self.event("claim.unconfirmed", "2026-01-01T00:00:00.000Z", game="Game", reward="Reward"))
        center.handle(self.event("claim.unconfirmed", "2026-01-01T00:01:00.000Z", game="Game", reward="Reward"))
        center.handle(self.event("claim.succeeded", "2026-01-01T00:02:00.000Z", game="Game", reward="Reward"))

        self.assertEqual(center.active, ())


if __name__ == "__main__":
    unittest.main()
