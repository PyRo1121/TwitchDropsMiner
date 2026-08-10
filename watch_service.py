from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from time import monotonic
from typing import TYPE_CHECKING

from constants import MAX_WATCH_CHANNELS, State
from translate import _
from utils import AwaitableValue, cancel_tasks
from watch_progress_service import WatchProgressService

if TYPE_CHECKING:
    from channel import Channel
    from inventory import TimedDrop
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
        self._channel_cooldowns: dict[int, float] = {}
        self._generation = 0
        self._history_signature: tuple[tuple[int, str], ...] | None = None
        self._dual_watch_enabled = False
        self._cooldown_handles: dict[int, asyncio.TimerHandle] = {}
        self.progress = WatchProgressService(twitch, self)
        self.start_session()

    def start_session(self) -> None:
        self._history_signature = None
        self._dual_watch_enabled = self._twitch.settings.experimental_dual_watch

    def _target_limit(self) -> int:
        return MAX_WATCH_CHANNELS if self._dual_watch_enabled else 1

    async def close(self) -> None:
        await self.stop_watching_and_wait()
        self.reset()

    def reset(self) -> None:
        for handle in self._cooldown_handles.values():
            handle.cancel()
        self._cooldown_handles.clear()
        self._channel_cooldowns.clear()
        self.progress.reset()

    def is_watching(self, channel: Channel) -> bool:
        return channel.id in self._watching_channels

    def active_channels(self) -> tuple[Channel, ...]:
        return tuple(self._watching_channels.values())

    def active_channel(self, channel_id: int) -> Channel | None:
        return self._watching_channels.get(channel_id)

    def assigned_drop_id(self, channel_id: int) -> str | None:
        return self._drop_ids.get(channel_id)

    def assignment_is_current(self, channel: Channel, generation: int) -> bool:
        return (
            generation == self._generation
            and self._watching_channels.get(channel.id) is channel
            and channel.id in self._drop_ids
        )

    def assigned_channel_for_drop(self, drop_id: str) -> Channel | None:
        return next(
            (
                channel
                for channel_id, channel in self._watching_channels.items()
                if self._drop_ids.get(channel_id) == drop_id
            ),
            None,
        )

    def assign_drop(self, channel: Channel, drop_id: str) -> str | None:
        previous_drop_id = self._drop_ids.get(channel.id)
        self._drop_ids[channel.id] = drop_id
        restart_event = self._restart_events.get(channel.id)
        if restart_event is not None:
            restart_event.set()
        return previous_drop_id

    def disable_secondary(self, channel: Channel) -> None:
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

    def block_channel(self, channel_id: int, seconds: float = 300) -> None:
        blocked_until = max(
            self._channel_cooldowns.get(channel_id, 0),
            monotonic() + seconds,
        )
        self._channel_cooldowns[channel_id] = blocked_until
        self._schedule_channel_release(channel_id, blocked_until)

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
        options: list[tuple[Channel, list[TimedDrop]]] = []
        for candidate in ordered:
            if (
                candidate.game is None
                or not self.can_watch(candidate)
                or channel_cooldowns.get(candidate.id, 0) > now
            ):
                continue
            eligible_drops = self.progress.eligible_drops_for_channel(candidate)
            if eligible_drops:
                options.append((candidate, eligible_drops))
        for first_index, (first_channel, first_drops) in enumerate(options):
            for first_drop in first_drops:
                first_assignment = (first_channel, first_drop)
                if self._target_limit() == 1:
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
        assignments = assignments[:self._target_limit()]
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
                self.progress.run_channel(channel, event, generation)
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
        self._twitch.gui.channels.set_watching_channels(channels)
        self._twitch.gui.display_drop(
            assignments[0][1],
            countdown=False,
            subone=True,
        )
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
        if len(self._watching_channels) < self._target_limit():
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
        elif watching_channel is not None and self.can_watch(watching_channel):
            self.watch(watching_channel, update_status=False)
            self._twitch.gui.status.update(
                _("status", "watching").format(channel=watching_channel.name)
            )
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
