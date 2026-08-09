from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any

from constants import (
    CALL,
    GQL_QUERIES,
    MAX_WATCH_CHANNELS,
    WATCH_INTERVAL,
    State,
)
from exceptions import GQLException, RequestException
from translate import _
from utils import cancel_tasks, require_int, task_wrapper, timestamp

if TYPE_CHECKING:
    from channel import Channel
    from constants import JsonType
    from inventory import TimedDrop
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


class WatchService:
    """Select, reconcile, and supervise the active watch assignments."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._cooldown_handles: dict[int, asyncio.TimerHandle] = {}

    def reset(self) -> None:
        for handle in self._cooldown_handles.values():
            handle.cancel()
        self._cooldown_handles.clear()
        self._twitch._watch_channel_cooldowns.clear()
        self._twitch._watch_claim_cooldowns.clear()
        self._twitch._watch_resync_cooldowns.clear()
        self._twitch._watch_completed_drop_ids.clear()

    async def _watch_sleep(self, event: asyncio.Event, delay: float) -> bool:
        # Each watched channel owns an event so a restart wakes every watch loop.
        wait_task = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((wait_task,), timeout=max(delay, 0))
            return bool(done)
        finally:
            await cancel_tasks((wait_task,))
            event.clear()

    def _display_primary_drop(self, drop: TimedDrop) -> None:
        primary = self._twitch.watching_channel.get_with_default(None)
        if primary is not None and self._twitch._watch_drop_ids.get(primary.id) == drop.id:
            drop.display()

    def _mark_watch_completed_drop(self, drop_id: str) -> None:
        completed_drop_ids = getattr(self._twitch, "_watch_completed_drop_ids", None)
        if completed_drop_ids is None:
            completed_drop_ids = set()
            self._twitch._watch_completed_drop_ids = completed_drop_ids
        completed_drop_ids.add(drop_id)

    def _request_watch_resync(self, key: str, seconds: float = 300) -> bool:
        resync_cooldowns = getattr(self._twitch, "_watch_resync_cooldowns", None)
        if resync_cooldowns is None:
            resync_cooldowns = {}
            self._twitch._watch_resync_cooldowns = resync_cooldowns
        now = monotonic()
        if resync_cooldowns.get(key, 0) > now:
            return False
        resync_cooldowns[key] = now + seconds
        self._twitch.change_state(State.INVENTORY_FETCH)
        return True

    def _disable_dual_watch_if_secondary(self, channel: Channel) -> None:
        primary = self._twitch.watching_channel.get_with_default(None)
        if (
            primary is not None
            and primary.id != channel.id
            and len(self._twitch._watching_channels) > 1
            and self._twitch._dual_watch_enabled
        ):
            self._twitch._dual_watch_enabled = False
            logger.warning(
                "Disabling the second watch target after unscoped progress for %s",
                channel.name,
            )

    async def _reconcile_watch_progress(self, channel: Channel) -> None:
        """Refresh assigned progress from Twitch's authoritative viewer session."""
        if self._twitch._watch_drop_ids.get(channel.id) is None:
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

        # DropCurrentSessionContext is account-scoped in practice: Twitch can return
        # a stale session for a different channel even when channelID was supplied.
        # The Drop ID is the safest discriminator, followed by the reported channel.
        # A mismatch is therefore advisory and must not restart the productive target.
        if drop_id in getattr(self._twitch, "_watch_completed_drop_ids", set()):
            stale_channel: Channel | None = channel
            if reported_channel_id is not None:
                stale_channel = self._twitch._watching_channels.get(reported_channel_id)
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
            self._disable_dual_watch_if_secondary(stale_channel)
            self._block_watch_channel(stale_channel.id)
            self._twitch.change_state(State.CHANNEL_SWITCH)
            return

        target = channel
        if reported_channel_id is not None and reported_channel_id != channel.id:
            assigned_owner = next(
                (
                    candidate
                    for candidate, assigned in self._twitch._watch_drop_ids.items()
                    if assigned == drop_id and candidate in self._twitch._watching_channels
                ),
                None,
            )
            if assigned_owner is not None:
                target = self._twitch._watching_channels[assigned_owner]
                logger.log(
                    CALL,
                    "Routing current drop %s from %s to assigned channel %s",
                    drop_id,
                    channel.name,
                    target.name,
                )
            elif reported_channel_id in self._twitch._watching_channels:
                target = self._twitch._watching_channels[reported_channel_id]
                logger.log(
                    CALL,
                    "Routing current drop response from %s to reported channel %s",
                    channel.name,
                    target.name,
                )
            else:
                logger.warning(
                    "Ignoring stale current-drop session for %s: %s",
                    channel.name,
                    drop_id,
                )
                return

        assigned_drop_id = self._twitch._watch_drop_ids.get(target.id)
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
            if self._request_watch_resync(f"unknown-current-drop:{drop_id}"):
                logger.warning(
                    "Twitch reported an unknown current drop for %s: %s",
                    target.name,
                    drop_id,
                )
            self._disable_dual_watch_if_secondary(target)
            self._block_watch_channel(target.id)
            return
        if drop_id != assigned_drop_id:
            assigned_elsewhere = any(
                other_id != target.id and assigned == drop_id
                for other_id, assigned in self._twitch._watch_drop_ids.items()
            )
            if assigned_elsewhere:
                logger.log(
                    CALL,
                    "Ignoring duplicate current drop %s already assigned elsewhere",
                    drop_id,
                )
                return
            if not gql_drop.can_earn(target):
                if self._request_watch_resync(f"ineligible-current-drop:{drop_id}"):
                    logger.warning(
                        "Twitch current drop %s is not locally eligible on %s",
                        drop_id,
                        target.name,
                    )
                self._disable_dual_watch_if_secondary(target)
                self._block_watch_channel(target.id)
                return
            self._twitch._watch_drop_ids[target.id] = drop_id
            restart_event = self._twitch._watch_restart_events.get(target.id)
            if restart_event is not None:
                restart_event.set()
            logger.info(
                "Reconciled watch assignment for %s: %s -> %s",
                target.name,
                assigned_drop_id,
                drop_id,
            )
        if gql_drop.is_claimed:
            logger.info("Twitch reported an already-claimed drop for %s", target.name)
            self._mark_watch_completed_drop(drop_id)
            self._disable_dual_watch_if_secondary(target)
            self._block_watch_channel(target.id)
            self._request_watch_resync(f"claimed-current-drop:{drop_id}")
            return
        if not gql_drop.can_earn(target):
            if self._request_watch_resync(f"lost-eligibility:{drop_id}"):
                logger.warning(
                    "Current drop %s is no longer locally eligible on %s",
                    drop_id,
                    target.name,
                )
            self._disable_dual_watch_if_secondary(target)
            self._block_watch_channel(target.id)
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
            self._block_watch_channel(target.id)
            if claimed:
                self._mark_watch_completed_drop(drop_id)
                self._twitch._watch_claim_cooldowns.pop(drop_id, None)
                logger.info("Claimed completed current drop %s", drop_id)
            else:
                # Do not let the normal inventory claim pass immediately retry
                # the same synthetic claim ID; retry it only after a cooldown.
                gql_drop.claim_id = None
                self._twitch._watch_claim_cooldowns[drop_id] = monotonic() + 300
                logger.warning("Could not claim completed current drop %s", drop_id)
            self._request_watch_resync(f"completed-current-drop:{drop_id}")
            return
        self._display_primary_drop(gql_drop)
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

    async def _watch_channel_loop(
        self, channel: Channel, restart_event: asyncio.Event, generation: int
    ) -> None:
        interval = WATCH_INTERVAL.total_seconds()
        try:
            while (
                generation == self._twitch._watch_generation
                and self._twitch._watching_channels.get(channel.id) is channel
                and channel.id in self._twitch._watch_drop_ids
            ):
                if not channel.online or not self.can_watch(channel):
                    self._twitch.change_state(State.CHANNEL_SWITCH)
                    return
                succeeded = await channel.send_watch()
                last_sent = monotonic()
                if not succeeded:
                    logger.log(CALL, "Watch request failed for channel: %s", channel.name)
                if await self._watch_sleep(restart_event, 20):
                    continue
                primary = self._twitch.watching_channel.get_with_default(None)
                if channel is not primary or self._twitch.gui.progress.minute_almost_done():
                    await self._reconcile_watch_progress(channel)
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
            self._block_watch_channel(channel.id, seconds=60)
            self._twitch.change_state(State.CHANNEL_SWITCH)

    def _watch_task_done(self, channel_id: int, task: asyncio.Task[None]) -> None:
        if self._twitch._watch_tasks.get(channel_id) is task:
            del self._twitch._watch_tasks[channel_id]
        if not task.cancelled() and task.exception() is not None:
            logger.error("Watch task failed for channel %s", channel_id)

    def _schedule_channel_release(
        self,
        channel_id: int,
        blocked_until: float,
    ) -> None:
        previous = self._cooldown_handles.pop(channel_id, None)
        if previous is not None:
            previous.cancel()
        try:
            handle = asyncio.get_running_loop().call_later(
                max(0, blocked_until - monotonic()),
                self._release_watch_channel,
                channel_id,
                blocked_until,
            )
        except RuntimeError:
            return
        self._cooldown_handles[channel_id] = handle

    def _release_watch_channel(self, channel_id: int, blocked_until: float) -> None:
        channel_cooldowns = getattr(self._twitch, "_watch_channel_cooldowns", {})
        if channel_cooldowns.get(channel_id) != blocked_until:
            return
        self._cooldown_handles.pop(channel_id, None)
        remaining = blocked_until - monotonic()
        if remaining > 0:
            self._schedule_channel_release(channel_id, blocked_until)
            return
        del channel_cooldowns[channel_id]
        self._twitch.change_state(State.CHANNEL_SWITCH)

    def _block_watch_channel(self, channel_id: int, seconds: float = 300) -> None:
        channel_cooldowns = getattr(self._twitch, "_watch_channel_cooldowns", None)
        if channel_cooldowns is None:
            channel_cooldowns = {}
            self._twitch._watch_channel_cooldowns = channel_cooldowns
        blocked_until = max(
            channel_cooldowns.get(channel_id, 0),
            monotonic() + seconds,
        )
        channel_cooldowns[channel_id] = blocked_until
        self._schedule_channel_release(channel_id, blocked_until)

    def _eligible_drops_for_channel(self, channel: Channel) -> list[TimedDrop]:
        candidates: list[TimedDrop] = []
        seen: set[str] = set()
        now = monotonic()
        claim_cooldowns = getattr(self._twitch, "_watch_claim_cooldowns", {})
        completed_drop_ids = getattr(self._twitch, "_watch_completed_drop_ids", set())
        for campaign in self._twitch.inventory:
            if not campaign.can_earn(channel):
                continue
            for drop in campaign.drops:
                if drop.id in completed_drop_ids:
                    continue
                blocked_until = claim_cooldowns.get(drop.id)
                if blocked_until is not None:
                    if blocked_until > now:
                        continue
                    claim_cooldowns.pop(drop.id, None)
                if drop.id not in seen and drop.can_earn(channel):
                    candidates.append(drop)
                    seen.add(drop.id)
        candidates.sort(key=lambda drop: (drop.remaining_minutes, drop.ends_at))
        return candidates

    def _select_watch_assignments(
        self, preferred: Channel | None = None
    ) -> list[tuple[Channel, TimedDrop]]:
        """Select up to two assignments with a unique game and drop per target."""
        ordered = self._twitch._rank_channels(self._twitch.channels.values())
        if preferred is not None and preferred in ordered:
            ordered.remove(preferred)
            ordered.insert(0, preferred)
        now = monotonic()
        channel_cooldowns = getattr(self._twitch, "_watch_channel_cooldowns", {})
        for candidate in ordered:
            blocked_until = channel_cooldowns.get(candidate.id)
            if blocked_until is not None and blocked_until <= now:
                del channel_cooldowns[candidate.id]
        options = [
            (candidate, self._eligible_drops_for_channel(candidate))
            for candidate in ordered
            if candidate.game is not None
            and self.can_watch(candidate)
            and channel_cooldowns.get(candidate.id, 0) <= now
        ]
        for first_index, (first_channel, first_drops) in enumerate(options):
            for first_drop in first_drops:
                first_assignment = (first_channel, first_drop)
                if MAX_WATCH_CHANNELS == 1 or not getattr(self._twitch, "_dual_watch_enabled", True):
                    return [first_assignment]
                for second_channel, second_drops in options[first_index + 1:]:
                    if second_channel.game == first_channel.game:
                        continue
                    for second_drop in second_drops:
                        if second_drop.id != first_drop.id:
                            return [first_assignment, (second_channel, second_drop)]
        if options:
            return [(options[0][0], options[0][1][0])]
        return []

    def _select_watch_channels(self, preferred: Channel | None = None) -> list[Channel]:
        return [channel for channel, _drop in self._select_watch_assignments(preferred)]

    def _apply_watch_assignments(
        self,
        assignments: list[tuple[Channel, TimedDrop]],
        *,
        update_status: bool = True,
    ) -> None:
        max_targets = (
            MAX_WATCH_CHANNELS if getattr(self._twitch, "_dual_watch_enabled", True) else 1
        )
        assignments = assignments[:max_targets]
        channels = [channel for channel, _drop in assignments]
        targets = OrderedDict((channel.id, channel) for channel in channels)
        target_drop_ids = {channel.id: drop.id for channel, drop in assignments}
        generation = self._bump_watch_generation()
        for event in self._twitch._watch_restart_events.values():
            event.set()
        for task in self._twitch._watch_tasks.values():
            task.cancel()
        self._twitch._watch_tasks.clear()
        self._twitch._watch_restart_events.clear()
        self._twitch._watching_channels = targets
        self._twitch._watch_drop_ids = target_drop_ids
        for channel in channels:
            event = asyncio.Event()
            self._twitch._watch_restart_events[channel.id] = event
            task = asyncio.create_task(
                self._watch_channel_loop(channel, event, generation)
            )
            self._twitch._watch_tasks[channel.id] = task
            task.add_done_callback(
                lambda completed, channel_id=channel.id: self._watch_task_done(
                    channel_id, completed
                )
            )
        primary = channels[0] if channels else None
        history_signature = getattr(self._twitch, "_history_watch_signature", None)
        if primary is None:
            if history_signature is not None:
                self._twitch.history_event(
                    "watch.stopped",
                    data={"targets": len(history_signature)},
                )
                self._twitch._history_watch_signature = None
            self._twitch._watch_drop_ids.clear()
            self._twitch.watching_channel.clear()
            self._twitch.gui.channels.clear_watching()
            return
        signature = tuple((channel.id, drop.id) for channel, drop in assignments)
        if signature != history_signature:
            self._twitch.history_event(
                "watch.started" if history_signature is None else "watch.changed",
                data={
                    "channels": ", ".join(channel.name for channel in channels),
                    "targets": len(signature),
                },
            )
            self._twitch._history_watch_signature = signature
        self._twitch.watching_channel.set(primary)
        set_watching_channels = getattr(self._twitch.gui.channels, "set_watching_channels", None)
        if set_watching_channels is not None:
            set_watching_channels(channels)
        else:
            self._twitch.gui.channels.set_watching(primary)
        if getattr(self._twitch.gui, "display_drop", None) is not None:
            assignments[0][1].display(countdown=False, subone=True)
        if update_status:
            status_text = _("status", "watching").format(channel=primary.name)
            self._twitch.print(status_text)
            self._twitch.gui.status.update(status_text)
        if len(assignments) > 1:
            logger.info(
                "Watching distinct drop targets: %s (%s) and %s (%s)",
                assignments[0][0].name,
                assignments[0][1].id,
                assignments[1][0].name,
                assignments[1][1].id,
            )

    @task_wrapper(critical=True)
    async def _maintenance_task(self) -> None:
        now = datetime.now(timezone.utc)
        next_period = now + timedelta(hours=1)
        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            next_trigger = next_period
            while self._twitch._mnt_triggers and self._twitch._mnt_triggers[0] <= next_trigger:
                next_trigger = self._twitch._mnt_triggers.popleft()
            trigger_type: str = "Reload" if next_trigger == next_period else "Cleanup"
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                )
            )
            await asyncio.sleep((next_trigger - now).total_seconds())
            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            if next_trigger != next_period:
                logger.log(CALL, "Maintenance task requests channels cleanup")
                self._twitch.change_state(State.CHANNELS_CLEANUP)
        # this triggers a restart of this task every (up to) 60 minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self._twitch.change_state(State.INVENTORY_FETCH)

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.
        """
        # exit early if stream is offline
        if not channel.online:
            return False
        for campaign in self._twitch.inventory:
            if (
                campaign.can_earn(channel)  # let the campaign do the "special games" check
                and (
                    # limit watching to the games the user wants
                    channel.game is not None
                    and channel.drops_enabled
                    and channel.game in self._twitch.wanted_games
                    # let the campaign ignore all channel-related checks
                    or campaign.game.is_special()
                )
            ):
                return True
        return False

    def should_switch(self, channel: Channel) -> bool:
        """Return whether a channel should enter the distinct watch set."""
        if not self.can_watch(channel) or channel.id in self._twitch._watching_channels:
            return False
        watching_channel = self._twitch.watching_channel.get_with_default(None)
        if watching_channel is None or not self.can_watch(watching_channel):
            return True
        selected = self._select_watch_channels(preferred=channel)
        if channel.id not in {candidate.id for candidate in selected}:
            return False
        if len(self._twitch._watching_channels) < MAX_WATCH_CHANNELS:
            return True
        current_worst = max(
            self._twitch._watching_channels.values(),
            key=lambda candidate: (self._twitch.get_priority(candidate), not candidate.acl_based),
        )
        return (
            self._twitch.get_priority(channel) < self._twitch.get_priority(current_worst)
            or (
                self._twitch.get_priority(channel) == self._twitch.get_priority(current_worst)
                and channel.acl_based > current_worst.acl_based
            )
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        self._twitch.gui.tray.change_icon("active")
        assignments = self._select_watch_assignments(preferred=channel)
        self._apply_watch_assignments(assignments, update_status=update_status)

    def _bump_watch_generation(self) -> int:
        self._twitch._watch_generation += 1
        return self._twitch._watch_generation

    async def stop_watching_and_wait(self) -> None:
        """Stop every watch loop and consume cancellation before returning."""
        tasks = tuple(self._twitch._watch_tasks.values())
        self.stop_watching()
        await cancel_tasks(tasks)

    def stop_watching(self):
        history_signature = getattr(self._twitch, "_history_watch_signature", None)
        if history_signature is not None:
            self._twitch.history_event(
                "watch.stopped",
                data={"targets": len(history_signature)},
            )
            self._twitch._history_watch_signature = None
        self._twitch.gui.clear_drop()
        self._bump_watch_generation()
        for event in self._twitch._watch_restart_events.values():
            event.set()
        for task in self._twitch._watch_tasks.values():
            task.cancel()
        self._twitch._watch_tasks.clear()
        self._twitch._watch_restart_events.clear()
        self._twitch._watching_channels.clear()
        self._twitch._watch_drop_ids.clear()
        self._twitch.watching_channel.clear()
        self._twitch.gui.channels.clear_watching()

    def restart_watching(self):
        self._twitch.gui.progress.stop_timer()
        for event in self._twitch._watch_restart_events.values():
            event.set()
