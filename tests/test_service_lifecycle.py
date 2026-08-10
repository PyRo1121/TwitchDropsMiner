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
from channel import Channel
from channel_directory_service import ChannelDirectoryService
from channel_event_service import ChannelEventService
from constants import PriorityMode, State
from drop_event_service import DropEventService
from exceptions import MinerException, ReloadRequest, RequestException
from gui_port import ChannelsPort
from http_transport import HttpTransport
from inventory_service import InventoryService
from twitch import Twitch, _StateIntentMailbox
from watch_service import WatchService


class _Presentation:
    def __init__(self, inventory: _Inventory, campaigns: list[Any]) -> None:
        self._inventory = inventory
        self._campaigns = campaigns
        self._previous: list[Any] | tuple[Any, ...] | None = None
        self._committed = False

    def commit(self) -> None:
        self._previous = self._inventory.current
        self._inventory.current = self._campaigns
        self._inventory.snapshots.append(self._campaigns)
        self._committed = True

    def rollback(self) -> None:
        if self._committed and self._previous is not None:
            self._inventory.current = self._previous
            self._inventory.snapshots.append(self._previous)

    def finalize(self) -> None:
        return None


class _Inventory:
    def __init__(self) -> None:
        self.snapshots: list[list[Any] | tuple[Any, ...]] = []
        self.current: list[Any] | tuple[Any, ...] = []

    async def stage_campaigns(self, campaigns: list[Any]) -> _Presentation:
        return _Presentation(self, list(campaigns))

    async def replace_campaigns(self, campaigns: list[Any]) -> None:
        presentation = await self.stage_campaigns(campaigns)
        presentation.commit()
        presentation.finalize()


