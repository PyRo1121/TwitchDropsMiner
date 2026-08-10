from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from exceptions import RequestException
from inventory import DropsCampaign


def _stamp(offset_minutes: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    ).isoformat()


def _campaign_data() -> dict[str, Any]:
    return {
        "id": "campaign",
        "name": "Campaign",
        "status": "ACTIVE",
        "startAt": _stamp(-10),
        "endAt": _stamp(60),
        "game": {
            "id": "1",
            "displayName": "Game",
            "boxArtURL": "https://cdn.example/game-285x380.jpg",
        },
        "self": {"isAccountConnected": True},
        "allow": {"isEnabled": False, "channels": []},
        "timeBasedDrops": [
            {
                "id": "drop",
                "name": "Drop",
                "startAt": _stamp(-10),
                "endAt": _stamp(60),
                "benefitEdges": [
                    {
                        "benefit": {
                            "id": "reward",
                            "name": "Reward",
                            "distributionType": "DIRECT_ENTITLEMENT",
                            "imageAssetURL": "https://cdn.example/reward.png",
                        }
                    }
                ],
                "preconditionDrops": [],
                "requiredMinutesWatched": 10,
                "self": {
                    "currentMinutesWatched": 10,
                    "dropInstanceID": "claim",
                    "isClaimed": False,
                },
            }
        ],
    }


class _Transport:
    def __init__(self) -> None:
        self.calls = 0
        self.status = "ELIGIBLE_FOR_ALL"

    async def gql_request(self, _query: object) -> dict[str, object]:
        self.calls += 1
        await asyncio.sleep(0)
        return {
            "data": {
                "claimDropRewards": {
                    "status": self.status,
                }
            }
        }


class _Twitch:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(enable_badges_emotes=True)
        self.transport = _Transport()
        self.user_id: object = 42
        self.history: list[str] = []
        self.prints: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.drop_updates: list[str] = []
        self.outcome_order: list[str] = []
        self.gui = SimpleNamespace(
            inv=SimpleNamespace(update_drop=self._update_drop),
            tray=SimpleNamespace(notify=self._notify),
        )

    async def get_auth(self) -> object:
        return SimpleNamespace(user_id=self.user_id)

    def history_event(self, kind: str, **_kwargs: object) -> None:
        self.history.append(kind)
        self.outcome_order.append("history")

    def print(self, message: str) -> None:
        self.prints.append(message)
        self.outcome_order.append("print")

    def _notify(self, message: str, title: str) -> None:
        self.notifications.append((message, title))
        self.outcome_order.append("notify")

    def _update_drop(self, drop: object) -> None:
        self.drop_updates.append(cast(Any, drop).id)
        self.outcome_order.append("display")


class ClaimSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_claims_share_one_request_and_notification(self) -> None:
        twitch = _Twitch()
        campaign = DropsCampaign(cast(Any, twitch), _campaign_data(), {})
        drop = next(iter(campaign.drops))

        first, second = await asyncio.gather(drop.claim(), drop.claim())

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(drop.is_claimed)
        self.assertEqual(drop.current_minutes, drop.required_minutes)
        self.assertEqual(twitch.transport.calls, 1)
        self.assertEqual(twitch.history, ["claim.succeeded"])
        self.assertEqual(len(twitch.prints), 1)
        self.assertEqual(len(twitch.notifications), 1)
        self.assertEqual(
            twitch.outcome_order,
            ["history", "print", "notify", "display", "display"],
        )

    async def test_unconfirmed_claim_preserves_ready_state_and_reports_once(self) -> None:
        twitch = _Twitch()
        twitch.transport.status = "INELIGIBLE"
        campaign = DropsCampaign(cast(Any, twitch), _campaign_data(), {})
        drop = next(iter(campaign.drops))

        claimed = await drop.claim()

        self.assertFalse(claimed)
        self.assertFalse(drop.is_claimed)
        self.assertTrue(drop.can_claim)
        self.assertEqual(twitch.transport.calls, 1)
        self.assertEqual(twitch.history, ["claim.unconfirmed"])
        self.assertEqual(twitch.prints, [])
        self.assertEqual(twitch.notifications, [])
        self.assertEqual(twitch.outcome_order, ["history", "display"])

    async def test_generated_claim_uses_a_valid_authenticated_user(self) -> None:
        twitch = _Twitch()
        data = _campaign_data()
        data["timeBasedDrops"][0]["self"].pop("dropInstanceID")
        campaign = DropsCampaign(cast(Any, twitch), data, {})
        drop = next(iter(campaign.drops))

        await drop.generate_claim()

        self.assertEqual(drop.claim_id, "42#campaign#drop")
        self.assertTrue(drop.can_claim)

    async def test_generated_claim_rejects_invalid_authenticated_users(self) -> None:
        for user_id in (None, True, 0, -1):
            with self.subTest(user_id=user_id):
                twitch = _Twitch()
                twitch.user_id = user_id
                data = _campaign_data()
                data["timeBasedDrops"][0]["self"].pop("dropInstanceID")
                campaign = DropsCampaign(cast(Any, twitch), data, {})
                drop = next(iter(campaign.drops))

                with self.assertRaisesRegex(RequestException, "user ID"):
                    await drop.generate_claim()

                self.assertIsNone(drop.claim_id)
                self.assertFalse(drop.can_claim)

    def test_claim_instance_state_rejects_empty_ids_and_can_be_cleared(self) -> None:
        twitch = _Twitch()
        campaign = DropsCampaign(cast(Any, twitch), _campaign_data(), {})
        drop = next(iter(campaign.drops))

        with self.assertRaisesRegex(ValueError, "non-empty"):
            drop.update_claim("")

        drop.claim_id = None
        self.assertIsNone(drop.claim_id)
        self.assertFalse(drop.can_claim)
        self.assertFalse(drop.is_claimed)


if __name__ == "__main__":
    unittest.main()
