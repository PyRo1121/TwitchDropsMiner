from __future__ import annotations

import asyncio
import unittest
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from constants import MAX_INT, State
from drop_event_service import DropEventService
from inventory_snapshot import merge_campaign_data
from watch_service import WatchService
from twitch import Twitch
from utils import AwaitableValue


class _Directory:
    def __init__(self, miner: Twitch) -> None:
        self._miner = miner

    def get_priority(self, channel: Any) -> int:
        if channel.game not in self._miner.wanted_games:
            return MAX_INT
        return self._miner.wanted_games.index(channel.game)

    def rank_channels(self, channels: Any) -> list[Any]:
        ordered = sorted(
            channels,
            key=lambda channel: channel.viewers,
            reverse=True,
        )
        ordered.sort(key=lambda channel: channel.acl_based, reverse=True)
        ordered.sort(key=self.get_priority)
        return ordered


def _service(miner: Twitch) -> Any:
    directory_service = getattr(miner, "channel_directory_service", None)
    if directory_service is None:
        cast(Any, miner).channel_directory_service = _Directory(miner)
    service = getattr(miner, "watch_service", None)
    if service is None:
        service = WatchService(miner)
        miner.watch_service = service
    return cast(Any, service)


def _drop_events(miner: Twitch) -> DropEventService:
    service = DropEventService(miner)
    miner.drop_event_service = service
    return service


class _Game:
    def __init__(self, name: str, *, special: bool = False) -> None:
        self.name = name
        self.special = special

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Game) and self.name == other.name

    def __str__(self) -> str:
        return self.name

    def is_special(self) -> bool:
        return self.special


class _Drop:
    def __init__(self, drop_id: str, campaign: _Campaign, remaining: int) -> None:
        self.id = drop_id
        self.campaign = campaign
        self.remaining_minutes = remaining
        self.ends_at = datetime.now(timezone.utc) + timedelta(hours=1)

    def can_earn(self, channel: _Channel | None = None) -> bool:
        return channel is not None and (
            channel.game == self.campaign.game or self.campaign.game.is_special()
        )


class _Campaign:
    def __init__(self, game: _Game, drop_id: str) -> None:
        self.game = game
        self.drops = [_Drop(drop_id, self, 30)]

    def can_earn(self, channel: _Channel | None = None) -> bool:
        return any(drop.can_earn(channel) for drop in self.drops)


class _Channel:
    def __init__(self, channel_id: int, game: _Game, viewers: int) -> None:
        self.id = channel_id
        self.game = game
        self.viewers = viewers
        self.online = True
        self.drops_enabled = True
        self.acl_based = False
        self.name = f"channel-{channel_id}"

    async def send_watch(self) -> bool:
        return True


