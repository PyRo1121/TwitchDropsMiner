from __future__ import annotations

import unittest
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from channel import Channel
from channel_directory_service import ChannelDirectoryService
from constants import State
from exceptions import MinerException
from twitch import Twitch


class ChannelDirectoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_ranking_preserves_game_acl_and_viewer_priority(self) -> None:
        primary_game = object()
        secondary_game = object()
        twitch = cast(
            Any,
            SimpleNamespace(wanted_games=[primary_game, secondary_game]),
        )
        service = ChannelDirectoryService(twitch)
        primary = SimpleNamespace(
            game=primary_game,
            acl_based=False,
            viewers=10,
        )
        primary_acl = SimpleNamespace(
            game=primary_game,
            acl_based=True,
            viewers=1,
        )
        secondary = SimpleNamespace(
            game=secondary_game,
            acl_based=True,
            viewers=100,
        )

        ranked = service.rank_channels(
            cast(Any, [secondary, primary, primary_acl])
        )

        self.assertEqual(ranked, [primary_acl, primary, secondary])

    async def test_cleanup_removes_stale_channels_and_advances_state(self) -> None:
        game = object()
        channel = cast(
            Any,
            SimpleNamespace(
                id=7,
                acl_based=False,
                offline=True,
                game=game,
                remove=Mock(),
            ),
        )
        channels = OrderedDict(((channel.id, channel),))
        states: list[State] = []
        websocket = SimpleNamespace(remove_topics=Mock())
        watch_service = SimpleNamespace(stop_watching_and_wait=AsyncMock())
        twitch = cast(
            Any,
            SimpleNamespace(
                wanted_games=[game],
                gui=SimpleNamespace(
                    status=SimpleNamespace(update=Mock()),
                ),
                websocket=websocket,
                watch_service=watch_service,
                change_state=states.append,
                print=Mock(),
            ),
        )
        service = ChannelDirectoryService(twitch)

        await service.cleanup_channels(channels, full_cleanup=False)

        self.assertEqual(channels, {})
        watch_service.stop_watching_and_wait.assert_awaited_once_with()
        channel.remove.assert_called_once_with()
        websocket.remove_topics.assert_called_once_with(
            [
                "video-playback-by-id.7",
                "broadcast-settings-update.7",
            ]
        )
        self.assertEqual(states, [State.CHANNELS_FETCH])

    async def test_fetch_reinstalls_registry_and_channel_topics(self) -> None:
        game = object()
        channel = Mock()
        channel.id = 7
        channel.game = game
        channel.viewers = 10
        channel.acl_based = False
        channels = cast(
            OrderedDict[int, Channel],
            OrderedDict(((channel.id, channel),)),
        )
        states: list[State] = []
        fast_drop = Mock()
        slow_drop = Mock()
        campaigns = [
            SimpleNamespace(
                game=game,
                allowed_channels=(channel,),
                can_earn_within=Mock(return_value=True),
                can_earn=Mock(return_value=True),
                remaining_minutes=20,
                first_drop=slow_drop,
            ),
            SimpleNamespace(
                game=game,
                allowed_channels=(channel,),
                can_earn_within=Mock(return_value=True),
                can_earn=Mock(return_value=True),
                remaining_minutes=5,
                first_drop=fast_drop,
            ),
        ]
        websocket = SimpleNamespace(
            add_topics=Mock(),
            remove_topics=Mock(),
        )
        watch_service = SimpleNamespace(
            stop_watching_and_wait=AsyncMock(),
            can_watch=Mock(return_value=True),
            primary_channel=SimpleNamespace(
                get_with_default=lambda default: default,
            ),
        )
        twitch = cast(
            Any,
            SimpleNamespace(
                wanted_games=[game],
                inventory=campaigns,
                gui=SimpleNamespace(
                    status=SimpleNamespace(update=Mock()),
                    channels=SimpleNamespace(clear=Mock()),
                ),
                websocket=websocket,
                watch_service=watch_service,
                channel_event_service=SimpleNamespace(
                    process_stream_state=AsyncMock(),
                    process_stream_update=AsyncMock(),
                ),
                change_state=states.append,
            ),
        )
        service = ChannelDirectoryService(twitch)

        await service.fetch_channels(channels)

        self.assertEqual(list(channels), [channel.id])
        watch_service.stop_watching_and_wait.assert_awaited_once_with()
        channel.display.assert_called_once_with(add=True)
        topics = websocket.add_topics.call_args.args[0]
        self.assertEqual(len(topics), 2)
        fast_drop.display.assert_called_once_with(countdown=False, subone=True)
        slow_drop.display.assert_not_called()
        self.assertEqual(states, [State.CHANNEL_SWITCH])

    async def test_directory_root_schema_is_required(self) -> None:
        transport = SimpleNamespace(gql_request=AsyncMock(return_value={"data": {}}))
        twitch = cast(
            Any,
            SimpleNamespace(
                transport=transport,
                wanted_games=[],
            ),
        )
        service = ChannelDirectoryService(twitch)
        game = cast(Any, SimpleNamespace(slug="game", name="Game"))

        with self.assertRaisesRegex(MinerException, "Malformed game directory"):
            await service.get_live_streams(game)

    async def test_directory_isolates_malformed_stream_entries(self) -> None:
        valid_node = {"broadcaster": {"id": "7"}}
        response = {
            "data": {
                "game": {
                    "streams": {
                        "edges": [
                            None,
                            {"node": None},
                            {"node": valid_node},
                        ]
                    }
                }
            }
        }
        transport = SimpleNamespace(gql_request=AsyncMock(return_value=response))
        twitch = cast(
            Any,
            SimpleNamespace(
                transport=transport,
                wanted_games=[],
            ),
        )
        service = ChannelDirectoryService(twitch)
        game = cast(Any, SimpleNamespace(slug="game", name="Game"))
        expected = cast(Channel, object())

        with patch.object(Channel, "from_directory", return_value=expected) as create:
            channels = await service.get_live_streams(game)

        self.assertEqual(channels, [expected])
        create.assert_called_once_with(
            twitch,
            valid_node,
            drops_enabled=True,
        )

    async def test_bulk_lookup_updates_only_matching_online_channels(self) -> None:
        response = [
            {"data": {"user": {"id": "7", "stream": {"id": "stream"}}}},
            {"data": {"user": {"id": "999", "stream": {"id": "other"}}}},
            {"data": {"user": {"id": True, "stream": {}}}},
        ]
        transport = SimpleNamespace(gql_request=AsyncMock(return_value=response))
        twitch = cast(
            Any,
            SimpleNamespace(
                transport=transport,
                settings=SimpleNamespace(available_drops_check=False),
                wanted_games=[],
            ),
        )
        service = ChannelDirectoryService(twitch)
        channel = cast(
            Any,
            SimpleNamespace(
                id=7,
                stream_gql=object(),
                external_update=Mock(),
            ),
        )

        await service.bulk_check_online([channel])

        channel.external_update.assert_called_once_with(
            {"id": "7", "stream": {"id": "stream"}},
            [],
        )

    async def test_directory_limit_is_bounded_before_network_access(self) -> None:
        transport = SimpleNamespace(gql_request=AsyncMock())
        twitch = cast(
            Any,
            SimpleNamespace(
                transport=transport,
                wanted_games=[],
            ),
        )
        service = ChannelDirectoryService(twitch)
        game = cast(Any, SimpleNamespace(slug="game", name="Game"))

        for limit in (True, 0, 101, 1.5, "20"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    await service.get_live_streams(game, limit=cast(Any, limit))

        transport.gql_request.assert_not_awaited()

    def test_coordinator_exposes_only_the_directory_service(self) -> None:
        self.assertFalse(hasattr(Twitch, "get_priority"))
        self.assertFalse(hasattr(Twitch, "_rank_channels"))
        self.assertFalse(hasattr(Twitch, "get_live_streams"))
        self.assertFalse(hasattr(Twitch, "bulk_check_online"))
        self.assertFalse(hasattr(Twitch, "_cleanup_channels_state"))
        self.assertFalse(hasattr(Twitch, "_fetch_channels"))
        self.assertFalse(hasattr(Twitch, "get_active_campaign"))


if __name__ == "__main__":
    unittest.main()
