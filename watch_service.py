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
from utils import AwaitableValue, cancel_tasks, require_int, task_wrapper, timestamp

if TYPE_CHECKING:
    from channel import Channel
    from constants import JsonType
    from inventory import DropsCampaign, TimedDrop
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


class WatchService:
    """Select, reconcile, and supervise the active watch assignments."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self.primary_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_channels: OrderedDict[int, Channel] = OrderedDict()
        self._drop_ids: dict[int, str] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._restart_events: dict[int, asyncio.Event] = {}
        self._claim_cooldowns: dict[str, float] = {}
        self._completed_drop_ids: set[str] = set()
        self._channel_cooldowns: dict[int, float] = {}
        self._resync_cooldowns: dict[str, float] = {}
        self._generation = 0
        self._history_signature: tuple[tuple[int, str], ...] | None = None
        self._dual_watch_enabled = False
        self._cooldown_handles: dict[int, asyncio.TimerHandle] = {}
        self.start_session()

    def start_session(self) -> None:
        self._history_signature = None
        settings = getattr(self._twitch, "settings", None)
        self._dual_watch_enabled = bool(
            getattr(settings, "experimental_dual_watch", False)
        )

    async def close(self) -> None:
        await self.stop_watching_and_wait()
        self.reset()

    def retain_claim_cooldowns(self, drops: dict[str, TimedDrop]) -> None:
        now = monotonic()
        self._claim_cooldowns = {
            drop_id: blocked_until
            for drop_id, blocked_until in self._claim_cooldowns.items()
            if blocked_until > now
            and drop_id in drops
            and not drops[drop_id].is_claimed
        }

    def reset(self) -> None:
        for handle in self._cooldown_handles.values():
            handle.cancel()
        self._cooldown_handles.clear()
        self._channel_cooldowns.clear()
        self._claim_cooldowns.clear()
        self._resync_cooldowns.clear()
        self._completed_drop_ids.clear()

    async def _watch_sleep(self, event: asyncio.Event, delay: float) -> bool:
        # Each watched channel owns an event so a restart wakes every watch loop.
        wait_task = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((wait_task,), timeout=max(delay, 0))
            return bool(done)
        finally:
            await cancel_tasks((wait_task,))
            event.clear()

    def display_primary_drop(self, drop: TimedDrop) -> None:
        primary = self.primary_channel.get_with_default(None)
        if primary is not None and self._drop_ids.get(primary.id) == drop.id:
            drop.display()

    def mark_completed_drop(self, drop_id: str) -> None:
        self._completed_drop_ids.add(drop_id)

    def _request_watch_resync(self, key: str, seconds: float = 300) -> bool:
        now = monotonic()
        if self._resync_cooldowns.get(key, 0) > now:
            return False
        self._resync_cooldowns[key] = now + seconds
        self._twitch.change_state(State.INVENTORY_FETCH)
        return True

    def is_watching(self, channel: Channel) -> bool:
        return channel.id in self._watching_channels

    def assigned_channels(self, drop_id: str) -> list[Channel]:
        return [
            channel
            for channel in self._watching_channels.values()
            if self._drop_ids.get(channel.id) == drop_id
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
                for channel in self._watching_channels.values()
                if drop.can_earn(channel)
            ]
            if drop is not None
            else []
        )
        if drop is None or len(candidates) != 1:
            if self._request_watch_resync(f"unassigned-drop:{drop_id}"):
                logger.warning(
                    "Ignoring an event for an unassigned drop: %s",
                    drop_id,
                )
            return []

        channel = candidates[0]
        previous_drop_id = self._drop_ids.get(channel.id)
        self._drop_ids[channel.id] = drop_id
        restart_event = self._restart_events.get(channel.id)
        if restart_event is not None:
            restart_event.set()
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
            self.can_watch(channel) for channel in self._watching_channels.values()
        ):
            primary = self.primary_channel.get_with_default(None)
            if primary is not None:
                self.watch(primary, update_status=False)
                self.restart_watching()
                return True
        elif not claimed and any(
            campaign.can_earn(channel) for channel in watching_channels
        ):
            self.restart_watching()
            return True
        return False

    def _disable_dual_watch_if_secondary(self, channel: Channel) -> None:
        primary = self.primary_channel.get_with_default(None)
        if (
            primary is not None
            and primary.id != channel.id
            and len(self._watching_channels) > 1
            and self._dual_watch_enabled
        ):
            self._dual_watch_enabled = False
            logger.warning(
                "Disabling the second watch target after unscoped progress for %s",
                channel.name,
            )

    async def _reconcile_watch_progress(self, channel: Channel) -> None:
        """Refresh assigned progress from Twitch's authoritative viewer session."""
        if self._drop_ids.get(channel.id) is None:
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
        if drop_id in self._completed_drop_ids:
            stale_channel: Channel | None = channel
            if reported_channel_id is not None:
                stale_channel = self._watching_channels.get(reported_channel_id)
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
                    for candidate, assigned in self._drop_ids.items()
                    if assigned == drop_id and candidate in self._watching_channels
                ),
                None,
            )
            if assigned_owner is not None:
                target = self._watching_channels[assigned_owner]
                logger.log(
                    CALL,
                    "Routing current drop %s from %s to assigned channel %s",
                    drop_id,
                    channel.name,
                    target.name,
                )
            elif reported_channel_id in self._watching_channels:
                target = self._watching_channels[reported_channel_id]
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

        assigned_drop_id = self._drop_ids.get(target.id)
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
                for other_id, assigned in self._drop_ids.items()
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
            self._drop_ids[target.id] = drop_id
            restart_event = self._restart_events.get(target.id)
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
            self.mark_completed_drop(drop_id)
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
                self.mark_completed_drop(drop_id)
                self._claim_cooldowns.pop(drop_id, None)
                logger.info("Claimed completed current drop %s", drop_id)
            else:
                # Do not let the normal inventory claim pass immediately retry
                # the same synthetic claim ID; retry it only after a cooldown.
                gql_drop.claim_id = None
                self._claim_cooldowns[drop_id] = monotonic() + 300
                logger.warning("Could not claim completed current drop %s", drop_id)
            self._request_watch_resync(f"completed-current-drop:{drop_id}")
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

    async def _watch_channel_loop(
        self, channel: Channel, restart_event: asyncio.Event, generation: int
    ) -> None:
        interval = WATCH_INTERVAL.total_seconds()
        try:
            while (
                generation == self._generation
                and self._watching_channels.get(channel.id) is channel
                and channel.id in self._drop_ids
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
                primary = self.primary_channel.get_with_default(None)
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
        if self._tasks.get(channel_id) is task:
            del self._tasks[channel_id]
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
        channel_cooldowns = self._channel_cooldowns
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
        blocked_until = max(
            self._channel_cooldowns.get(channel_id, 0),
            monotonic() + seconds,
        )
        self._channel_cooldowns[channel_id] = blocked_until
        self._schedule_channel_release(channel_id, blocked_until)

    def _eligible_drops_for_channel(self, channel: Channel) -> list[TimedDrop]:
        candidates: list[TimedDrop] = []
        seen: set[str] = set()
        now = monotonic()
        claim_cooldowns = self._claim_cooldowns
        completed_drop_ids = self._completed_drop_ids
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
        ordered = self._twitch.channel_directory_service.rank_channels(
            self._twitch.channels.values()
        )
        if preferred is not None and preferred in ordered:
            ordered.remove(preferred)
            ordered.insert(0, preferred)
        now = monotonic()
        channel_cooldowns = self._channel_cooldowns
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
                if MAX_WATCH_CHANNELS == 1 or not self._dual_watch_enabled:
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
            MAX_WATCH_CHANNELS if self._dual_watch_enabled else 1
        )
        assignments = assignments[:max_targets]
        channels = [channel for channel, _drop in assignments]
        targets = OrderedDict((channel.id, channel) for channel in channels)
        target_drop_ids = {channel.id: drop.id for channel, drop in assignments}
        generation = self._bump_watch_generation()
        self._cancel_watch_tasks()
        self._watching_channels = targets
        self._drop_ids = target_drop_ids
        for channel in channels:
            event = asyncio.Event()
            self._restart_events[channel.id] = event
            task = asyncio.create_task(
                self._watch_channel_loop(channel, event, generation)
            )
            self._tasks[channel.id] = task
            task.add_done_callback(
                lambda completed, channel_id=channel.id: self._watch_task_done(
                    channel_id, completed
                )
            )
        primary = channels[0] if channels else None
        history_signature = self._history_signature
        if primary is None:
            if history_signature is not None:
                self._twitch.history_event(
                    "watch.stopped",
                    data={"targets": len(history_signature)},
                )
                self._history_signature = None
            self._drop_ids.clear()
            self.primary_channel.clear()
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
            self._history_signature = signature
        self.primary_channel.set(primary)
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
        if not self.can_watch(channel) or channel.id in self._watching_channels:
            return False
        watching_channel = self.primary_channel.get_with_default(None)
        if watching_channel is None or not self.can_watch(watching_channel):
            return True
        selected = self._select_watch_channels(preferred=channel)
        if channel.id not in {candidate.id for candidate in selected}:
            return False
        if len(self._watching_channels) < MAX_WATCH_CHANNELS:
            return True
        get_priority = self._twitch.channel_directory_service.get_priority
        current_worst = max(
            self._watching_channels.values(),
            key=lambda candidate: (get_priority(candidate), not candidate.acl_based),
        )
        candidate_priority = get_priority(channel)
        current_priority = get_priority(current_worst)
        return candidate_priority < current_priority or (
            candidate_priority == current_priority
            and channel.acl_based > current_worst.acl_based
        )

    def handle_idle_state(self) -> bool:
        if self._twitch.settings.dump:
            self._twitch.gui.close()
            return True
        self._twitch.gui.tray.change_icon("idle")
        self._twitch.gui.status.update(_("gui", "status", "idle"))
        self.stop_watching()
        self._twitch._state_change.clear()
        return False

    def switch_channel(self, channels: OrderedDict[int, Channel]) -> bool:
        if self._twitch.settings.dump:
            self._twitch.gui.close()
            return True
        self._twitch.gui.status.update(_("gui", "status", "switching"))
        selected_channel = self._twitch.gui.channels.get_selection()
        new_watching = None
        if selected_channel is not None and self.can_watch(selected_channel):
            new_watching = selected_channel
        else:
            for channel in sorted(
                channels.values(),
                key=self._twitch.channel_directory_service.get_priority,
            ):
                if self.should_switch(channel):
                    new_watching = channel
                    break

        watching_channel = self.primary_channel.get_with_default(None)
        if new_watching is not None:
            self.watch(new_watching)
            self._twitch._state_change.clear()
        elif watching_channel is not None and self.can_watch(watching_channel):
            self.watch(watching_channel, update_status=False)
            self._twitch.gui.status.update(
                _("status", "watching").format(channel=watching_channel.name)
            )
            self._twitch._state_change.clear()
        else:
            self._twitch.print(_("status", "no_channel"))
            self._twitch.history_event(
                "watch.unavailable",
                severity="warning",
                data={"reason": "no_eligible_channel"},
            )
            self._twitch.change_state(State.IDLE)
        return False

    def watch(self, channel: Channel, *, update_status: bool = True) -> None:
        self._twitch.gui.tray.change_icon("active")
        assignments = self._select_watch_assignments(preferred=channel)
        self._apply_watch_assignments(assignments, update_status=update_status)

    def _bump_watch_generation(self) -> int:
        self._generation += 1
        return self._generation

    def _cancel_watch_tasks(self) -> None:
        for event in self._restart_events.values():
            event.set()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._restart_events.clear()

    async def stop_watching_and_wait(self) -> None:
        """Stop every watch loop and consume cancellation before returning."""
        tasks = tuple(self._tasks.values())
        self.stop_watching()
        await cancel_tasks(tasks)

    def stop_watching(self) -> None:
        history_signature = self._history_signature
        if history_signature is not None:
            self._twitch.history_event(
                "watch.stopped",
                data={"targets": len(history_signature)},
            )
            self._history_signature = None
        self._twitch.gui.clear_drop()
        self._bump_watch_generation()
        self._cancel_watch_tasks()
        self._watching_channels.clear()
        self._drop_ids.clear()
        self.primary_channel.clear()
        self._twitch.gui.channels.clear_watching()

    def restart_watching(self) -> None:
        self._twitch.gui.progress.stop_timer()
        for event in self._restart_events.values():
            event.set()
