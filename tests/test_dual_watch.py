from __future__ import annotations

import asyncio
import unittest
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from constants import State
from twitch import Twitch
from utils import AwaitableValue


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


class DualWatchSelectionTests(unittest.TestCase):
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
                update_minutes=lambda value: setattr(drop, "current_minutes", value),
                display=lambda: None,
            )
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._watch_drop_ids = {2: "drop-a"}
            miner._watching_channels = OrderedDict(((2, channel),))
            miner._watch_restart_events = {2: asyncio.Event()}
            miner._watch_claim_cooldowns = {}
            miner._drops = {"drop-b": drop}
            miner.watching_channel = AwaitableValue()
            miner.watching_channel.set(channel)
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
                            }
                        }
                    }
                }

            miner.gql_request = gql_request
            await miner._reconcile_watch_progress(channel)

            self.assertEqual(miner._watch_drop_ids[2], "drop-b")
            self.assertEqual(drop.current_minutes, 3)
            self.assertTrue(miner._watch_restart_events[2].is_set())
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
                can_earn=lambda _channel: True,
                generate_claim=generate_claim,
                claim=claim,
            )
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._watch_drop_ids = {2: "drop-a"}
            miner._watching_channels = OrderedDict(((2, channel),))
            miner._watch_restart_events = {2: asyncio.Event()}
            miner._watch_claim_cooldowns = {}
            miner._drops = {"drop-b": drop}
            miner.watching_channel = AwaitableValue()
            miner.watching_channel.set(channel)
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

            miner.gql_request = gql_request
            await miner._reconcile_watch_progress(channel)

            self.assertEqual(calls, ["generate", "claim"])
            self.assertIn("drop-b", miner._watch_completed_drop_ids)
            self.assertEqual(states, [State.INVENTORY_FETCH])
            self.assertGreater(miner._watch_channel_cooldowns[2], 0)

            await miner._reconcile_watch_progress(channel)

            self.assertEqual(calls, ["generate", "claim"])
            self.assertEqual(states, [State.INVENTORY_FETCH, State.CHANNEL_SWITCH])

        asyncio.run(exercise())

    def test_stale_cross_channel_current_drop_response_is_ignored(self) -> None:
        async def exercise() -> None:
            channel = _Channel(2, _Game("Game B"), 90)
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._watch_drop_ids = {2: "drop-a"}
            miner._watching_channels = OrderedDict(((2, channel),))
            miner._watch_restart_events = {}
            miner._watch_claim_cooldowns = {}
            miner._watch_completed_drop_ids = set()
            miner._watch_channel_cooldowns = {}
            miner._watch_resync_cooldowns = {}
            miner._drops = {}
            miner.watching_channel = AwaitableValue()
            miner.watching_channel.set(channel)
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
                            }
                        }
                    }
                }

            miner.gql_request = gql_request
            await miner._reconcile_watch_progress(channel)

            self.assertEqual(miner._watch_drop_ids, {2: "drop-a"})
            self.assertEqual(miner._watch_channel_cooldowns, {})
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
                update_minutes=lambda value: setattr(drop_b, "current_minutes", value),
                display=lambda: None,
            )
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._watch_drop_ids = {1: "drop-a", 2: "drop-b"}
            miner._watching_channels = OrderedDict(((1, channel_a), (2, channel_b)))
            miner._watch_restart_events = {}
            miner._watch_claim_cooldowns = {}
            miner._watch_completed_drop_ids = set()
            miner._watch_channel_cooldowns = {}
            miner._watch_resync_cooldowns = {}
            miner._drops = {"drop-b": drop_b}
            miner.watching_channel = AwaitableValue()
            miner.watching_channel.set(channel_a)
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
                            }
                        }
                    }
                }

            miner.gql_request = gql_request
            await miner._reconcile_watch_progress(channel_a)

            self.assertEqual(drop_b.current_minutes, 4)
            self.assertEqual(miner._watch_drop_ids[2], "drop-b")
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

                def update_minutes(self, value: int) -> None:
                    self.progress.append(value)

                def display(self) -> None:
                    return None

            drop = Drop()
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._drops = {drop.id: drop}
            miner._watching_channels = OrderedDict(((1, channel),))
            miner._watch_drop_ids = {1: "drop-a"}
            miner._watch_restart_events = {1: asyncio.Event()}
            miner._watch_completed_drop_ids = set()
            miner._watch_resync_cooldowns = {}
            miner.watching_channel = AwaitableValue()
            miner.watching_channel.set(channel)
            states: list[object] = []
            miner.change_state = states.append

            await miner.process_drops(
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

            self.assertEqual(miner._watch_drop_ids[1], "drop-b")
            self.assertEqual(drop.progress, [4])
            self.assertTrue(miner._watch_restart_events[1].is_set())
            self.assertEqual(states, [])

        asyncio.run(exercise())

    def test_progress_events_are_routed_by_assigned_drop_id(self) -> None:
        class Drop:
            id = "drop-a"
            name = "Drop A"

            def __init__(self) -> None:
                self.progress: list[int] = []

            def update_minutes(self, value: int) -> None:
                self.progress.append(value)

            def display(self) -> None:
                return None

        async def exercise() -> None:
            channel_a = _Channel(1, _Game("Game A"), 100)
            channel_b = _Channel(2, _Game("Game B"), 90)
            drop = Drop()
            miner = cast(Any, Twitch.__new__(Twitch))
            miner._drops = {drop.id: drop}
            miner._watching_channels = OrderedDict(((1, channel_a), (2, channel_b)))
            miner._watch_drop_ids = {1: "drop-a", 2: "drop-b"}
            miner.watching_channel = AwaitableValue()
            miner.watching_channel.set(channel_a)

            await miner.process_drops(
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
            miner.watching_channel = AwaitableValue()
            miner._watching_channels = OrderedDict()
            miner._watch_drop_ids = {}
            miner._watch_tasks = {}
            miner._watch_restart_events = {}
            miner._watch_generation = 0

            assignments = miner._select_watch_assignments()
            miner._apply_watch_assignments(assignments, update_status=False)
            await asyncio.sleep(0)
            self.assertEqual(set(miner._watch_tasks), {1, 2})
            self.assertEqual(list(miner._watching_channels), [1, 2])
            self.assertEqual(miner._watch_drop_ids, {1: "drop-a", 2: "drop-b"})
            tasks = tuple(miner._watch_tasks.values())
            miner.stop_watching()
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
            miner.watching_channel = AwaitableValue()
            miner._watching_channels = OrderedDict()
            miner._watch_drop_ids = {}
            miner._watch_tasks = {}
            miner._watch_restart_events = {}
            miner._watch_generation = 0
            miner._dual_watch_enabled = True
            assignments = miner._select_watch_assignments()
            self.assertEqual(len(assignments), 2)

            miner._dual_watch_enabled = False
            miner._apply_watch_assignments(assignments, update_status=False)
            await asyncio.sleep(0)

            self.assertEqual(set(miner._watch_tasks), {1})
            self.assertEqual(list(miner._watching_channels), [1])
            self.assertEqual(miner._watch_drop_ids, {1: "drop-a"})
            tasks = tuple(miner._watch_tasks.values())
            miner.stop_watching()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(exercise())

    def test_inventory_detail_merge_preserves_progress_and_adds_rewards(self) -> None:
        miner = cast(Any, Twitch.__new__(Twitch))
        merged = miner._merge_data(
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

        assignments = miner._select_watch_assignments(preferred=first)

        self.assertEqual([channel.id for channel, _drop in assignments], [1, 2])
        self.assertEqual([drop.id for _channel, drop in assignments], ["shared-drop", "drop-b"])

    def test_reconciliation_failure_can_disable_the_second_slot(self) -> None:
        game_a = _Game("Game A")
        game_b = _Game("Game B")
        channels = (_Channel(1, game_a, 100), _Channel(2, game_b, 90))
        miner = cast(Any, Twitch.__new__(Twitch))
        miner.watching_channel = AwaitableValue()
        miner.watching_channel.set(channels[0])
        miner._watching_channels = OrderedDict((channel.id, channel) for channel in channels)
        miner._dual_watch_enabled = True

        miner._disable_dual_watch_if_secondary(channels[1])

        self.assertFalse(miner._dual_watch_enabled)

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
        miner.channels = OrderedDict((channel.id, channel) for channel in (first, second, same_game))
        miner.inventory = [campaign_a, campaign_b, campaign_a_same_game]
        miner.wanted_games = [game_a, game_b]

        assignments = miner._select_watch_assignments(preferred=first)
        selected = miner._select_watch_channels(preferred=first)

        self.assertEqual([channel.id for channel in selected], [1, 2])
        self.assertEqual({channel.game for channel in selected}, {game_a, game_b})
        self.assertEqual(
            {drop.id for _channel, drop in assignments}, {"drop-a", "drop-b"}
        )


if __name__ == "__main__":
    unittest.main()
