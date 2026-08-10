from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from channel import Channel
from constants import CALL, GQL_QUERIES, State
from exceptions import GQLException, RequestException
from utils import task_wrapper

if TYPE_CHECKING:
    from constants import JsonType
    from inventory import TimedDrop
    from twitch import Twitch


logger = logging.getLogger("TwitchDrops")


class DropEventService:
    """Validate and apply account-scoped Twitch Drop events."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch

    def _inventory_drop_is_current(
        self,
        generation: int,
        drop: TimedDrop,
    ) -> bool:
        return (
            generation == self._twitch._inventory_generation
            and self._twitch._drops.get(drop.id) is drop
        )

    def _assigned_channels(self, drop_id: str) -> list[Channel]:
        return [
            channel
            for channel in self._twitch._watching_channels.values()
            if self._twitch._watch_drop_ids.get(channel.id) == drop_id
        ]

    def _adopt_unassigned_drop(
        self,
        drop_id: str,
        drop: TimedDrop | None,
    ) -> list[Channel]:
        if drop_id in self._twitch._watch_completed_drop_ids:
            logger.log(
                CALL,
                "Ignoring an event for a previously completed drop: %s",
                drop_id,
            )
            return []
        candidates = (
            [
                channel
                for channel in self._twitch._watching_channels.values()
                if drop.can_earn(channel)
            ]
            if drop is not None
            else []
        )
        if drop is None or len(candidates) != 1:
            if self._twitch.watch_service._request_watch_resync(
                f"unassigned-drop:{drop_id}"
            ):
                logger.warning(
                    "Ignoring an event for an unassigned drop: %s",
                    drop_id,
                )
            return []

        channel = candidates[0]
        previous_drop_id = self._twitch._watch_drop_ids.get(channel.id)
        self._twitch._watch_drop_ids[channel.id] = drop_id
        restart_event = self._twitch._watch_restart_events.get(channel.id)
        if restart_event is not None:
            restart_event.set()
        logger.info(
            "Adopted unassigned drop event for %s: %s -> %s",
            channel.name,
            previous_drop_id,
            drop_id,
        )
        return [channel]

    async def _wait_for_next_drop(
        self,
        channel: Channel,
        drop: TimedDrop,
        inventory_generation: int,
    ) -> None:
        # Twitch starts the next Drop after another watch payload, usually
        # within 4-20 seconds. Reconcile each assigned channel independently.
        for _attempt in range(8):
            try:
                context = await self._twitch.transport.gql_request(
                    GQL_QUERIES["CurrentDrop"].with_variables(
                        {"channelID": str(channel.id)}
                    )
                )
                current_data: JsonType | None = (
                    context["data"]["currentUser"]["dropCurrentSession"]
                )
            except (GQLException, RequestException, KeyError, TypeError):
                return
            if not self._inventory_drop_is_current(
                inventory_generation,
                drop,
            ):
                return
            if (
                not isinstance(current_data, dict)
                or current_data.get("dropID") != drop.id
            ):
                return
            await asyncio.sleep(2)

    async def _process_claim(
        self,
        data: JsonType,
        drop: TimedDrop,
        watching_channels: list[Channel],
        inventory_generation: int,
    ) -> None:
        claim_id = data.get("drop_instance_id")
        if not isinstance(claim_id, str) or not claim_id:
            logger.warning("Ignoring a drop claim without a valid instance ID")
            return
        drop.update_claim(claim_id)
        campaign = drop.campaign
        claimed = await drop.claim()
        if not self._inventory_drop_is_current(inventory_generation, drop):
            logger.info("Ignoring a claim result from a replaced inventory")
            return
        if claimed:
            self._twitch.watch_service._mark_watch_completed_drop(drop.id)
        self._twitch.watch_service._display_primary_drop(drop)

        await asyncio.sleep(4)
        if not self._inventory_drop_is_current(inventory_generation, drop):
            return
        await asyncio.gather(
            *(
                self._wait_for_next_drop(
                    channel,
                    drop,
                    inventory_generation,
                )
                for channel in watching_channels
            )
        )
        if not self._inventory_drop_is_current(inventory_generation, drop):
            return
        active_channels = self._twitch._watching_channels.values()
        if claimed and any(
            self._twitch.watch_service.can_watch(channel)
            for channel in active_channels
        ):
            primary = self._twitch.watching_channel.get_with_default(None)
            if primary is not None:
                self._twitch.watch_service.watch(primary, update_status=False)
                self._twitch.watch_service.restart_watching()
                return
        elif not claimed and any(
            campaign.can_earn(channel) for channel in watching_channels
        ):
            self._twitch.watch_service.restart_watching()
            return
        self._twitch.change_state(State.INVENTORY_FETCH)

    def _process_progress(
        self,
        data: JsonType,
        drop: TimedDrop,
        drop_id: str,
    ) -> None:
        current_progress = data.get("current_progress_min")
        required_progress = data.get("required_progress_min")
        if (
            type(current_progress) is not int
            or type(required_progress) is not int
            or current_progress < 0
            or required_progress < 0
            or current_progress > required_progress
        ):
            logger.warning(
                "Ignoring a drop event with invalid progress: %s",
                drop_id,
            )
            return
        logger.log(
            CALL,
            "Drop update from websocket: %s (%s/%s)",
            drop.name,
            current_progress,
            required_progress,
        )
        # PubSub does not include a channel ID; the assigned Drop ID is the
        # authoritative discriminator when two channels are being farmed.
        drop.update_minutes(current_progress, required_progress)
        self._twitch.watch_service._display_primary_drop(drop)

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType) -> None:
        del user_id
        inventory_generation = self._twitch._inventory_generation
        msg_type = message.get("type")
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        data = message.get("data")
        if not isinstance(data, dict):
            logger.warning("Ignoring a drop event without an object data payload")
            return
        drop_id = data.get("drop_id")
        if not isinstance(drop_id, str) or not drop_id:
            logger.warning("Ignoring a drop event without a valid drop ID")
            return
        drop = self._twitch._drops.get(drop_id)
        watching_channels = self._assigned_channels(drop_id)
        if not watching_channels:
            watching_channels = self._adopt_unassigned_drop(drop_id, drop)
            if not watching_channels:
                return
        if drop is None:
            logger.error("Received an event for an unknown drop: %s", drop_id)
            self._twitch.change_state(State.INVENTORY_FETCH)
            return
        if msg_type == "drop-claim":
            await self._process_claim(
                data,
                drop,
                watching_channels,
                inventory_generation,
            )
            return
        self._process_progress(data, drop, drop_id)

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType) -> None:
        del user_id
        if message.get("type") != "create-notification":
            return
        payload = message.get("data")
        if not isinstance(payload, dict):
            logger.warning("Ignoring a notification without an object payload")
            return
        notification = payload.get("notification")
        if not isinstance(notification, dict):
            logger.warning("Ignoring a notification without notification data")
            return
        if notification.get("type") not in (
            "user_drop_reward_reminder_notification",
            "quests_viewer_reward_campaign_earned_emote",
        ):
            return
        self._twitch.change_state(State.INVENTORY_FETCH)
        notification_id = notification.get("id")
        if not isinstance(notification_id, str) or not notification_id:
            logger.warning("Unable to delete a notification without a valid ID")
            return
        try:
            await self._twitch.transport.gql_request(
                GQL_QUERIES["NotificationsDelete"].with_variables(
                    {"input": {"id": notification_id}}
                )
            )
        except (GQLException, RequestException):
            # The inventory refresh remains valid when Twitch has already
            # removed the notification or its delete request fails.
            logger.debug("Unable to delete Twitch notification")
