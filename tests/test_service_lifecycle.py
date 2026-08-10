from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from auth import AuthState
from channel_event_service import ChannelEventService
from constants import State
from drop_event_service import DropEventService
from exceptions import MinerException, RequestException
from http_transport import HttpTransport
from twitch import Twitch
from utils import cancel_tasks


class _Inventory:
    def __init__(self) -> None:
        self.snapshots: list[list[Any]] = []

    async def replace_campaigns(self, campaigns: list[Any]) -> None:
        self.snapshots.append(campaigns)


class _Gui:
    def __init__(self, _twitch: Twitch) -> None:
        self.close_requested = False
        self.inv = _Inventory()
        self.channels = SimpleNamespace(clear_watching=lambda: None)
        self.authenticated: list[bool] = []

    def clear_drop(self) -> None:
        return None

    def set_games(self, _games: set[object]) -> None:
        return None

    def set_authenticated(self, authenticated: bool) -> None:
        self.authenticated.append(authenticated)


class ServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_game_directory_does_not_abort_other_games(self) -> None:
        class GameStub:
            def __init__(self, name: str) -> None:
                self.name = name

        miner = cast(Any, Twitch.__new__(Twitch))
        expected_channel = cast(Any, object())

        async def get_live_streams(
            game: GameStub,
            *,
            drops_enabled: bool,
        ) -> list[Any]:
            self.assertTrue(drops_enabled)
            if game.name == "failed":
                raise MinerException("temporary directory failure")
            return [expected_channel]

        miner.get_live_streams = get_live_streams

        channels = await miner._fetch_live_streams_for_games(
            [GameStub("failed"), GameStub("working")]
        )

        self.assertEqual(channels, {expected_channel})

    async def test_manual_refresh_is_not_overwritten_by_fetch_completion(self) -> None:
        miner = cast(Any, Twitch.__new__(Twitch))
        miner._state = State.INVENTORY_FETCH
        miner._state_generation = 1
        miner._state_change = asyncio.Event()
        miner.inventory = []
        miner._drops = {}
        miner._inventory_retry_attempt = 0
        miner._inventory_retry_task = None
        miner.gui = SimpleNamespace(
            tray=SimpleNamespace(change_icon=lambda _icon: None),
            set_games=lambda _games: None,
        )
        miner.watch_service = SimpleNamespace(stop_watching=lambda: None)
        miner.websocket = SimpleNamespace(start=AsyncMock(return_value=None))
        miner.history_event = lambda *_args, **_kwargs: None
        miner._record_campaign_deadlines = lambda: None
        miner.save = lambda: None

        async def fetch_inventory() -> None:
            miner.change_state(State.INVENTORY_FETCH)

        miner.inventory_service = SimpleNamespace(fetch_inventory=fetch_inventory)

        await miner._fetch_inventory_state()

        self.assertIs(miner._state, State.INVENTORY_FETCH)
        self.assertEqual(miner._state_generation, 2)

    async def test_transient_inventory_failure_preserves_snapshot_and_retries(self) -> None:
        miner = cast(Any, Twitch.__new__(Twitch))
        miner._state = State.INVENTORY_FETCH
        miner._state_generation = 1
        miner._state_change = asyncio.Event()
        old_inventory = [object()]
        old_drops = {"old": object()}
        miner.inventory = old_inventory
        miner._drops = old_drops
        miner._inventory_retry_attempt = 0
        miner._inventory_retry_task = None
        statuses: list[str] = []
        events: list[str] = []
        delay_started = asyncio.Event()

        async def wait_for_delay(_delay: float) -> None:
            delay_started.set()
            await asyncio.Event().wait()

        async def fail_inventory() -> None:
            raise RequestException("temporary Twitch outage")

        miner.gui = SimpleNamespace(
            tray=SimpleNamespace(change_icon=lambda _icon: None),
            status=SimpleNamespace(update=statuses.append),
        )
        miner.websocket = SimpleNamespace(start=AsyncMock(return_value=None))
        miner.transport = SimpleNamespace(wait_for_delay=wait_for_delay)
        miner.inventory_service = SimpleNamespace(fetch_inventory=fail_inventory)
        miner.history_event = lambda kind, **_kwargs: events.append(kind)

        await miner._fetch_inventory_state()
        await asyncio.wait_for(delay_started.wait(), timeout=1)
        try:
            self.assertIs(miner.inventory, old_inventory)
            self.assertIs(miner._drops, old_drops)
            self.assertEqual(events, ["inventory.sync_failed"])
            self.assertIn("retrying", statuses[-1].lower())
            self.assertIsNotNone(miner._inventory_retry_task)
            self.assertFalse(cast(asyncio.Task[Any], miner._inventory_retry_task).done())
        finally:
            retry_task = miner._inventory_retry_task
            miner._inventory_retry_task = None
            if retry_task is not None:
                await cancel_tasks((retry_task,))

    async def test_inventory_to_shutdown_smoke_has_no_loop_errors(self) -> None:
        loop_errors: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        try:
            settings = SimpleNamespace(history_retention_days=90)
            miner = Twitch(
                cast(Any, settings),
                gui_factory=cast(Any, _Gui),
            )
            self.assertIsInstance(miner.transport, HttpTransport)
            self.assertIsInstance(miner._auth_state, AuthState)
            self.assertIsInstance(miner.drop_event_service, DropEventService)
            self.assertIsInstance(miner.channel_event_service, ChannelEventService)
            gui = cast(_Gui, miner.gui)

            statuses: list[str] = []
            await miner.inventory_service._install_inventory(
                [],
                statuses.append,
            )
            inventory = cast(_Inventory, miner.gui.inv)
            self.assertEqual(inventory.snapshots, [[]])
            self.assertIsNotNone(miner._mnt_task)

            with patch("twitch.asyncio.sleep", new=AsyncMock(return_value=None)):
                await miner.shutdown()
            await asyncio.sleep(0)

            self.assertEqual(miner.inventory, [])
            self.assertEqual(miner._drops, {})
            self.assertEqual(inventory.snapshots, [[], ()])
            self.assertEqual(loop_errors, [])
            self.assertEqual(gui.authenticated, [False])
        finally:
            loop.set_exception_handler(previous_handler)


if __name__ == "__main__":
    unittest.main()
