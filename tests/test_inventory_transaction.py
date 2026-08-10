from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from exceptions import ExitRequest, RequestException
from inventory import DropsCampaign
from inventory_service import InventoryService
from inventory_snapshot import (
    parse_available_campaigns,
    parse_inventory_snapshot,
    prepare_inventory,
)
from twitch import Twitch


class _Drop:
    def __init__(self, drop_id: str, *, claimed: bool = False) -> None:
        self.id = drop_id
        self.is_claimed = claimed


class _Campaign:
    def __init__(self, campaign_id: str, *drops: _Drop) -> None:
        self.id = campaign_id
        self.drops = drops
        self.time_triggers = {
            datetime.now(timezone.utc) + timedelta(minutes=30),
        }

    def can_earn_within(self, _stamp: datetime) -> bool:
        return True


class _InventoryAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.attempts: list[tuple[object, ...]] = []
        self.replacements: list[tuple[object, ...]] = []

    async def replace_campaigns(self, campaigns: list[object]) -> None:
        replacement = tuple(campaigns)
        self.attempts.append(replacement)
        if self.fail:
            raise OSError("presentation failed")
        self.replacements.append(replacement)


class _WebsocketAdapter:
    def __init__(self) -> None:
        self.paused = False
        self.pauses = 0
        self.resumes = 0

    async def pause_topic_dispatch(self) -> None:
        self.paused = True
        self.pauses += 1
        await asyncio.sleep(0)

    async def resume_topic_dispatch(self) -> None:
        self.paused = False
        self.resumes += 1

    def dispatch(self, callback: Callable[[], None]) -> bool:
        if self.paused:
            return False
        callback()
        return True


class _WatchAdapter:
    def __init__(
        self,
        claim_cooldowns: dict[str, float],
        websocket: _WebsocketAdapter,
    ) -> None:
        self.stops = 0
        self.claim_cooldowns = claim_cooldowns
        self.progress = self
        self.websocket = websocket
        self.active = True
        self.resurrection_attempts = 0

    async def stop_watching_and_wait(self) -> None:
        self.stops += 1
        self.active = False
        await asyncio.sleep(0)
        self.resurrection_attempts += 1
        self.websocket.dispatch(self._resurrect)

    def _resurrect(self) -> None:
        self.active = True

    def retain_claim_cooldowns(self, drops: dict[str, Any]) -> None:
        self.claim_cooldowns = {
            drop_id: blocked_until
            for drop_id, blocked_until in self.claim_cooldowns.items()
            if drop_id in drops and not drops[drop_id].is_claimed
        }


