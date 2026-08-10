from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import TYPE_CHECKING

from constants import CALL, GQL_QUERIES, WATCH_INTERVAL, State
from exceptions import GQLException, RequestException
from utils import cancel_tasks, require_int

if TYPE_CHECKING:
    from channel import Channel
    from constants import JsonType
    from inventory import DropsCampaign, TimedDrop
    from twitch import Twitch
    from watch_service import WatchService

logger = logging.getLogger("TwitchDrops")


class WatchProgressService:
    """Reconcile authoritative Drop progress for owned watch assignments."""

    def __init__(self, twitch: Twitch, watch_service: WatchService) -> None:
        self._twitch = twitch
        self._watch = watch_service
        self._claim_cooldowns: dict[str, float] = {}
        self._completed_drop_ids: set[str] = set()
        self._resync_cooldowns: dict[str, float] = {}

    def reset(self) -> None:
        self._claim_cooldowns.clear()
        self._completed_drop_ids.clear()
        self._resync_cooldowns.clear()

    def retain_claim_cooldowns(self, drops: dict[str, TimedDrop]) -> None:
        now = monotonic()
        self._claim_cooldowns = {
            drop_id: blocked_until
            for drop_id, blocked_until in self._claim_cooldowns.items()
            if blocked_until > now
            and drop_id in drops
            and not drops[drop_id].is_claimed
        }

    def display_primary_drop(self, drop: TimedDrop) -> None:
        primary = self._watch.primary_channel.get_with_default(None)
        if (
            primary is not None
            and self._watch.assigned_drop_id(primary.id) == drop.id
        ):
            drop.display()

    def mark_completed_drop(self, drop_id: str) -> None:
        self._completed_drop_ids.add(drop_id)

    def _request_resync(self, key: str, seconds: float = 300) -> bool:
        now = monotonic()
        if self._resync_cooldowns.get(key, 0) > now:
            return False
        self._resync_cooldowns[key] = now + seconds
        self._twitch.change_state(State.INVENTORY_FETCH)
        return True

    async def _watch_sleep(self, event: asyncio.Event, delay: float) -> bool:
        wait_task = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((wait_task,), timeout=max(delay, 0))
            return bool(done)
        finally:
            await cancel_tasks((wait_task,))
            event.clear()

    async def run_channel(
        self,
        channel: Channel,
        restart_event: asyncio.Event,
        generation: int,
    ) -> None:
        interval = WATCH_INTERVAL.total_seconds()
        try:
            while self._watch.assignment_is_current(channel, generation):
                if not channel.online or not self._watch.can_watch(channel):
                    self._twitch.change_state(State.CHANNEL_SWITCH)
                    return
                succeeded = await channel.send_watch()
                last_sent = monotonic()
                if not succeeded:
                    logger.log(CALL, "Watch request failed for channel: %s", channel.name)
                if await self._watch_sleep(restart_event, 20):
                    continue
                primary = self._watch.primary_channel.get_with_default(None)
                if channel is not primary or self._twitch.gui.progress.minute_almost_done():
                    await self.reconcile(channel)
                await self._watch_sleep(
                    restart_event,
                    interval - min(monotonic() - last_sent, interval),
                )
        except Exception as exc:
            logger.exception("Watch loop failed for channel %s", channel.name)
            self._twitch.history_event(
                "watch.failed",
                severity="warning",
                data={
                    "channel_id": channel.id,
                    "error_type": type(exc).__name__,
                },
            )
            self._watch.block_channel(channel.id, seconds=60)
            self._twitch.change_state(State.CHANNEL_SWITCH)

    def assigned_channels(self, drop_id: str) -> list[Channel]:
        return [
            channel
            for channel in self._watch.active_channels()
            if self._watch.assigned_drop_id(channel.id) == drop_id
        ]

    def adopt_unassigned_drop(
        self,
        drop_id: str,
        drop: TimedDrop | None,
    ) -> list[Channel]:
        if drop_id in self._completed_drop_ids:
            logger.log(
                CALL,
                "Ignoring an event for a previously completed drop: %s",
                drop_id,
            )
            return []
        candidates = (
            [
                channel
                for channel in self._watch.active_channels()
                if drop.can_earn(channel)
            ]
            if drop is not None
            else []
        )
        if drop is None or len(candidates) != 1:
            if self._request_resync(f"unassigned-drop:{drop_id}"):
                logger.warning(
                    "Ignoring an event for an unassigned drop: %s",
                    drop_id,
                )
            return []

        channel = candidates[0]
        previous_drop_id = self._watch.assign_drop(channel, drop_id)
        logger.info(
            "Adopted unassigned drop event for %s: %s -> %s",
            channel.name,
            previous_drop_id,
            drop_id,
        )
        return [channel]

    def continue_after_claim(
        self,
        claimed: bool,
        campaign: DropsCampaign,
        watching_channels: list[Channel],
    ) -> bool:
        if claimed and any(
            self._watch.can_watch(channel)
            for channel in self._watch.active_channels()
        ):
            primary = self._watch.primary_channel.get_with_default(None)
            if primary is not None:
                self._watch.watch(primary, update_status=False)
                self._watch.restart_watching()
                return True
        elif not claimed and any(
            campaign.can_earn(channel) for channel in watching_channels
        ):
            self._watch.restart_watching()
            return True
        return False

    def eligible_drops_for_channel(self, channel: Channel) -> list[TimedDrop]:
        candidates: list[TimedDrop] = []
        seen: set[str] = set()
        now = monotonic()
        for campaign in self._twitch.inventory:
            if not campaign.can_earn(channel):
                continue
            for drop in campaign.drops:
                if drop.id in self._completed_drop_ids:
                    continue
                blocked_until = self._claim_cooldowns.get(drop.id)
                if blocked_until is not None:
                    if blocked_until > now:
                        continue
                    self._claim_cooldowns.pop(drop.id, None)
                if drop.id not in seen and drop.can_earn(channel):
                    candidates.append(drop)
                    seen.add(drop.id)
        candidates.sort(key=lambda drop: (drop.remaining_minutes, drop.ends_at))
        return candidates

    async def reconcile(self, channel: Channel) -> None:
        """Refresh assigned progress from Twitch's authoritative viewer session."""
        if self._watch.assigned_drop_id(channel.id) is None:
            return
        try:
            context = await self._twitch.transport.gql_request(
                GQL_QUERIES["CurrentDrop"].with_variables(
                    {"channelID": str(channel.id)}
                )
            )
            drop_data: JsonType | None = (
                context["data"]["currentUser"]["dropCurrentSession"]
            )
        except (GQLException, RequestException, KeyError, TypeError):
            logger.warning("Unable to reconcile drop progress for %s", channel.name)
            return
        if not isinstance(drop_data, dict):
            logger.log(CALL, "Twitch reported no current drop for %s", channel.name)
            return
        drop_id = drop_data.get("dropID")
        if not isinstance(drop_id, str):
            logger.warning("Twitch returned an invalid current drop for %s", channel.name)
            return

        reported_channel_id: int | None = None
        reported_channel = drop_data.get("channel")
        if isinstance(reported_channel, dict):
            try:
                raw_channel_id = reported_channel.get("id")
                reported_channel_id = (
                    require_int(raw_channel_id, "Invalid reported channel ID")
                    if raw_channel_id is not None
                    else None
                )
            except (TypeError, ValueError):
                logger.warning("Twitch returned an invalid channel for %s", channel.name)
                return

        # CurrentDrop is account-scoped in practice and can report a stale session
        # for another channel. Prefer the assigned Drop owner, then the reported
        # channel, and never restart a productive target for an unknown mismatch.
        if drop_id in self._completed_drop_ids:
            stale_channel: Channel | None = channel
            if reported_channel_id is not None:
                stale_channel = self._watch.active_channel(reported_channel_id)
                if stale_channel is None:
                    logger.log(
                        CALL,
                        "Ignoring stale completed drop %s from channel %s",
                        drop_id,
                        reported_channel_id,
                    )
                    return
            logger.info(
                "Twitch still reports previously completed drop %s for %s; skipping claim",
                drop_id,
                stale_channel.name,
            )
            self._watch.disable_secondary(stale_channel)
            self._watch.block_channel(stale_channel.id)
            self._twitch.change_state(State.CHANNEL_SWITCH)
            return

        target = channel
        if reported_channel_id is not None and reported_channel_id != channel.id:
            assigned_owner = self._watch.assigned_channel_for_drop(drop_id)
            if assigned_owner is not None:
                target = assigned_owner
                logger.log(
                    CALL,
                    "Routing current drop %s from %s to assigned channel %s",
                    drop_id,
                    channel.name,
                    target.name,
                )
            else:
                reported_target = self._watch.active_channel(reported_channel_id)
                if reported_target is None:
                    logger.warning(
                        "Ignoring stale current-drop session for %s: %s",
                        channel.name,
                        drop_id,
                    )
                    return
                target = reported_target
                logger.log(
                    CALL,
                    "Routing current drop response from %s to reported channel %s",
                    channel.name,
                    target.name,
                )

        assigned_drop_id = self._watch.assigned_drop_id(target.id)
        if assigned_drop_id is None:
            return
        current_minutes = drop_data.get("currentMinutesWatched")
        required_minutes = drop_data.get("requiredMinutesWatched")
        if (
            type(current_minutes) is not int
            or type(required_minutes) is not int
            or current_minutes < 0
            or required_minutes < 0
            or current_minutes > required_minutes
        ):
            logger.warning("Twitch returned invalid progress for %s", target.name)
            return
        gql_drop = self._twitch._drops.get(drop_id)
        if gql_drop is None:
            if self._request_resync(f"unknown-current-drop:{drop_id}"):
                logger.warning(
                    "Twitch reported an unknown current drop for %s: %s",
                    target.name,
                    drop_id,
                )
            self._watch.disable_secondary(target)
            self._watch.block_channel(target.id)
            return
        if drop_id != assigned_drop_id:
            assigned_elsewhere = self._watch.assigned_channel_for_drop(drop_id)
            if assigned_elsewhere is not None and assigned_elsewhere.id != target.id:
                logger.log(
                    CALL,
                    "Ignoring duplicate current drop %s already assigned elsewhere",
                    drop_id,
                )
                return
            if not gql_drop.can_earn(target):
                if self._request_resync(f"ineligible-current-drop:{drop_id}"):
                    logger.warning(
                        "Twitch current drop %s is not locally eligible on %s",
                        drop_id,
                        target.name,
                    )
                self._watch.disable_secondary(target)
                self._watch.block_channel(target.id)
                return
            self._watch.assign_drop(target, drop_id)
            logger.info(
                "Reconciled watch assignment for %s: %s -> %s",
                target.name,
                assigned_drop_id,
                drop_id,
            )
        if gql_drop.is_claimed:
            logger.info("Twitch reported an already-claimed drop for %s", target.name)
            self.mark_completed_drop(drop_id)
            self._watch.disable_secondary(target)
            self._watch.block_channel(target.id)
            self._request_resync(f"claimed-current-drop:{drop_id}")
            return
        if not gql_drop.can_earn(target):
            if self._request_resync(f"lost-eligibility:{drop_id}"):
                logger.warning(
                    "Current drop %s is no longer locally eligible on %s",
                    drop_id,
                    target.name,
                )
            self._watch.disable_secondary(target)
            self._watch.block_channel(target.id)
            return
        previous_minutes = gql_drop.current_minutes
        try:
            gql_drop.update_minutes(current_minutes, required_minutes)
        except ValueError:
            logger.warning("Twitch returned inconsistent progress for %s", target.name)
            return
        if current_minutes >= required_minutes > 0:
            try:
                await gql_drop.generate_claim()
                claimed = await gql_drop.claim()
            except (GQLException, RequestException):
                claimed = False
            self._watch.block_channel(target.id)
            if claimed:
                self.mark_completed_drop(drop_id)
                self._claim_cooldowns.pop(drop_id, None)
                logger.info("Claimed completed current drop %s", drop_id)
            else:
                # Delay another synthetic claim attempt after a failed claim.
                gql_drop.claim_id = None
                self._claim_cooldowns[drop_id] = monotonic() + 300
                logger.warning("Could not claim completed current drop %s", drop_id)
            self._request_resync(f"completed-current-drop:{drop_id}")
            return
        self.display_primary_drop(gql_drop)
        if gql_drop.current_minutes > previous_minutes:
            logger.log(
                CALL,
                "Drop progress from GQL: %s (%s, %s/%s) on %s",
                gql_drop.name,
                gql_drop.campaign.game,
                gql_drop.current_minutes,
                gql_drop.required_minutes,
                target.name,
            )
