from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from channel import Channel
from channel_directory_service import ChannelDirectoryService
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


if __name__ == "__main__":
    unittest.main()
