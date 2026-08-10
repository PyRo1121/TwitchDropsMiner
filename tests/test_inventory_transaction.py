from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from exceptions import (
    ExitRequest,
    InventoryPresentationError,
    RequestException,
)
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


class _PresentationAdapter:
    def __init__(
        self,
        adapter: _InventoryAdapter,
        campaigns: tuple[object, ...],
    ) -> None:
        self._adapter = adapter
        self._campaigns = campaigns
        self._previous = adapter.current
        self._committed = False

    def commit(self) -> None:
        self._adapter.current = self._campaigns
        self._committed = True
        if self._adapter.fail_commit:
            raise OSError("presentation commit failed")

    def rollback(self) -> None:
        self._adapter.rollback_attempts += 1
        if self._adapter.fail_rollback:
            raise OSError("presentation rollback failed")
        self._adapter.current = self._previous

    def finalize(self) -> None:
        if not self._committed:
            raise AssertionError("presentation was not committed")
        self._adapter.replacements.append(self._campaigns)


class _InventoryAdapter:
    def __init__(
        self,
        *,
        fail_commit: bool = False,
        fail_rollback: bool = False,
    ) -> None:
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.attempts: list[tuple[object, ...]] = []
        self.replacements: list[tuple[object, ...]] = []
        self.current: tuple[object, ...] = ()
        self.rollback_attempts = 0

    async def stage_campaigns(
        self,
        campaigns: list[object],
    ) -> _PresentationAdapter:
        replacement = tuple(campaigns)
        self.attempts.append(replacement)
        await asyncio.sleep(0)
        return _PresentationAdapter(self, replacement)

    async def replace_campaigns(self, campaigns: list[object]) -> None:
        presentation = await self.stage_campaigns(campaigns)
        presentation.commit()
        presentation.finalize()


class _WebsocketAdapter:
    def __init__(self) -> None:
        self.paused = False
        self.pauses = 0
        self.resumes = 0

    def topic_dispatch_lease(self, _policy: object) -> object:
        owner = self

        class Lease:
            async def __aenter__(self) -> object:
                owner.paused = True
                owner.pauses += 1
                await asyncio.sleep(0)
                return self

            async def __aexit__(self, *_args: object) -> None:
                owner.paused = False
                owner.resumes += 1

        return Lease()


class _WatchAdapter:
    def __init__(self, claim_cooldowns: dict[str, float]) -> None:
        self.quiesces = 0
        self.resumes = 0
        self.claim_cooldowns = claim_cooldowns
        self.progress = self
        self.active = True

    async def quiesce(self) -> None:
        self.quiesces += 1
        self.active = False
        await asyncio.sleep(0)

    def resume(self) -> None:
        self.resumes += 1
        self.active = True

    def retain_claim_cooldowns(self, drops: dict[str, Any]) -> None:
        self.claim_cooldowns = {
            drop_id: blocked_until
            for drop_id, blocked_until in self.claim_cooldowns.items()
            if drop_id in drops and not drops[drop_id].is_claimed
        }


class _ProbeAdapter:
    def __init__(self) -> None:
        self.quiesces = 0
        self.resumes = 0
        self.quiesced = False

    async def quiesce_probes(self, *, restart: bool) -> None:
        if not restart:
            raise AssertionError("inventory probes must be restartable")
        self.quiesces += 1
        self.quiesced = True
        await asyncio.sleep(0)

    def resume_probes(self, *, restart: bool) -> None:
        if not restart:
            raise AssertionError("inventory probes must be restarted")
        self.resumes += 1
        self.quiesced = False