class DualWatchSelectionTests(unittest.TestCase):
    def test_watch_reset_cancels_cooldown_callbacks(self) -> None:
        async def exercise() -> None:
            miner = cast(Any, Twitch.__new__(Twitch))
            service = _service(miner)
            service._channel_cooldowns = {}
            service._claim_cooldowns = {"drop": 1.0}
            service._resync_cooldowns = {"refresh": 1.0}
            service._completed_drop_ids = {"drop"}

            service._block_watch_channel(1, seconds=60)
            handle = service._cooldown_handles[1]
            service.reset()

            self.assertTrue(handle.cancelled())
            self.assertEqual(service._cooldown_handles, {})
            self.assertEqual(_service(miner)._channel_cooldowns, {})
            self.assertEqual(_service(miner)._claim_cooldowns, {})
            self.assertEqual(_service(miner)._resync_cooldowns, {})
            self.assertEqual(_service(miner)._completed_drop_ids, set())

        asyncio.run(exercise())

    def test_current_drop_reconciliation_adopts_twitch_assignment(self) -> None:
        async def exercise() -> None:
            channel = _Channel(2, _Game("Game B"), 90)
            drop = SimpleNamespace(
                id="drop-b",
                name="Drop B",
                required_minutes=10,
                current_minutes=0,
                is_claimed=False,
                campaign=SimpleNamespace(game=_Game("Game B")),
                can_earn=lambda _channel: True,
                update_minutes=lambda value, required: (
                    setattr(drop, "current_minutes", value),
                    setattr(drop, "required_minutes", required),
                ),
                display=lambda: None,
            )
            miner = cast(Any, Twitch.__new__(Twitch))
            _service(miner)._drop_ids = {2: "drop-a"}
            _service(miner)._watching_channels = OrderedDict(((2, channel),))
            _service(miner)._restart_events = {2: asyncio.Event()}
            _service(miner)._claim_cooldowns = {}
            miner._drops = {"drop-b": drop}
            _service(miner).primary_channel = AwaitableValue()
            _service(miner).primary_channel.set(channel)
            states: list[object] = []
            miner.change_state = states.append

            async def gql_request(_operation: object) -> dict[str, object]:
                return {
                    "data": {
                        "currentUser": {
                            "dropCurrentSession": {
                                "channel": {"id": "2"},
                                "dropID": "drop-b",
                                "currentMinutesWatched": 3,
                                "requiredMinutesWatched": 10,
                            }
                        }
                    }
                }

            miner.transport = SimpleNamespace(gql_request=gql_request)
            await _service(miner)._reconcile_watch_progress(channel)

            self.assertEqual(_service(miner)._drop_ids[2], "drop-b")
            self.assertEqual(drop.current_minutes, 3)
            self.assertTrue(_service(miner)._restart_events[2].is_set())
            self.assertEqual(states, [])

        asyncio.run(exercise())

    def test_completed_current_drop_is_claimed_before_refresh(self) -> None:
        async def exercise() -> None:
            channel = _Channel(2, _Game("Game B"), 90)
            calls: list[str] = []

            async def generate_claim() -> None:
                calls.append("generate")

            async def claim() -> bool:
                calls.append("claim")
                return True

            drop = SimpleNamespace(
                id="drop-b",
                name="Drop B",
                required_minutes=10,
                is_claimed=False,
                campaign=SimpleNamespace(game=_Game("Game B")),
                current_minutes=0,
                can_earn=lambda _channel: True,
                update_minutes=lambda value, required: (
                    setattr(drop, "current_minutes", value),
                    setattr(drop, "required_minutes", required),
                ),
                generate_claim=generate_claim,
                claim=claim,
            )
            miner = cast(Any, Twitch.__new__(Twitch))
            _service(miner)._drop_ids = {2: "drop-a"}
            _service(miner)._watching_channels = OrderedDict(((2, channel),))
            _service(miner)._restart_events = {2: asyncio.Event()}
            _service(miner)._claim_cooldowns = {}
            miner._drops = {"drop-b": drop}
            _service(miner).primary_channel = AwaitableValue()
            _service(miner).primary_channel.set(channel)
            states: list[object] = []
            miner.change_state = states.append

            async def gql_request(_operation: object) -> dict[str, object]:
                return {
                    "data": {
                        "currentUser": {
                            "dropCurrentSession": {
                                "channel": {"id": "2"},
                                "dropID": "drop-b",
                                "currentMinutesWatched": 10,
                                "requiredMinutesWatched": 10,
                            }
                        }
                    }
                }

            miner.transport = SimpleNamespace(gql_request=gql_request)
            await _service(miner)._reconcile_watch_progress(channel)

            self.assertEqual(calls, ["generate", "claim"])
            self.assertIn("drop-b", _service(miner)._completed_drop_ids)
            self.assertEqual(states, [State.INVENTORY_FETCH])
            self.assertGreater(_service(miner)._channel_cooldowns[2], 0)

            await _service(miner)._reconcile_watch_progress(channel)

            self.assertEqual(calls, ["generate", "claim"])
            self.assertEqual(states, [State.INVENTORY_FETCH, State.CHANNEL_SWITCH])

        asyncio.run(exercise())

    def test_authoritative_requirement_prevents_premature_claim(self) -> None:
        async def exercise() -> None:
            channel = _Channel(2, _Game("Game B"), 90)
            claims = 0
            drop = SimpleNamespace(
                id="drop-b",
                name="Drop B",
                required_minutes=5,
                current_minutes=0,
                is_claimed=False,
                campaign=SimpleNamespace(game=_Game("Game B")),
                can_earn=lambda _channel: True,
                display=lambda: None,
            )

            def update_minutes(value: int, required: int) -> None:
                drop.current_minutes = value
                drop.required_minutes = required

            async def generate_claim() -> None:
                nonlocal claims
                claims += 1

            drop.update_minutes = update_minutes
            drop.generate_claim = generate_claim
            drop.claim = generate_claim

            miner = cast(Any, Twitch.__new__(Twitch))
            _service(miner)._drop_ids = {2: "drop-b"}
            _service(miner)._watching_channels = OrderedDict(((2, channel),))
            _service(miner)._restart_events = {2: asyncio.Event()}
            _service(miner)._claim_cooldowns = {}
            _service(miner)._completed_drop_ids = set()
            _service(miner)._channel_cooldowns = {}
            _service(miner)._resync_cooldowns = {}
            miner._drops = {"drop-b": drop}
            _service(miner).primary_channel = AwaitableValue()
            _service(miner).primary_channel.set(channel)

            async def gql_request(_operation: object) -> dict[str, object]:
                return {
                    "data": {
                        "currentUser": {
                            "dropCurrentSession": {
                                "channel": {"id": "2"},
                                "dropID": "drop-b",
                                "currentMinutesWatched": 5,
                                "requiredMinutesWatched": 10,
                            }
                        }
                    }
                }

            miner.transport = SimpleNamespace(gql_request=gql_request)
            await _service(miner)._reconcile_watch_progress(channel)

            self.assertEqual(claims, 0)
            self.assertEqual(drop.current_minutes, 5)
            self.assertEqual(drop.required_minutes, 10)

        asyncio.run(exercise())

    def test_stale_cross_channel_current_drop_response_is_ignored(self) -> None:
        async def exercise() -> None:
            channel = _Channel(2, _Game("Game B"), 90)
            miner = cast(Any, Twitch.__new__(Twitch))
            _service(miner)._drop_ids = {2: "drop-a"}
            _service(miner)._watching_channels = OrderedDict(((2, channel),))
            _service(miner)._restart_events = {}
            _service(miner)._claim_cooldowns = {}
            _service(miner)._completed_drop_ids = set()
            _service(miner)._channel_cooldowns = {}
            _service(miner)._resync_cooldowns = {}
            miner._drops = {}
            _service(miner).primary_channel = AwaitableValue()
            _service(miner).primary_channel.set(channel)
            states: list[object] = []
            miner.change_state = states.append

            async def gql_request(_operation: object) -> dict[str, object]:
                return {
                    "data": {
                        "currentUser": {
                            "dropCurrentSession": {
                                "channel": {"id": "999"},
                                "dropID": "stale-drop",
                                "currentMinutesWatched": 10,
                                "requiredMinutesWatched": 10,
                            }
                        }
                    }
                }

            miner.transport = SimpleNamespace(gql_request=gql_request)
            await _service(miner)._reconcile_watch_progress(channel)

            self.assertEqual(_service(miner)._drop_ids, {2: "drop-a"})
            self.assertEqual(_service(miner)._channel_cooldowns, {})
            self.assertEqual(states, [])

        asyncio.run(exercise())

    def test_cross_channel_current_drop_routes_by_assigned_drop_id(self) -> None:
        async def exercise() -> None:
            game_a = _Game("Game A")
            game_b = _Game("Game B")
            channel_a = _Channel(1, game_a, 100)
            channel_b = _Channel(2, game_b, 90)
            drop_b = SimpleNamespace(
                id="drop-b",
                name="Drop B",
                required_minutes=10,
                current_minutes=0,
                is_claimed=False,
                campaign=SimpleNamespace(game=game_b),
                can_earn=lambda _channel: True,
                update_minutes=lambda value, required: (
                    setattr(drop_b, "current_minutes", value),
                    setattr(drop_b, "required_minutes", required),
                ),
                display=lambda: None,
            )
            miner = cast(Any, Twitch.__new__(Twitch))
            _service(miner)._drop_ids = {1: "drop-a", 2: "drop-b"}
            _service(miner)._watching_channels = OrderedDict(((1, channel_a), (2, channel_b)))
            _service(miner)._restart_events = {}
            _service(miner)._claim_cooldowns = {}
            _service(miner)._completed_drop_ids = set()
            _service(miner)._channel_cooldowns = {}
            _service(miner)._resync_cooldowns = {}
            miner._drops = {"drop-b": drop_b}
            _service(miner).primary_channel = AwaitableValue()
            _service(miner).primary_channel.set(channel_a)
            states: list[object] = []
            miner.change_state = states.append

            async def gql_request(_operation: object) -> dict[str, object]:
                return {
                    "data": {
                        "currentUser": {
                            "dropCurrentSession": {
                                "channel": {"id": "999"},
                                "dropID": "drop-b",
                                "currentMinutesWatched": 4,
                                "requiredMinutesWatched": 10,
                            }
                        }
                    }
                }

            miner.transport = SimpleNamespace(
                gql_request=gql_request,
            )
            await _service(miner)._reconcile_watch_progress(channel_a)

            self.assertEqual(drop_b.current_minutes, 4)
            self.assertEqual(_service(miner)._drop_ids[2], "drop-b")
            self.assertEqual(states, [])

        asyncio.run(exercise())

    def test_single_active_target_adopts_valid_unassigned_drop_event(self) -> None:
        async def exercise() -> None:
            channel = _Channel(1, _Game("Game A"), 100)

            class Drop:
                id = "drop-b"
                name = "Drop B"

                def __init__(self) -> None:
                    self.progress: list[int] = []

                def can_earn(self, candidate: _Channel) -> bool:
                    return candidate is channel

                def update_minutes(self, value: int, required: int) -> None:
                    self.progress.append(value)
                    self.required = required

                def display(self) -> None:
                    return None

            drop = Drop()
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._drops = {drop.id: drop}
            miner._inventory_generation = 1
            _service(miner)._watching_channels = OrderedDict(((1, channel),))
            _service(miner)._drop_ids = {1: "drop-a"}
            _service(miner)._restart_events = {1: asyncio.Event()}
            _service(miner)._completed_drop_ids = set()
            _service(miner)._resync_cooldowns = {}
            _service(miner).primary_channel = AwaitableValue()
            _service(miner).primary_channel.set(channel)
            states: list[object] = []
            miner.change_state = states.append
            _service(miner)

            await _drop_events(miner).process_drops(
                42,
                {
                    "type": "drop-progress",
                    "data": {
                        "drop_id": "drop-b",
                        "current_progress_min": 4,
                        "required_progress_min": 10,
                    },
                },
            )

            self.assertEqual(_service(miner)._drop_ids[1], "drop-b")
            self.assertEqual(drop.progress, [4])
            self.assertTrue(_service(miner)._restart_events[1].is_set())
            self.assertEqual(states, [])

        asyncio.run(exercise())

    def test_progress_events_are_routed_by_assigned_drop_id(self) -> None:
        class Drop:
            id = "drop-a"
            name = "Drop A"

            def __init__(self) -> None:
                self.progress: list[int] = []

            def update_minutes(self, value: int, required: int) -> None:
                self.progress.append(value)
                self.required = required

            def display(self) -> None:
                return None

        async def exercise() -> None:
            channel_a = _Channel(1, _Game("Game A"), 100)
            channel_b = _Channel(2, _Game("Game B"), 90)
            drop = Drop()
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._drops = {drop.id: drop}
            miner._inventory_generation = 1
            _service(miner)._watching_channels = OrderedDict(((1, channel_a), (2, channel_b)))
            _service(miner)._drop_ids = {1: "drop-a", 2: "drop-b"}
            _service(miner).primary_channel = AwaitableValue()
            _service(miner).primary_channel.set(channel_a)
            _service(miner)

            await _drop_events(miner).process_drops(
                42,
                {
                    "type": "drop-progress",
                    "data": {
                        "drop_id": "drop-a",
                        "current_progress_min": 4,
                        "required_progress_min": 10,
                    },
                },
            )
            self.assertEqual(drop.progress, [4])

        asyncio.run(exercise())

    def test_apply_starts_one_watch_task_per_selected_channel(self) -> None:
        async def exercise() -> None:
            game_a = _Game("Game A")
            game_b = _Game("Game B")
            channels = [_Channel(1, game_a, 100), _Channel(2, game_b, 90)]
            campaigns = [_Campaign(game_a, "drop-a"), _Campaign(game_b, "drop-b")]

            class Gui:
                tray = SimpleNamespace(change_icon=lambda _value: None)
                channels = SimpleNamespace(
                    set_watching_channels=lambda _channels: None,
                    set_watching=lambda _channel: None,
                    clear_watching=lambda: None,
                )
                status = SimpleNamespace(update=lambda _value: None)

                def clear_drop(self) -> None:
                    return None

            miner = cast(Any, Twitch.__new__(Twitch))
            miner.channels = OrderedDict((channel.id, channel) for channel in channels)
            miner.inventory = campaigns
            miner.wanted_games = [game_a, game_b]
            miner.gui = Gui()
            _service(miner).primary_channel = AwaitableValue()
            _service(miner)._watching_channels = OrderedDict()
            _service(miner)._drop_ids = {}
            _service(miner)._tasks = {}
            _service(miner)._restart_events = {}
            _service(miner)._generation = 0
            _service(miner)._dual_watch_enabled = True

            assignments = _service(miner)._select_watch_assignments()
            _service(miner)._apply_watch_assignments(assignments, update_status=False)
            await asyncio.sleep(0)
            self.assertEqual(set(_service(miner)._tasks), {1, 2})
            self.assertEqual(list(_service(miner)._watching_channels), [1, 2])
            self.assertEqual(_service(miner)._drop_ids, {1: "drop-a", 2: "drop-b"})
            tasks = tuple(_service(miner)._tasks.values())
            _service(miner).stop_watching()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(exercise())

    def test_apply_hard_clamps_assignments_after_dual_watch_is_disabled(self) -> None:
        async def exercise() -> None:
            game_a = _Game("Game A")
            game_b = _Game("Game B")
            channels = [_Channel(1, game_a, 100), _Channel(2, game_b, 90)]
            campaigns = [_Campaign(game_a, "drop-a"), _Campaign(game_b, "drop-b")]

            class Gui:
                tray = SimpleNamespace(change_icon=lambda _value: None)
                channels = SimpleNamespace(
                    set_watching_channels=lambda _channels: None,
                    set_watching=lambda _channel: None,
                    clear_watching=lambda: None,
                )
                status = SimpleNamespace(update=lambda _value: None)

                def clear_drop(self) -> None:
                    return None

            miner = cast(Any, Twitch.__new__(Twitch))
            miner.channels = OrderedDict((channel.id, channel) for channel in channels)
            miner.inventory = campaigns
            miner.wanted_games = [game_a, game_b]
            miner.gui = Gui()
            _service(miner).primary_channel = AwaitableValue()
            _service(miner)._watching_channels = OrderedDict()
            _service(miner)._drop_ids = {}
            _service(miner)._tasks = {}
            _service(miner)._restart_events = {}
            _service(miner)._generation = 0
            _service(miner)._dual_watch_enabled = True
            assignments = _service(miner)._select_watch_assignments()
            self.assertEqual(len(assignments), 2)

            _service(miner)._dual_watch_enabled = False
            _service(miner)._apply_watch_assignments(assignments, update_status=False)
            await asyncio.sleep(0)

            self.assertEqual(set(_service(miner)._tasks), {1})
            self.assertEqual(list(_service(miner)._watching_channels), [1])
            self.assertEqual(_service(miner)._drop_ids, {1: "drop-a"})
            tasks = tuple(_service(miner)._tasks.values())
            _service(miner).stop_watching()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(exercise())

    def test_inventory_detail_merge_preserves_progress_and_adds_rewards(self) -> None:
        miner = cast(Any, Twitch.__new__(Twitch))
        merged = merge_campaign_data(
            {
                "id": "campaign",
                "timeBasedDrops": [
                    {"id": "drop", "self": {"currentMinutesWatched": 4}}
                ],
            },
            {
                "id": "campaign",
                "timeBasedDrops": [
                    {
                        "id": "drop",
                        "requiredMinutesWatched": 10,
                        "benefitEdges": [{"id": "benefit"}],
                    },
                    {"id": "second-drop"},
                ],
            },
        )

        self.assertEqual(merged["timeBasedDrops"][0]["self"]["currentMinutesWatched"], 4)
        self.assertEqual(merged["timeBasedDrops"][0]["requiredMinutesWatched"], 10)
        self.assertEqual(merged["timeBasedDrops"][1]["id"], "second-drop")

    def test_selection_can_avoid_a_shared_special_drop(self) -> None:
        game_a = _Game("Game A")
        game_b = _Game("Game B")
        shared_campaign = _Campaign(_Game("Global Drops", special=True), "shared-drop")
        campaign_a = _Campaign(game_a, "drop-a")
        campaign_b = _Campaign(game_b, "drop-b")
        first = _Channel(1, game_a, 100)
        second = _Channel(2, game_b, 90)

        miner = cast(Any, Twitch.__new__(Twitch))
        miner.channels = OrderedDict((channel.id, channel) for channel in (first, second))
        miner.inventory = [shared_campaign, campaign_a, campaign_b]
        miner.wanted_games = [game_a, game_b]
        _service(miner)._dual_watch_enabled = True

        assignments = _service(miner)._select_watch_assignments(preferred=first)

        self.assertEqual([channel.id for channel, _drop in assignments], [1, 2])
        self.assertEqual([drop.id for _channel, drop in assignments], ["shared-drop", "drop-b"])

    def test_reconciliation_failure_can_disable_the_second_slot(self) -> None:
        game_a = _Game("Game A")
        game_b = _Game("Game B")
        channels = (_Channel(1, game_a, 100), _Channel(2, game_b, 90))
        miner = cast(Any, Twitch.__new__(Twitch))
        _service(miner).primary_channel = AwaitableValue()
        _service(miner).primary_channel.set(channels[0])
        _service(miner)._watching_channels = OrderedDict(
            (channel.id, channel) for channel in channels
        )
        _service(miner)._dual_watch_enabled = True

        _service(miner)._disable_dual_watch_if_secondary(channels[1])

        self.assertFalse(_service(miner)._dual_watch_enabled)

    def test_selection_uses_two_distinct_games_and_drops(self) -> None:
        game_a = _Game("Game A")
        game_b = _Game("Game B")
        campaign_a = _Campaign(game_a, "drop-a")
        campaign_b = _Campaign(game_b, "drop-b")
        campaign_a_same_game = _Campaign(game_a, "drop-c")
        first = _Channel(1, game_a, 100)
        second = _Channel(2, game_b, 90)
        same_game = _Channel(3, game_a, 200)

        miner = cast(Any, Twitch.__new__(Twitch))
        miner.channels = OrderedDict(
            (channel.id, channel) for channel in (first, second, same_game)
        )
        miner.inventory = [campaign_a, campaign_b, campaign_a_same_game]
        miner.wanted_games = [game_a, game_b]
        _service(miner)._dual_watch_enabled = True

        assignments = _service(miner)._select_watch_assignments(preferred=first)
        selected = _service(miner)._select_watch_channels(preferred=first)

        self.assertEqual([channel.id for channel in selected], [1, 2])
        self.assertEqual({channel.game for channel in selected}, {game_a, game_b})
        self.assertEqual(
            {drop.id for _channel, drop in assignments}, {"drop-a", "drop-b"}
        )


if __name__ == "__main__":
    unittest.main()