class InventoryTransactionTests(unittest.IsolatedAsyncioTestCase):
    def _miner(self, adapter: _InventoryAdapter) -> tuple[Twitch, object, object]:
        miner = Twitch.__new__(Twitch)
        old_drop = _Drop("old-drop")
        old_campaign = _Campaign("old-campaign", old_drop)
        miner._drops = {old_drop.id: cast(Any, old_drop)}
        miner.inventory = [cast(Any, old_campaign)]
        miner._campaigns = {old_campaign.id: cast(Any, old_campaign)}
        miner._inventory_generation = 1
        websocket = _WebsocketAdapter()
        miner.websocket = cast(Any, websocket)
        miner.gui = cast(
            Any,
            SimpleNamespace(inv=adapter, close_requested=False),
        )
        miner.inventory_service = InventoryService(miner)
        miner.watch_service = cast(
            Any,
            _WatchAdapter({old_drop.id: 99999999999.0}, websocket),
        )
        miner.history_event = lambda *_args, **_kwargs: None
        return miner, old_drop, old_campaign

    async def test_failed_presentation_does_not_roll_back_core_snapshot(self) -> None:
        adapter = _InventoryAdapter(fail=True)
        miner, _, _ = self._miner(adapter)
        new_drop = _Drop("new-drop")
        new_campaign = _Campaign("new-campaign", new_drop)

        await miner.inventory_service._install_inventory(
            cast(list[DropsCampaign], [new_campaign]),
            lambda _status: None,
        )
        try:
            self.assertEqual(adapter.attempts, [(new_campaign,), ()])
            self.assertEqual(miner.inventory, [new_campaign])
            self.assertEqual(miner._campaigns, {"new-campaign": new_campaign})
            self.assertEqual(miner._drops, {"new-drop": new_drop})
            self.assertEqual(cast(_WatchAdapter, miner.watch_service).stops, 1)
        finally:
            await miner.inventory_service.close()

    async def test_aborted_install_releases_topic_dispatch_barrier(self) -> None:
        miner, _, _ = self._miner(_InventoryAdapter())
        cast(Any, miner.gui).close_requested = True
        new_campaign = _Campaign("new-campaign", _Drop("new-drop"))

        with self.assertRaises(ExitRequest):
            await miner.inventory_service._install_inventory(
                cast(list[DropsCampaign], [new_campaign]),
                lambda _status: None,
            )

        websocket = cast(_WebsocketAdapter, miner.websocket)
        self.assertEqual(websocket.pauses, 1)
        self.assertEqual(websocket.resumes, 1)
        self.assertFalse(websocket.paused)
        self.assertFalse(miner.inventory_service.maintenance_running)
        await miner.inventory_service.close()

    async def test_successful_presentation_commits_complete_snapshot(self) -> None:
        adapter = _InventoryAdapter()
        miner, _, _ = self._miner(adapter)
        new_drop = _Drop("new-drop")
        new_campaign = _Campaign("new-campaign", new_drop)
        statuses: list[str] = []

        await miner.inventory_service._install_inventory(
            cast(list[DropsCampaign], [new_campaign]),
            statuses.append,
        )
        try:
            self.assertEqual(adapter.replacements, [(new_campaign,)])
            self.assertEqual(miner.inventory, [new_campaign])
            self.assertEqual(miner._campaigns, {"new-campaign": new_campaign})
            self.assertEqual(miner._drops, {"new-drop": new_drop})
            self.assertEqual(miner._inventory_generation, 2)
            websocket = cast(_WebsocketAdapter, miner.websocket)
            watch = cast(_WatchAdapter, miner.watch_service)
            self.assertEqual(websocket.pauses, 1)
            self.assertEqual(websocket.resumes, 1)
            self.assertFalse(websocket.paused)
            self.assertEqual(watch.resurrection_attempts, 1)
            self.assertFalse(watch.active)
            self.assertTrue(miner.inventory_service.maintenance_running)
            self.assertFalse(hasattr(watch, "_maintenance_task"))
            self.assertNotIn(
                "old-drop",
                cast(_WatchAdapter, miner.watch_service).claim_cooldowns,
            )
            self.assertTrue(statuses[-1].endswith("(1/1)"))
        finally:
            await miner.inventory_service.close()
        self.assertFalse(miner.inventory_service.maintenance_running)

    def test_duplicate_drop_ids_are_rejected_before_presentation(self) -> None:
        miner, _, _ = self._miner(_InventoryAdapter())
        campaigns = [
            _Campaign("campaign-a", _Drop("duplicate")),
            _Campaign("campaign-b", _Drop("duplicate")),
        ]

        with self.assertRaisesRegex(RequestException, "duplicate drop ID"):
            prepare_inventory(cast(list[DropsCampaign], campaigns))

    def test_malformed_remote_lists_are_rejected(self) -> None:
        with self.assertRaisesRegex(RequestException, "campaign list"):
            parse_available_campaigns(
                {"data": {"currentUser": {"dropCampaigns": {}}}}
            )
        with self.assertRaisesRegex(RequestException, "in-progress campaign list"):
            parse_inventory_snapshot(
                {
                    "dropCampaignsInProgress": {},
                    "gameEventDrops": [],
                }
            )
        with self.assertRaisesRegex(RequestException, "claimed-benefit list"):
            parse_inventory_snapshot(
                {
                    "dropCampaignsInProgress": [],
                    "gameEventDrops": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
