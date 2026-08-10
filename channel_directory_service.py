from __future__ import annotations

import asyncio
import logging
from collections import abc
from typing import TYPE_CHECKING

from channel import Channel
from constants import (
    GQL_BATCH_SIZE,
    GQL_QUERIES,
    MAX_INT,
    GQLOperation,
    JsonType,
    WebsocketTopic,
)
from exceptions import GQLException, MinerException
from utils import cancel_tasks, chunk, extract_available_drops, require_int

if TYPE_CHECKING:
    from game import Game
    from twitch import Twitch


logger = logging.getLogger("TwitchDrops")


class ChannelDirectoryService:
    """Discover, validate, and rank channels without owning watch policy."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch

    def get_priority(self, channel: Channel) -> int:
        """Return a channel's selected-game priority; zero is highest."""
        game = channel.game
        if game is None or game not in self._twitch.wanted_games:
            return MAX_INT
        return self._twitch.wanted_games.index(game)

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        viewers = channel.viewers
        return viewers if viewers is not None else -1

    def rank_channels(self, channels: abc.Iterable[Channel]) -> list[Channel]:
        ordered = sorted(channels, key=self._viewers_key, reverse=True)
        ordered.sort(key=lambda channel: channel.acl_based, reverse=True)
        ordered.sort(key=self.get_priority)
        return ordered

    @staticmethod
    def channel_state_topics(
        channels: abc.Iterable[Channel],
    ) -> list[str]:
        topics: list[str] = []
        for channel in channels:
            topics.append(
                WebsocketTopic.as_str(
                    "Channel",
                    "StreamState",
                    channel.id,
                )
            )
            topics.append(
                WebsocketTopic.as_str(
                    "Channel",
                    "StreamUpdate",
                    channel.id,
                )
            )
        return topics

    async def get_live_streams(
        self,
        game: Game,
        *,
        limit: int = 20,
        drops_enabled: bool = True,
    ) -> list[Channel]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Directory limit must be between 1 and 100")
        filters = ["DROPS_ENABLED"] if drops_enabled else []
        try:
            response = await self._twitch.transport.gql_request(
                GQL_QUERIES["GameDirectory"].with_variables(
                    {
                        "limit": limit,
                        "slug": game.slug,
                        "options": {
                            "includeRestricted": ["SUB_ONLY_LIVE"],
                            "systemFilters": filters,
                        },
                    }
                )
            )
        except GQLException as exc:
            raise MinerException(f"Game: {game.slug}") from exc
        data = response.get("data") if isinstance(response, dict) else None
        game_data = data.get("game") if isinstance(data, dict) else None
        streams = game_data.get("streams") if isinstance(game_data, dict) else None
        edges = streams.get("edges") if isinstance(streams, dict) else None
        if not isinstance(edges, list):
            raise MinerException(f"Malformed game directory: {game.slug}")
        channels: list[Channel] = []
        for edge in edges:
            if not isinstance(edge, dict):
                logger.warning("Ignoring malformed directory stream response")
                continue
            node = edge.get("node")
            if not isinstance(node, dict) or node.get("broadcaster") is None:
                logger.warning("Ignoring malformed directory stream response")
                continue
            try:
                channels.append(
                    Channel.from_directory(
                        self._twitch,
                        node,
                        drops_enabled=drops_enabled,
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed directory stream response")
        return channels

    async def fetch_live_streams_for_games(
        self,
        games: abc.Iterable[Game],
    ) -> set[Channel]:
        channels: set[Channel] = set()
        for game in games:
            try:
                streams = await self.get_live_streams(
                    game,
                    drops_enabled=True,
                )
            except MinerException:
                logger.warning(
                    "Unable to fetch live channels for %s; continuing",
                    game.name,
                )
                continue
            channels.update(streams)
        return channels

    async def bulk_check_online(
        self,
        channels: abc.Iterable[Channel],
    ) -> None:
        """Batch-refresh online state and optional available-Drop metadata."""
        channel_batch = tuple(channels)
        stream_operations = [channel.stream_gql for channel in channel_batch]
        if not stream_operations:
            return
        stream_data = await self._fetch_stream_data(stream_operations)
        available_drops: dict[int, list[JsonType]] = {}
        if self._twitch.settings.available_drops_check:
            online_ids = [
                channel_id
                for channel_id, channel_data in stream_data.items()
                if isinstance(channel_data.get("stream"), dict)
            ]
            available_drops = await self._fetch_available_drops(online_ids)
        for channel in channel_batch:
            channel_data = stream_data.get(channel.id)
            if channel_data is None or not isinstance(
                channel_data.get("stream"),
                dict,
            ):
                continue
            try:
                channel.external_update(
                    channel_data,
                    available_drops.get(channel.id, []),
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Ignoring malformed stream data for channel %s",
                    channel.id,
                )

    async def _fetch_stream_data(
        self,
        operations: list[GQLOperation],
    ) -> dict[int, JsonType]:
        tasks = [
            asyncio.create_task(self._twitch.transport.gql_request(operation_chunk))
            for operation_chunk in chunk(operations, GQL_BATCH_SIZE)
        ]
        stream_data: dict[int, JsonType] = {}
        try:
            for completed in asyncio.as_completed(tasks):
                responses: list[JsonType] = await completed
                for response in responses:
                    try:
                        data = response["data"]["user"]
                        channel_id = (
                            require_int(data["id"], "Invalid channel ID")
                            if data is not None
                            else None
                        )
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Ignoring malformed stream lookup response")
                        continue
                    if isinstance(data, dict) and channel_id is not None:
                        stream_data[channel_id] = data
        finally:
            await cancel_tasks(tasks)
        return stream_data

    async def _fetch_available_drops(
        self,
        channel_ids: abc.Iterable[int],
    ) -> dict[int, list[JsonType]]:
        operations = [
            GQL_QUERIES["AvailableDrops"].with_variables(
                {"channelID": str(channel_id)}
            )
            for channel_id in channel_ids
        ]
        tasks = [
            asyncio.create_task(self._twitch.transport.gql_request(operation_chunk))
            for operation_chunk in chunk(operations, GQL_BATCH_SIZE)
        ]
        available_drops: dict[int, list[JsonType]] = {}
        try:
            for completed in asyncio.as_completed(tasks):
                responses: list[JsonType] = await completed
                for response in responses:
                    try:
                        info = response["data"]["channel"]
                        channel_id = require_int(
                            info["id"],
                            "Invalid channel ID",
                        )
                    except (KeyError, TypeError, ValueError):
                        logger.warning(
                            "Ignoring malformed available-drops response"
                        )
                        continue
                    available_drops[channel_id] = extract_available_drops(response)
        finally:
            await cancel_tasks(tasks)
        return available_drops
