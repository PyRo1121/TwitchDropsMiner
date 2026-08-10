from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from constants import CALL, State
from translate import _
from utils import require_int, task_wrapper

if TYPE_CHECKING:
    from channel import Channel, Stream
    from constants import JsonType
    from twitch import Twitch


logger = logging.getLogger("TwitchDrops")


class ChannelEventService:
    """Validate channel PubSub events and coordinate channel transitions."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch

    @task_wrapper
    async def process_stream_state(
        self,
        channel_id: int,
        message: JsonType,
    ) -> None:
        msg_type = message.get("type")
        if not isinstance(msg_type, str):
            logger.warning("Ignoring a stream state event without a valid type")
            return
        channel = self._twitch.channels.get(channel_id)
        if channel is None:
            logger.error(
                "Stream state change for a non-existing channel: %s",
                channel_id,
            )
            return
        if msg_type == "viewcount":
            if not channel.online:
                channel.check_online()
                return
            try:
                viewers = require_int(
                    message.get("viewers"),
                    "Invalid viewer count",
                )
            except ValueError:
                logger.warning(
                    "Ignoring invalid viewer count for %s",
                    channel.name,
                )
                return
            if viewers < 0:
                logger.warning(
                    "Ignoring invalid viewer count for %s",
                    channel.name,
                )
                return
            channel.viewers = viewers
            channel.display()
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type != "commercial":
            logger.warning("Unknown stream state: %s", msg_type)

    @task_wrapper
    async def process_stream_update(
        self,
        channel_id: int,
        message: JsonType,
    ) -> None:
        if message.get("type") != "broadcast_settings_update":
            logger.warning("Ignoring an unknown broadcast settings event")
            return
        try:
            payload_channel_id = require_int(
                message.get("channel_id"),
                "Invalid broadcast channel ID",
            )
        except ValueError:
            logger.warning("Ignoring a broadcast update without a valid channel ID")
            return
        if payload_channel_id != channel_id:
            logger.warning(
                "Ignoring a broadcast update for mismatched channel %s",
                payload_channel_id,
            )
            return
        channel = self._twitch.channels.get(channel_id)
        if channel is None:
            logger.error(
                "Broadcast settings update for a non-existing channel: %s",
                channel_id,
            )
            return
        old_game = message.get("old_game")
        game = message.get("game")
        if not isinstance(old_game, str) or not isinstance(game, str):
            logger.warning(
                "Ignoring a broadcast update with invalid game metadata"
            )
            return
        game_change = (
            f", game changed: {old_game} -> {game}"
            if old_game != game
            else ""
        )
        logger.log(
            CALL,
            "Channel update from websocket: %s%s",
            channel.name,
            game_change,
        )
        # Tags are omitted, so delay and coalesce a full stream refresh.
        channel.check_online()

    def on_channel_update(
        self,
        channel: Channel,
        stream_before: Stream | None,
        stream_after: Stream | None,
    ) -> None:
        if stream_before is None:
            if stream_after is not None:
                if self._twitch.watch_service.should_switch(channel):
                    self._twitch.print(
                        _("status", "goes_online").format(channel=channel.name)
                    )
                    self._twitch.watch_service.watch(channel)
                else:
                    logger.info("%s goes ONLINE", channel.name)
            else:
                logger.log(CALL, "%s stays OFFLINE", channel.name)
        else:
            is_watching = self._twitch.watch_service.is_watching(channel)
            if is_watching:
                if not self._twitch.watch_service.can_watch(channel):
                    if stream_after is None:
                        self._twitch.print(
                            _("status", "goes_offline").format(
                                channel=channel.name
                            )
                        )
                    else:
                        logger.info(
                            "%s status has been updated, switching... "
                            "(🎁: %s -> %s)",
                            channel.name,
                            self._drops_marker(stream_before),
                            self._drops_marker(stream_after),
                        )
                    self._twitch.change_state(State.CHANNEL_SWITCH)
            elif stream_after is None:
                logger.info("%s goes OFFLINE", channel.name)
            else:
                logger.info(
                    "%s status has been updated (🎁: %s -> %s)",
                    channel.name,
                    self._drops_marker(stream_before),
                    self._drops_marker(stream_after),
                )
                if self._twitch.watch_service.should_switch(channel):
                    self._twitch.watch_service.watch(channel)
        channel.display()

    @staticmethod
    def _drops_marker(stream: Stream | None) -> str:
        return "✔" if stream is not None and stream.drops_enabled else "❌"