class _NoopTopicLease:
    async def __aenter__(self) -> _NoopTopicLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Websocket:
    async def start(self) -> None:
        return None

    def add_topics(self, _topics: object) -> None:
        return None

    def topic_dispatch_lease(self, _policy: object) -> _NoopTopicLease:
        return _NoopTopicLease()

    def consume_topic_replay_overflow(self) -> bool:
        return False


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
        miner._state_intents = _StateIntentMailbox()
        miner._history_auth_recorded = True
        miner.channels = OrderedDict()
        miner.gui = SimpleNamespace(
            start=lambda: None,
            tray=SimpleNamespace(change_icon=lambda _icon: None),
            status=SimpleNamespace(update=lambda _status: None),
        )
        miner.watch_service = SimpleNamespace(
            start_session=lambda: None,
            handle_idle_state=lambda: False,
            switch_channel=lambda _channels: False,
            restart_watching=lambda: None,
        )
        miner.websocket = _Websocket()
        miner.drop_event_service = SimpleNamespace(
            process_drops=AsyncMock(return_value=None),
            process_notifications=AsyncMock(return_value=None),
        )
        miner.inventory_service = SimpleNamespace(
            start_session=lambda: None,
            sync_state=sync_state,
            update_wanted_games=AsyncMock(return_value=None),
        )
        miner.channel_directory_service = SimpleNamespace(
            start_session=lambda: None,
            cleanup_channels=AsyncMock(return_value=None),
            fetch_channels=AsyncMock(return_value=None),
        )
        miner.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=1))
        return cast(Twitch, miner)

    async def test_state_loop_obeys_inventory_retry_backoff(self) -> None:
        delay_started = asyncio.Event()
        release_delay = asyncio.Event()
        switch_seen = asyncio.Event()
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
        miner.watch_service.switch_channel = (
            lambda _channels: switch_seen.set() or False
        )
        service = cast(Any, InventoryService(miner))
        service.fetch_inventory = fail_inventory
        miner.inventory_service = service
        run_task = asyncio.create_task(miner._run())
        await asyncio.wait_for(delay_started.wait(), timeout=1)
        miner.change_state(State.CHANNEL_SWITCH)
        await asyncio.wait_for(switch_seen.wait(), timeout=1)
        self.assertEqual(attempts, 1)

        release_delay.set()
        await asyncio.wait_for(run_task, timeout=1)
        self.assertEqual(attempts, 2)
        await service.close()

    async def test_state_mailbox_preserves_distinct_intents_in_both_orders(
        self,
    ) -> None:
        for requested in (
            (State.RESTART, State.CHANNEL_SWITCH),
            (State.CHANNEL_SWITCH, State.RESTART),
        ):
            with self.subTest(requested=requested):
                first_started = asyncio.Event()
                release_first = asyncio.Event()

                async def sync_state() -> None:
                    first_started.set()
                    await release_first.wait()

                miner = self._state_loop_miner(sync_state)
                switch_channel = Mock(return_value=False)
                cast(Any, miner.watch_service).switch_channel = switch_channel
                run_task = asyncio.create_task(miner._run())
                await asyncio.wait_for(first_started.wait(), timeout=1)
                for state in requested:
                    miner.change_state(state)
                release_first.set()

                with self.assertRaises(ReloadRequest):
                    await asyncio.wait_for(run_task, timeout=1)
                switch_channel.assert_not_called()
                self.assertIs(miner._state_intents.terminal, State.RESTART)

    async def test_state_mailbox_deduplicates_equivalent_pending_intents(
        self,
    ) -> None:
        mailbox = _StateIntentMailbox()
        mailbox.put(State.CHANNEL_SWITCH)
        mailbox.put(State.CHANNEL_SWITCH)

        self.assertEqual(mailbox.pending, {State.CHANNEL_SWITCH})
        self.assertIs(await mailbox.get(), State.CHANNEL_SWITCH)
        self.assertEqual(mailbox.pending, set())

    async def test_exit_supersedes_restart_and_rejects_later_intents(self) -> None:
        mailbox = _StateIntentMailbox()
        mailbox.put(State.RESTART)
        mailbox.put(State.EXIT)
        mailbox.put(State.CHANNEL_SWITCH)

        self.assertIs(await mailbox.get(), State.EXIT)
        self.assertIs(mailbox.terminal, State.EXIT)
        self.assertNotIn(State.CHANNEL_SWITCH, mailbox.pending)

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

    async def test_inventory_success_prioritizes_mandatory_continuation(self) -> None:
        miner = cast(Any, Twitch.__new__(Twitch))
        miner._state_intents = _StateIntentMailbox()
        miner.inventory = []
        miner._drops = {}
        miner.gui = SimpleNamespace(
            tray=SimpleNamespace(change_icon=lambda _icon: None),
            set_games=lambda _games: None,
        )
        miner.websocket = _Websocket()
        miner.history_event = lambda *_args, **_kwargs: None
        miner.save = lambda: None

        async def fetch_inventory() -> None:
            miner.change_state(State.CHANNEL_SWITCH)
            miner.change_state(State.INVENTORY_FETCH)

        service = cast(Any, InventoryService(miner))
        service.fetch_inventory = fetch_inventory
        stale_retry = asyncio.create_task(asyncio.Event().wait())
        service._retry_task = stale_retry
        miner.inventory_service = service

        await service.sync_state()

        self.assertTrue(stale_retry.done())
        self.assertIsNone(service._retry_task)
        self.assertIs(await miner._state_intents.get(), State.GAMES_UPDATE)
        self.assertIs(await miner._state_intents.get(), State.INVENTORY_FETCH)
        self.assertIs(await miner._state_intents.get(), State.CHANNEL_SWITCH)

    async def test_transient_inventory_failure_preserves_snapshot_and_retries(self) -> None:
        miner = cast(Any, Twitch.__new__(Twitch))
        miner._state_intents = _StateIntentMailbox()
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
        miner.websocket = _Websocket()
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

        self.assertFalse(service.handle_idle_state())
        gui.tray.change_icon.assert_called_with("idle")
        gui.clear_drop.assert_called_once_with()
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
            self.assertEqual(inventory.snapshots, [[], []])
            self.assertEqual(loop_errors, [])
            self.assertEqual(gui.authenticated, [False])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_real_detached_probe_cannot_resurrect_watch_on_shutdown(
        self,
    ) -> None:
        probe_started = asyncio.Event()
        probe_cancelled = asyncio.Event()
        watch_closed = asyncio.Event()

        class Lease:
            async def __aenter__(self) -> Lease:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

        class WebsocketStub:
            def topic_dispatch_lease(self, _policy: object) -> Lease:
                return Lease()

            async def stop(self, *, clear_topics: bool) -> None:
                self.assert_clear_topics(clear_topics)

            @staticmethod
            def assert_clear_topics(clear_topics: bool) -> None:
                if not clear_topics:
                    raise AssertionError("shutdown must clear topics")

        watch_direct = Mock(
            side_effect=AssertionError("probe directly resurrected watch")
        )
        miner = cast(Any, Twitch.__new__(Twitch))
        miner._state_intents = _StateIntentMailbox()
        miner.change_state(State.EXIT)
        rows = SimpleNamespace(display=Mock(), remove=Mock())
        miner.gui = SimpleNamespace(
            channels=rows,
            inv=SimpleNamespace(
                replace_campaigns=AsyncMock(return_value=None),
            ),
            set_games=lambda _games: None,
        )
        miner.channels = OrderedDict()
        miner.channel_directory_service = ChannelDirectoryService(miner)
        miner.channel_event_service = ChannelEventService(miner)
        miner.watch_service = SimpleNamespace(
            should_switch=Mock(return_value=True),
            watch=watch_direct,
        )
        channel = Channel(miner, id=1, login="detached")
        miner.channels[channel.id] = channel

        async def adversarial_probe(_channel: Channel) -> None:
            probe_started.set()
            try:
                await asyncio.Event().wait()
            except (asyncio.CancelledError,):
                probe_cancelled.set()
                miner.channel_event_service.on_channel_update(
                    channel,
                    None,
                    cast(Any, object()),
                )
                raise

        with patch.object(Channel, "_online_delay", adversarial_probe):
            channel.check_online()
        await asyncio.wait_for(probe_started.wait(), timeout=1)

        async def watch_close() -> None:
            self.assertTrue(probe_cancelled.is_set())
            self.assertEqual(miner.channel_directory_service.pending_probe_count, 0)
            watch_closed.set()

        miner.websocket = WebsocketStub()
        miner.inventory_service = SimpleNamespace(close=AsyncMock(return_value=None))
        miner.watch_service.close = watch_close
        miner.transport = SimpleNamespace(close=AsyncMock(return_value=None))
        miner._inventory_generation = 0
        miner._drops = {}
        miner.inventory = []
        miner._campaigns = {}
        miner._auth_state = SimpleNamespace(clear=lambda: None)
        miner.wanted_games = []
        miner.print = Mock()

        with patch("twitch.asyncio.sleep", new=AsyncMock(return_value=None)):
            await miner.shutdown()

        self.assertTrue(watch_closed.is_set())
        self.assertIsNone(channel._pending_stream_up)
        self.assertEqual(miner.channel_directory_service.pending_probe_count, 0)
        watch_direct.assert_not_called()

    async def test_shutdown_pauses_and_stops_producers_before_consumers(self) -> None:
        events: list[str] = []

        class ChannelStub:
            def __init__(self) -> None:
                self._pending_stream_up: asyncio.Task[None] | None = None

            def remove(self) -> None:
                if self._pending_stream_up is not None:
                    raise AssertionError("probe was not awaited before channel removal")
                events.append("channel.remove")

        channel = ChannelStub()

        async def wait_forever() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                channel._pending_stream_up = None

        class WebsocketStub:
            def __init__(self) -> None:
                self.paused = False

            def topic_dispatch_lease(self, _policy: object) -> object:
                owner = self

                class Lease:
                    async def __aenter__(self) -> object:
                        events.append("websocket.pause")
                        owner.paused = True
                        return self

                    async def __aexit__(self, *_args: object) -> None:
                        events.append("websocket.resume")
                        owner.paused = False

                return Lease()

            async def stop(self, *, clear_topics: bool) -> None:
                if not clear_topics:
                    raise AssertionError("shutdown must clear websocket topics")
                if not self.paused:
                    raise AssertionError("shutdown lease was not acquired")
                events.append("websocket.stop")

        async def quiesce_probes(*, restart: bool) -> None:
            self.assertFalse(restart)
            events.append("probes.quiesce")
            task = channel._pending_stream_up
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                channel._pending_stream_up = None

        async def inventory_close() -> None:
            events.append("inventory.close")

        async def watch_close() -> None:
            self.assertIsNone(channel._pending_stream_up)
            events.append("watch.close")

        async def transport_close() -> None:
            events.append("transport.close")

        async def replace_campaigns(_campaigns: object) -> None:
            events.append("inventory.clear")

        miner = cast(Any, Twitch.__new__(Twitch))
        websocket = WebsocketStub()
        channel._pending_stream_up = asyncio.create_task(wait_forever())
        miner._inventory_generation = 1
        miner.websocket = websocket
        miner.inventory_service = SimpleNamespace(close=inventory_close)
        miner.channel_directory_service = SimpleNamespace(
            quiesce_probes=quiesce_probes
        )
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

        self.assertIsNone(channel._pending_stream_up)
        self.assertEqual(
            events,
            [
                "websocket.pause",
                "websocket.stop",
                "probes.quiesce",
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