class InventoryTransactionTests(unittest.IsolatedAsyncioTestCase):
    def _miner(self, adapter: _InventoryAdapter) -> tuple[Twitch, object, object]:
        miner = Twitch.__new__(Twitch)
        old_drop = _Drop("old-drop")
        old_campaign = _Campaign("old-campaign", old_drop)
        miner._drops = {old_drop.id: cast(Any, old_drop)}
        miner.inventory = [cast(Any, old_campaign)]
        miner._campaigns = {old_campaign.id: cast(Any, old_campaign)}
        miner._inventory_generation = 1
        adapter.current = (old_campaign,)
        websocket = _WebsocketAdapter()
        miner.websocket = cast(Any, websocket)
        miner.gui = cast(
            Any,
            SimpleNamespace(inv=adapter, close_requested=False),
        )
        miner.inventory_service = InventoryService(miner)
        miner.watch_service = cast(
            Any,
            _WatchAdapter({old_drop.id: 99999999999.0}),
        )
        miner.channel_directory_service = cast(Any, _ProbeAdapter())
        miner.history_event = lambda *_args, **_kwargs: None
        return miner, old_drop, old_campaign

    async def test_failed_presentation_restores_last_good_snapshot(self) -> None:
        adapter = _InventoryAdapter(fail_commit=True)
        miner, old_drop, old_campaign = self._miner(adapter)
        new_campaign = _Campaign("new-campaign", _Drop("new-drop"))

        with self.assertRaisesRegex(
            InventoryPresentationError,
            "last-good state restored",
        ):
            await miner.inventory_service._install_inventory(
                cast(list[DropsCampaign], [new_campaign]),
                lambda _status: None,
            )

        self.assertEqual(adapter.attempts, [(new_campaign,)])
        self.assertEqual(adapter.current, (old_campaign,))
        self.assertEqual(adapter.rollback_attempts, 1)
        self.assertEqual(miner.inventory, [old_campaign])
        self.assertEqual(miner._campaigns, {"old-campaign": old_campaign})
        self.assertEqual(miner._drops, {"old-drop": old_drop})
        self.assertEqual(miner._inventory_generation, 1)
        watch = cast(_WatchAdapter, miner.watch_service)
        self.assertEqual((watch.quiesces, watch.resumes), (1, 1))
        await miner.inventory_service.close()

    async def test_double_gui_failure_is_a_controlled_fatal_outcome(self) -> None:
        adapter = _InventoryAdapter(fail_commit=True, fail_rollback=True)
        miner, old_drop, old_campaign = self._miner(adapter)
        new_campaign = _Campaign("new-campaign", _Drop("new-drop"))

        with self.assertRaisesRegex(
            InventoryPresentationError,
            "rollback failed",
        ):
            await miner.inventory_service._install_inventory(
                cast(list[DropsCampaign], [new_campaign]),
                lambda _status: None,
            )

        self.assertEqual(adapter.rollback_attempts, 1)
        self.assertEqual(miner.inventory, [old_campaign])
        self.assertEqual(miner._campaigns, {"old-campaign": old_campaign})
        self.assertEqual(miner._drops, {"old-drop": old_drop})
        self.assertEqual(miner._inventory_generation, 1)
        websocket = cast(_WebsocketAdapter, miner.websocket)
        self.assertFalse(websocket.paused)
        self.assertEqual((websocket.pauses, websocket.resumes), (1, 1))
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

    async def test_cancellation_during_probe_quiescence_restores_owners(self) -> None:
        adapter = _InventoryAdapter()
        miner, old_drop, old_campaign = self._miner(adapter)
        quiesce_started = asyncio.Event()

        class BlockingProbes(_ProbeAdapter):
            async def quiesce_probes(self, *, restart: bool) -> None:
                if not restart:
                    raise AssertionError("inventory probes must be restartable")
                self.quiesces += 1
                self.quiesced = True
                quiesce_started.set()
                await asyncio.Event().wait()

        probes = BlockingProbes()
        miner.channel_directory_service = cast(Any, probes)
        new_campaign = _Campaign("new-campaign", _Drop("new-drop"))
        install = asyncio.create_task(
            miner.inventory_service._install_inventory(
                cast(list[DropsCampaign], [new_campaign]),
                lambda _status: None,
            )
        )
        await asyncio.wait_for(quiesce_started.wait(), timeout=1)
        install.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await install

        self.assertEqual(adapter.current, (old_campaign,))
        self.assertEqual(miner.inventory, [old_campaign])
        self.assertEqual(miner._drops, {"old-drop": old_drop})
        self.assertEqual((probes.quiesces, probes.resumes), (1, 1))
        self.assertFalse(probes.quiesced)
        websocket = cast(_WebsocketAdapter, miner.websocket)
        self.assertEqual((websocket.pauses, websocket.resumes), (1, 1))
        self.assertFalse(websocket.paused)
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
            self.assertEqual((watch.quiesces, watch.resumes), (1, 1))
            self.assertTrue(watch.active)
            probes = cast(_ProbeAdapter, miner.channel_directory_service)
            self.assertEqual((probes.quiesces, probes.resumes), (1, 1))
            self.assertFalse(probes.quiesced)
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
