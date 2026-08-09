from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from inventory import BaseDrop, Benefit


class _SerializedDrop(BaseDrop):
    def __init__(self) -> None:
        raise AssertionError("test constructs this model explicitly")

    async def _claim(self) -> bool:
        self.claim_calls += 1
        await asyncio.sleep(0)
        return True


class ClaimSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_claims_share_one_request_and_notification(self) -> None:
        history: list[str] = []
        prints: list[str] = []
        notifications: list[tuple[str, str]] = []
        twitch = SimpleNamespace(
            history_event=lambda kind, **_kwargs: history.append(kind),
            print=prints.append,
            gui=SimpleNamespace(
                tray=SimpleNamespace(
                    notify=lambda message, title: notifications.append((message, title))
                )
            ),
        )
        campaign = SimpleNamespace(
            id="campaign",
            ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
            game=SimpleNamespace(name="Game"),
            claimed_drops=0,
            total_drops=1,
        )
        drop = _SerializedDrop.__new__(_SerializedDrop)
        drop._claim_lock = asyncio.Lock()
        drop._twitch = cast(Any, twitch)
        drop.campaign = cast(Any, campaign)
        drop.id = "drop"
        drop.name = "Drop"
        drop.claim_id = "claim"
        drop.is_claimed = False
        drop.benefits = [
            Benefit(
                {
                    "benefit": {
                        "id": "reward",
                        "name": "Reward",
                        "distributionType": "DIRECT_ENTITLEMENT",
                        "imageAssetURL": "https://cdn.example/reward.png",
                    }
                }
            )
        ]
        drop.claim_calls = 0

        first, second = await asyncio.gather(drop.claim(), drop.claim())

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(drop.claim_calls, 1)
        self.assertEqual(history, ["claim.succeeded"])
        self.assertEqual(len(prints), 1)
        self.assertEqual(len(notifications), 1)


if __name__ == "__main__":
    unittest.main()
