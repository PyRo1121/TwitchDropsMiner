from __future__ import annotations

import asyncio
import logging
from collections import abc, OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from channel import Channel
from constants import (
    GQL_BATCH_SIZE,
    GQL_QUERIES,
    MAX_CHANNELS,
    MAX_INT,
    GQLOperation,
    JsonType,
    State,
    WebsocketTopic,
)
from exceptions import GQLException, MinerException
from translate import _
from utils import cancel_tasks, chunk, extract_available_drops, require_int

if TYPE_CHECKING:
    from game import Game
    from inventory import DropsCampaign
    from twitch import Twitch


logger = logging.getLogger("TwitchDrops")


class ChannelDirectoryService:
    """Own channel discovery and registry reconciliation, not watch loops."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._probe_tasks: dict[
            int, tuple[Channel, asyncio.Task[Any]]
        ] = {}
        self._probe_restart: dict[int, Channel] = {}
        self._probes_quiesced = False

    @property
    def probes_quiesced(self) -> bool:
        return self._probes_quiesced

    @property
    def pending_probe_count(self) -> int:
        return len(self._probe_tasks)

    def start_session(self) -> None:
        self._probes_quiesced = False
        self._probe_restart.clear()

    def start_online_probe(self, channel: Channel) -> None:
        if self._probes_quiesced or channel.id in self._probe_tasks:
            return
        task = asyncio.create_task(channel._online_delay())
        self._probe_tasks[channel.id] = (channel, task)
        channel._pending_stream_up = task
        task.add_done_callback(
            lambda completed, owner=channel: self._probe_done(owner, completed)
        )
        channel.display()

    def _probe_done(
        self,
        channel: Channel,
        task: asyncio.Task[Any],
    ) -> None:
        if self._probe_tasks.get(channel.id) == (channel, task):
            del self._probe_tasks[channel.id]
        if channel._pending_stream_up is task:
            channel._pending_stream_up = None
            channel.display()
        if not task.cancelled():
            task.exception()

    @staticmethod
    async def _cancel_probe_tasks(
        tasks: abc.Iterable[asyncio.Task[Any]],
    ) -> None:
        owned = tuple(tasks)
        for task in owned:
            if not task.done():
                task.cancel()
        if not owned:
            return
        barrier = asyncio.gather(*owned, return_exceptions=True)
        cancelled = False
        while not barrier.done():
            try:
                await asyncio.shield(barrier)
            except (asyncio.CancelledError,):
                cancelled = True
        barrier.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def cancel_probes(
        self,
        channels: abc.Iterable[Channel],
    ) -> None:
        selected = tuple(channels)
        tasks: list[asyncio.Task[Any]] = []
        for channel in selected:
            owned = self._probe_tasks.get(channel.id)
            if owned is not None and owned[0] is channel:
                tasks.append(owned[1])
            self._probe_restart.pop(channel.id, None)
        await self._cancel_probe_tasks(tasks)

    async def quiesce_probes(self, *, restart: bool) -> None:
        """Block new probes, then cancel and await every detached probe."""
        self._probes_quiesced = True
        active = tuple(self._probe_tasks.values())
        if restart:
            self._probe_restart.update(
                (channel.id, channel) for channel, _task in active
            )
        else:
            self._probe_restart.clear()
        await self._cancel_probe_tasks(
            task for _channel, task in active
        )

    def resume_probes(self, *, restart: bool) -> None:
        candidates = tuple(self._probe_restart.values()) if restart else ()
        self._probe_restart.clear()
        self._probes_quiesced = False
        for channel in candidates:
            if self._twitch.channels.get(channel.id) is channel:
                self.start_online_probe(channel)

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

    async def cleanup_channels(
        self,
        channels: OrderedDict[int, Channel],
        *,
        full_cleanup: bool,
    ) -> None:
        self._twitch.gui.status.update(_("gui", "status", "cleanup"))
        if not self._twitch.wanted_games or full_cleanup:
            to_remove = list(channels.values())
        else:
            to_remove = [
                channel
                for channel in channels.values()
                if not channel.acl_based
                and (
                    channel.offline
                    or channel.game is None
                    or channel.game not in self._twitch.wanted_games
                )
            ]
        if to_remove:
            await self.cancel_probes(to_remove)
            await self._twitch.watch_service.stop_watching_and_wait()
            self._twitch.websocket.remove_topics(
                self.channel_state_topics(to_remove)
            )
            for channel in to_remove:
                del channels[channel.id]
                channel.remove()
        if self._twitch.wanted_games:
            self._twitch.change_state(State.CHANNELS_FETCH)
        else:
            self._twitch.print(_("status", "no_campaign"))
            self._twitch.change_state(State.IDLE)

    async def fetch_channels(
        self,
        channels: OrderedDict[int, Channel],
    ) -> None:
        # Quiesce detached probes before watch tasks and Channel replacement.
        await self.cancel_probes(tuple(channels.values()))
        await self._twitch.watch_service.stop_watching_and_wait()
        self._twitch.gui.status.update(_("gui", "status", "gathering"))
        new_channels = set(channels.values())
        channels.clear()
        self._twitch.gui.channels.clear()

        no_acl: set[Game] = set()
        acl_channels: set[Channel] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        for campaign in self._twitch.inventory:
            if (
                campaign.game in self._twitch.wanted_games
                and campaign.can_earn_within(next_hour)
            ):
                if campaign.allowed_channels:
                    acl_channels.update(campaign.allowed_channels)
                else:
                    no_acl.add(campaign.game)

        acl_channels.difference_update(new_channels)
        await self.bulk_check_online(acl_channels)
        new_channels.update(acl_channels)
        new_channels.update(await self.fetch_live_streams_for_games(no_acl))
        ordered = self.rank_channels(new_channels)
        removed = ordered[MAX_CHANNELS:]
        ordered = ordered[:MAX_CHANNELS]
        if removed:
            self._twitch.websocket.remove_topics(
                self.channel_state_topics(removed)
            )
        for channel in ordered:
            channels[channel.id] = channel
            channel.display(add=True)

        topics: list[WebsocketTopic] = []
        for channel_id in channels:
            topics.extend(
                (
                    WebsocketTopic(
                        "Channel",
                        "StreamState",
                        channel_id,
                        self._twitch.channel_event_service.process_stream_state,
                    ),
                    WebsocketTopic(
                        "Channel",
                        "StreamUpdate",
                        channel_id,
                        self._twitch.channel_event_service.process_stream_update,
                    ),
                )
            )
        self._twitch.websocket.add_topics(topics)

        for channel in channels.values():
            if not self._twitch.watch_service.can_watch(channel):
                continue
            active_campaign = self._get_active_campaign(channel)
            if active_campaign is not None and active_campaign.first_drop is not None:
                active_campaign.first_drop.display(countdown=False, subone=True)
            break
        self._twitch.change_state(State.CHANNEL_SWITCH)

    def _get_active_campaign(
        self,
        channel: Channel | None = None,
    ) -> DropsCampaign | None:
        if not self._twitch.wanted_games:
            return None
        watching_channel = self._twitch.watch_service.primary_channel.get_with_default(
            channel
        )
        if watching_channel is None:
            return None
        campaigns = [
            campaign
            for campaign in self._twitch.inventory
            if campaign.can_earn(watching_channel)
        ]
        if not campaigns:
            return None
        return min(campaigns, key=lambda campaign: campaign.remaining_minutes)

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
