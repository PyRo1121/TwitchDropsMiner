from __future__ import annotations

import asyncio
import unittest
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from auth import AuthState
from channel_directory_service import ChannelDirectoryService
from channel_event_service import ChannelEventService
from constants import PriorityMode, State
from drop_event_service import DropEventService
from exceptions import MinerException, RequestException
from gui_port import ChannelsPort
from http_transport import HttpTransport
from inventory_service import InventoryService
from twitch import Twitch
from watch_service import WatchService


class _Inventory:
    def __init__(self) -> None:
        self.snapshots: list[list[Any]] = []

    async def replace_campaigns(self, campaigns: list[Any]) -> None:
        self.snapshots.append(campaigns)


class _Gui:
    def __init__(self, _twitch: Twitch) -> None:
        self.close_requested = False
        self.inv = _Inventory()
        self.channels = SimpleNamespace(
            clear_watching=lambda: None,
            set_watching_channels=lambda _channels: None,
        )
        self.authenticated: list[bool] = []

    def display_drop(
        self,
        _drop: object,
        *,
        countdown: bool = True,
        subone: bool = False,
    ) -> None:
        del countdown, subone

    def clear_drop(self) -> None:
        return None

    def set_games(self, _games: set[object]) -> None:
        return None

    def set_authenticated(self, authenticated: bool) -> None:
        self.authenticated.append(authenticated)


class ServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _state_loop_miner(
        sync_state: Callable[[], Awaitable[None]],
    ) -> Twitch:
        miner = cast(Any, Twitch.__new__(Twitch))
        miner.settings = SimpleNamespace(dump=False)
        miner._state = State.IDLE
        miner._state_generation = 0
        miner._state_change = asyncio.Event()
        miner._history_auth_recorded = True
        miner.channels = OrderedDict()
        miner.gui = SimpleNamespace(
            start=lambda: None,
            tray=SimpleNamespace(change_icon=lambda _icon: None),
            status=SimpleNamespace(update=lambda _status: None),
        )
        miner.watch_service = SimpleNamespace(start_session=lambda: None)
        miner.websocket = SimpleNamespace(
            start=AsyncMock(return_value=None),
            add_topics=lambda _topics: None,
        )
        miner.drop_event_service = SimpleNamespace(
            process_drops=AsyncMock(return_value=None),
            process_notifications=AsyncMock(return_value=None),
        )
        miner.inventory_service = SimpleNamespace(sync_state=sync_state)
        miner.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=1))
        return cast(Twitch, miner)

    async def test_state_loop_obeys_inventory_retry_backoff(self) -> None:
        delay_started = asyncio.Event()
        release_delay = asyncio.Event()
        attempts = 0

        async def unused_sync_state() -> None:
            raise AssertionError("real InventoryService must own the retry")

        miner = cast(Any, self._state_loop_miner(unused_sync_state))
        miner.inventory = []
        miner._drops = {}
        miner.history_event = lambda *_args, **_kwargs: None

        async def wait_for_delay(_delay: float) -> None:
            delay_started.set()
            await release_delay.wait()

        async def fail_inventory() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                miner.change_state(State.EXIT)
            raise RequestException("temporary Twitch outage")

        miner.transport = SimpleNamespace(wait_for_delay=wait_for_delay)
        service = cast(Any, InventoryService(miner))
        service.fetch_inventory = fail_inventory
        miner.inventory_service = service
        run_task = asyncio.create_task(miner._run())
        await asyncio.wait_for(delay_started.wait(), timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(attempts, 1)

        release_delay.set()
        await asyncio.wait_for(run_task, timeout=1)
        self.assertEqual(attempts, 2)
        await service.close()

    async def test_state_loop_preserves_a_transition_requested_during_await(self) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        attempts = 0
        miner: Twitch

        async def sync_state() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                first_started.set()
                await release_first.wait()
            else:
                miner.change_state(State.EXIT)

        miner = self._state_loop_miner(sync_state)
        run_task = asyncio.create_task(miner._run())
        await asyncio.wait_for(first_started.wait(), timeout=1)
        miner.change_state(State.INVENTORY_FETCH)
        release_first.set()

        await asyncio.wait_for(run_task, timeout=1)
        self.assertEqual(attempts, 2)

    async def test_failed_game_directory_does_not_abort_other_games(self) -> None:
        class GameStub:
            def __init__(self, name: str) -> None:
                self.name = name

        directory = cast(
            Any,
            ChannelDirectoryService(cast(Any, SimpleNamespace())),
        )
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

        directory.get_live_streams = get_live_streams

        channels = await directory.fetch_live_streams_for_games(
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
        miner.gui = SimpleNamespace(
            tray=SimpleNamespace(change_icon=lambda _icon: None),
            set_games=lambda _games: None,
        )
        miner.watch_service = SimpleNamespace(stop_watching=lambda: None)
        miner.websocket = SimpleNamespace(start=AsyncMock(return_value=None))
        miner.history_event = lambda *_args, **_kwargs: None
        miner.save = lambda: None

        async def fetch_inventory() -> None:
            miner.change_state(State.INVENTORY_FETCH)

        service = cast(Any, InventoryService(miner))
        service.fetch_inventory = fetch_inventory
        stale_retry = asyncio.create_task(asyncio.Event().wait())
        service._retry_task = stale_retry
        miner.inventory_service = service

        await service.sync_state()

        self.assertIs(miner._state, State.INVENTORY_FETCH)
        self.assertEqual(miner._state_generation, 2)
        self.assertTrue(stale_retry.done())
        self.assertIsNone(service._retry_task)

    async def test_transient_inventory_failure_preserves_snapshot_and_retries(self) -> None:
        miner = cast(Any, Twitch.__new__(Twitch))
        miner._state = State.INVENTORY_FETCH
        miner._state_generation = 1
        miner._state_change = asyncio.Event()
        old_inventory = [object()]
        old_drops = {"old": object()}
        miner.inventory = old_inventory
        miner._drops = old_drops
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
        service = cast(Any, InventoryService(miner))
        service.fetch_inventory = fail_inventory
        miner.inventory_service = service
        miner.history_event = lambda kind, **_kwargs: events.append(kind)

        await service.sync_state()
        await asyncio.wait_for(delay_started.wait(), timeout=1)
        try:
            self.assertIs(miner.inventory, old_inventory)
            self.assertIs(miner._drops, old_drops)
            self.assertEqual(events, ["inventory.sync_failed"])
            self.assertIn("retrying", statuses[-1].lower())
            self.assertIsNotNone(service._retry_task)
            self.assertFalse(cast(asyncio.Task[Any], service._retry_task).done())
        finally:
            await service.close()
            self.assertIsNone(service._retry_task)
            self.assertEqual(service._retry_attempt, 0)

    def test_watch_service_owns_idle_and_channel_switch_policy(self) -> None:
        selected_channel = SimpleNamespace(id=7, name="selected")
        state_change = asyncio.Event()
        state_change.set()
        gui = SimpleNamespace(
            close=Mock(),
            clear_drop=Mock(),
            tray=SimpleNamespace(change_icon=Mock()),
            status=SimpleNamespace(update=Mock()),
            channels=SimpleNamespace(
                get_selection=Mock(return_value=selected_channel),
                clear_watching=Mock(),
            ),
        )
        miner = cast(
            Any,
            SimpleNamespace(
                settings=SimpleNamespace(
                    dump=False,
                    experimental_dual_watch=False,
                ),
                gui=gui,
                _state_change=state_change,
                channel_directory_service=SimpleNamespace(
                    get_priority=Mock(return_value=0),
                ),
                print=Mock(),
                history_event=Mock(),
                change_state=Mock(),
            ),
        )
        service = cast(Any, WatchService(miner))
        service.can_watch = Mock(return_value=True)
        service.watch = Mock()

        self.assertFalse(
            service.switch_channel(
                cast(OrderedDict[int, Any], OrderedDict(((7, selected_channel),)))
            )
        )
        service.watch.assert_called_once_with(selected_channel)
        self.assertTrue(state_change.is_set())

        self.assertFalse(service.handle_idle_state())
        gui.tray.change_icon.assert_called_with("idle")
        gui.clear_drop.assert_called_once_with()
        self.assertTrue(state_change.is_set())
        self.assertFalse(hasattr(Twitch, "_handle_idle_state"))
        self.assertFalse(hasattr(Twitch, "_switch_channel"))
        self.assertFalse(hasattr(service, "_reconcile_watch_progress"))
        self.assertFalse(hasattr(service, "_claim_cooldowns"))
        self.assertTrue(hasattr(service.progress, "reconcile"))
        self.assertTrue(hasattr(service.progress, "_claim_cooldowns"))
        self.assertFalse(hasattr(ChannelsPort, "set_watching"))

    async def test_game_selection_is_atomic_and_does_not_reorder_inventory(self) -> None:
        class GameStub:
            def __init__(self, name: str) -> None:
                self.name = name

        game_a = GameStub("A")
        game_b = GameStub("B")
        game_c = GameStub("C")
        claimable = SimpleNamespace(
            id="drop",
            can_claim=True,
            claim=AsyncMock(return_value=True),
        )
        campaign_a = SimpleNamespace(
            game=game_a,
            upcoming=False,
            drops=[claimable],
            ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
            availability=10,
            can_earn_within=Mock(return_value=True),
        )
        campaign_c = SimpleNamespace(
            game=game_c,
            upcoming=False,
            drops=[],
            ends_at=datetime.now(timezone.utc) + timedelta(hours=2),
            availability=20,
            can_earn_within=Mock(return_value=True),
        )
        campaign_b = SimpleNamespace(
            game=game_b,
            upcoming=False,
            drops=[],
            ends_at=datetime.now(timezone.utc) + timedelta(hours=3),
            availability=30,
            can_earn_within=Mock(return_value=True),
        )
        inventory = [campaign_a, campaign_c, campaign_b]
        completed = Mock()
        miner = cast(
            Any,
            SimpleNamespace(
                inventory=inventory,
                wanted_games=[GameStub("old")],
                settings=SimpleNamespace(
                    exclude=["C"],
                    priority=["B"],
                    priority_mode=PriorityMode.ENDING_SOONEST,
                ),
                watch_service=SimpleNamespace(
                    progress=SimpleNamespace(mark_completed_drop=completed),
                ),
            ),
        )
        service = InventoryService(miner)

        await service.update_wanted_games()

        self.assertEqual(miner.wanted_games, [game_b, game_a])
        self.assertEqual(inventory, [campaign_a, campaign_c, campaign_b])
        claimable.claim.assert_awaited_once_with()
        completed.assert_called_once_with("drop")

    def test_campaign_deadline_alerts_are_deduplicated_per_session(self) -> None:
        events: list[str] = []
        campaign = SimpleNamespace(
            id="campaign",
            name="Campaign",
            game=SimpleNamespace(name="Game"),
            ends_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            finished=False,
            active=True,
        )
        miner = cast(
            Any,
            SimpleNamespace(
                inventory=[campaign],
                history_event=lambda kind, **_kwargs: events.append(kind),
            ),
        )
        service = InventoryService(miner)

        service._record_campaign_deadlines()
        service._record_campaign_deadlines()
        self.assertEqual(events, ["campaign.deadline"])

        service.start_session()
        service._record_campaign_deadlines()
        self.assertEqual(events, ["campaign.deadline", "campaign.deadline"])

    def test_coordinator_does_not_own_inventory_retry_lifecycle(self) -> None:
        self.assertFalse(hasattr(Twitch, "_retry_inventory_after"))
        self.assertFalse(hasattr(Twitch, "_fetch_inventory_state"))
        self.assertFalse(hasattr(Twitch, "_record_campaign_deadlines"))
        self.assertFalse(hasattr(Twitch, "_update_games_state"))
        self.assertFalse(hasattr(Twitch, "_mnt_task"))
        self.assertFalse(hasattr(WatchService, "_maintenance_task"))
        self.assertTrue(hasattr(InventoryService, "restart_maintenance"))

    async def test_inventory_to_shutdown_smoke_has_no_loop_errors(self) -> None:
        loop_errors: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        try:
            settings = SimpleNamespace(
                history_retention_days=90,
                experimental_dual_watch=False,
            )
            miner = Twitch(
                cast(Any, settings),
                gui_factory=cast(Any, _Gui),
            )
            self.assertIsInstance(miner.transport, HttpTransport)
            self.assertIsInstance(miner._auth_state, AuthState)
            self.assertIsInstance(miner.drop_event_service, DropEventService)
            self.assertIsInstance(miner.channel_event_service, ChannelEventService)
            self.assertIsInstance(
                miner.channel_directory_service,
                ChannelDirectoryService,
            )
            gui = cast(_Gui, miner.gui)

            statuses: list[str] = []
            await miner.inventory_service._install_inventory(
                [],
                statuses.append,
            )
            inventory = cast(_Inventory, miner.gui.inv)
            self.assertEqual(inventory.snapshots, [[]])
            self.assertTrue(miner.inventory_service.maintenance_running)
            async def wait_forever() -> None:
                await asyncio.Event().wait()

            retry_task = asyncio.create_task(wait_forever())
            miner.inventory_service._retry_task = retry_task

            with patch("twitch.asyncio.sleep", new=AsyncMock(return_value=None)):
                await miner.shutdown()
            await asyncio.sleep(0)

            self.assertEqual(miner.inventory, [])
            self.assertEqual(miner._drops, {})
            self.assertTrue(retry_task.done())
            self.assertIsNone(miner.inventory_service._retry_task)
            self.assertFalse(miner.inventory_service.maintenance_running)
            self.assertEqual(inventory.snapshots, [[], ()])
            self.assertEqual(loop_errors, [])
            self.assertEqual(gui.authenticated, [False])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_shutdown_pauses_and_stops_producers_before_consumers(self) -> None:
        events: list[str] = []

        class ChannelStub:
            def __init__(self) -> None:
                self._pending_stream_up: asyncio.Task[None] | None = None

            def remove(self) -> None:
                events.append("channel.remove")
                if self._pending_stream_up is not None:
                    self._pending_stream_up.cancel()
                    self._pending_stream_up = None

        channel = ChannelStub()

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        class WebsocketStub:
            def __init__(self) -> None:
                self.paused = False
                self.resurrection_attempts = 0

            async def pause_topic_dispatch(self) -> None:
                events.append("websocket.pause")
                self.paused = True
                await asyncio.sleep(0)

            async def stop(self, *, clear_topics: bool) -> None:
                self.assert_clear_topics(clear_topics)
                events.append("websocket.stop")
                await asyncio.sleep(0)
                self.resurrection_attempts += 1
                if not self.paused:
                    channel._pending_stream_up = asyncio.create_task(wait_forever())

            async def resume_topic_dispatch(self) -> None:
                events.append("websocket.resume")
                self.paused = False

            @staticmethod
            def assert_clear_topics(clear_topics: bool) -> None:
                if not clear_topics:
                    raise AssertionError("shutdown must clear websocket topics")

        async def inventory_close() -> None:
            events.append("inventory.close")

        async def watch_close() -> None:
            events.append("watch.close")

        async def transport_close() -> None:
            events.append("transport.close")

        async def replace_campaigns(_campaigns: object) -> None:
            events.append("inventory.clear")

        miner = cast(Any, Twitch.__new__(Twitch))
        websocket = WebsocketStub()
        miner._inventory_generation = 1
        miner.websocket = websocket
        miner.inventory_service = SimpleNamespace(close=inventory_close)
        miner.watch_service = SimpleNamespace(close=watch_close)
        miner.channels = OrderedDict(((1, channel),))
        miner.transport = SimpleNamespace(close=transport_close)
        miner.gui = SimpleNamespace(
            inv=SimpleNamespace(replace_campaigns=replace_campaigns),
            set_games=lambda _games: None,
        )
        miner._drops = {}
        miner.inventory = []
        miner._campaigns = {}
        miner._auth_state = SimpleNamespace(clear=lambda: None)
        miner.wanted_games = []

        with patch("twitch.asyncio.sleep", new=AsyncMock(return_value=None)):
            await miner.shutdown()

        self.assertEqual(websocket.resurrection_attempts, 1)
        self.assertIsNone(channel._pending_stream_up)
        self.assertEqual(
            events,
            [
                "websocket.pause",
                "websocket.stop",
                "inventory.close",
                "watch.close",
                "channel.remove",
                "transport.close",
                "inventory.clear",
                "websocket.resume",
            ],
        )


if __name__ == "__main__":
    unittest.main()
