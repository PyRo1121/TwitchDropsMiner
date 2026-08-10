from __future__ import annotations

import asyncio
import logging
from math import floor
from time import monotonic
from functools import partial
from collections import abc, deque, OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Final, TYPE_CHECKING

import aiohttp
from translate import _
from auth import AuthState
from channel import Channel
from channel_directory_service import ChannelDirectoryService
from channel_event_service import ChannelEventService
from websocket import WebsocketPool
from watch_service import WatchService
from inventory import DropsCampaign
from inventory_service import InventoryService
from drop_event_service import DropEventService
from session_history import HistoryEvent, Scalar, SessionHistory, Severity
from http_transport import HttpTransport
from exceptions import (
    ExitRequest,
    ReloadRequest,
    LoginException,
    RequestInvalid,
    RequestException,
)
from utils import (
    timestamp,
    cancel_tasks,
    AwaitableValue,
    redact_log_value,
)
from constants import (
    MAX_INT,
    DUMP_PATH,
    MAX_CHANNELS,
    INVENTORY_RETRY_BASE,
    INVENTORY_RETRY_MAX,
    State,
    ClientType,
    PriorityMode,
    WebsocketTopic,
)

if TYPE_CHECKING:
    from game import Game
    from gui_port import GuiPort
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo


logger = logging.getLogger("TwitchDrops")


def _open_dump(mode: Literal["w", "a"]) -> Any:
    try:
        return open(DUMP_PATH, mode, encoding="utf8")
    except OSError as exc:
        raise RuntimeError(f"Unable to open dump file: {DUMP_PATH}") from exc


class Twitch:
    def __init__(
        self,
        settings: Settings,
        gui_factory: Callable[["Twitch"], GuiPort],
    ):
        self.settings: Settings = settings
        retention_days = getattr(settings, "history_retention_days", 90)
        if not isinstance(retention_days, int) or isinstance(retention_days, bool):
            retention_days = 90
        retention_days = max(1, min(retention_days, 3650))
        self.history = SessionHistory(retention_days=retention_days)
        # State management
        self._state: State = State.IDLE
        self._state_generation = 0
        self._state_change = asyncio.Event()
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._inventory_generation = 0
        self._inventory_retry_attempt = 0
        self._inventory_retry_task: asyncio.Task[None] | None = None
        self._mnt_triggers: deque[datetime] = deque()
        # Client type, transport, and auth
        self._client_type: ClientInfo = ClientType.ANDROID_APP
        self.transport = HttpTransport(self)
        self._auth_state = AuthState(self)
        self.inventory_service = InventoryService(self)
        self.drop_event_service = DropEventService(self)
        self.channel_event_service = ChannelEventService(self)
        self.channel_directory_service = ChannelDirectoryService(self)
        self.gui: GuiPort = gui_factory(self)
        # Storing and watching channels
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_channels: OrderedDict[int, Channel] = OrderedDict()
        self._watch_drop_ids: dict[int, str] = {}
        self._watch_tasks: dict[int, asyncio.Task[None]] = {}
        self._watch_restart_events: dict[int, asyncio.Event] = {}
        self._watch_claim_cooldowns: dict[str, float] = {}
        self._watch_completed_drop_ids: set[str] = set()
        self._watch_channel_cooldowns: dict[int, float] = {}
        self._watch_resync_cooldowns: dict[str, float] = {}
        self._watch_generation = 0
        self._dual_watch_enabled = bool(
            getattr(settings, "experimental_dual_watch", False)
        )
        self._history_auth_recorded = False
        self._history_watch_signature: tuple[tuple[int, str], ...] | None = None
        self._history_deadline_alerts: set[str] = set()
        self.watch_service: WatchService = WatchService(self)
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None

    async def shutdown(self) -> None:
        start_time = monotonic()
        self._inventory_generation += 1
        background_tasks: list[asyncio.Task[Any]] = list(self._watch_tasks.values())
        self.watch_service.stop_watching()
        self.watch_service.reset()
        if self._mnt_task is not None:
            background_tasks.append(self._mnt_task)
            self._mnt_task = None
        if self._inventory_retry_task is not None:
            background_tasks.append(self._inventory_retry_task)
            self._inventory_retry_task = None
        self._inventory_retry_attempt = 0
        pending_channel_tasks = [
            channel._pending_stream_up
            for channel in self.channels.values()
            if channel._pending_stream_up is not None
        ]
        for channel in self.channels.values():
            channel.remove()
        await cancel_tasks((*background_tasks, *pending_channel_tasks))
        # stop websocket, close transport, and persist cookies
        await self.websocket.stop(clear_topics=True)
        await self.transport.close()
        try:
            await self.gui.inv.replace_campaigns(())
            self.gui.set_games(set())
        except Exception:
            logger.exception("Unable to clear account presentation during shutdown")
        self._drops.clear()
        self.channels.clear()
        self.inventory.clear()
        self._campaigns.clear()
        self._auth_state.clear()
        self.wanted_games.clear()
        self._mnt_triggers.clear()
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(max(0, start_time + 0.5 - monotonic()))

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        if self._state is not State.EXIT:
            # Prevent state changing once we switch to exit state. Every accepted
            # request gets a generation even when it requests the same state.
            self._state = state
            self._state_generation += 1
        self._state_change.set()

    def state_change(self, state: State) -> abc.Callable[[], None]:
        # this is identical to change_state, but defers the call
        # perfect for GUI usage
        return partial(self.change_state, state)

    def close(self):
        """
        Called when the application is requested to close by the user,
        usually by the console or application window being closed.
        """
        self.change_state(State.EXIT)

    def prevent_close(self):
        """
        Called when the application window has to be prevented from closing, even after the user
        closes it with X. Usually used solely to display tracebacks from the closing sequence.
        """
        self.gui.prevent_close()

    def print(self, message: str):
        """
        Can be used to print messages within the GUI.
        """
        self.gui.print(message)

    def history_event(
        self,
        kind: str,
        *,
        severity: Severity = "info",
        data: Mapping[str, Scalar] | None = None,
    ) -> None:
        history = getattr(self, "history", None)
        if history is None:
            return
        event: HistoryEvent | None = None
        try:
            event = history.record(kind, severity=severity, data=data)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.debug("Unable to record history event %s: %s", kind, type(exc).__name__)
        refresh = getattr(self.gui, "history_changed", None)
        if refresh is not None:
            refresh()
        on_event = getattr(self.gui, "on_history_event", None)
        if event is not None and on_event is not None:
            on_event(event)

    def save(self, *, force: bool = False) -> None:
        """
        Saves the application state.
        """
        self.gui.save(force=force)
        self.settings.save(force=force)

    async def run(self):
        self.history.start()
        self._history_auth_recorded = False
        self._history_watch_signature = None
        self._history_deadline_alerts.clear()
        refresh_history = getattr(self.gui, "history_changed", None)
        if refresh_history is not None:
            refresh_history()
        session_status: Literal["stopped", "failed"] = "stopped"
        failure_reason: str | None = None
        try:
            if self.settings.dump:
                with _open_dump("w"):
                    # replace the existing file with an empty one
                    pass
            while True:
                try:
                    await self._run()
                    break
                except Exception as exc:
                    if isinstance(exc, ReloadRequest):
                        self.history.record(
                            "session.reload",
                            data={"reason": "maintenance"},
                        )
                        await self.shutdown()
                    elif isinstance(exc, ExitRequest):
                        break
                    elif isinstance(exc, aiohttp.ContentTypeError):
                        session_status = "failed"
                        failure_reason = "unexpected_content"
                        raise RequestException(
                            _("login", "unexpected_content")
                        ) from exc
                    else:
                        raise
        except Exception as exc:
            session_status = "failed"
            if failure_reason is None:
                failure_reason = type(exc).__name__
            raise
        finally:
            finished = self.history.finish(session_status, reason=failure_reason)
            if refresh_history is not None:
                refresh_history()
            on_event = getattr(self.gui, "on_history_event", None)
            if finished is not None and finished.events and on_event is not None:
                on_event(finished.events[-1])

    async def _run(self):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        self.gui.start()
        # The second slot is experimental and opt-in until live canary evidence
        # proves Twitch credits both independent watch sessions reliably.
        self._dual_watch_enabled = bool(
            getattr(self.settings, "experimental_dual_watch", False)
        )
        try:
            auth_state = await self.get_auth()
        except (LoginException, RequestInvalid) as exc:
            self.history_event(
                "auth.required",
                severity="warning",
                data={"reason": type(exc).__name__},
            )
            raise
        if not self._history_auth_recorded:
            self.history_event("auth.restored")
            self._history_auth_recorded = True
        await self.websocket.start()
        # Watch tasks are created per channel when the first targets are selected.
        # Add default topics
        self.websocket.add_topics([
            WebsocketTopic(
                "User",
                "Drops",
                auth_state.user_id,
                self.drop_event_service.process_drops,
            ),
            WebsocketTopic(
                "User",
                "Notifications",
                auth_state.user_id,
                self.drop_event_service.process_notifications,
            ),
        ])
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        while True:
            if self._state is State.IDLE:
                if self._handle_idle_state():
                    continue
            elif self._state is State.INVENTORY_FETCH:
                await self._fetch_inventory_state()
            elif self._state is State.GAMES_UPDATE:
                await self._update_games_state()
                full_cleanup = True
                self.watch_service.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                full_cleanup = self._cleanup_channels_state(channels, full_cleanup)
            elif self._state is State.CHANNELS_FETCH:
                await self._fetch_channels(channels)
            elif self._state is State.CHANNEL_SWITCH:
                if self._switch_channel(channels):
                    continue
            elif self._state is State.RESTART:
                raise ReloadRequest()
            elif self._state is State.EXIT:
                self.gui.tray.change_icon("pickaxe")
                self.gui.status.update(_("gui", "status", "exiting"))
                # we've been requested to exit the application
                break
            await self._state_change.wait()

    async def _update_games_state(self) -> None:
        # claim drops from expired and active campaigns
        for campaign in self.inventory:
            if not campaign.upcoming:
                for drop in campaign.drops:
                    if drop.can_claim and await drop.claim():
                        self.watch_service._mark_watch_completed_drop(drop.id)
        # figure out which games we want
        self.wanted_games.clear()
        exclude = self.settings.exclude
        priority = self.settings.priority
        priority_mode = self.settings.priority_mode
        priority_only = priority_mode is PriorityMode.PRIORITY_ONLY
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        # sorted_campaigns: list[DropsCampaign] = list(self.inventory)
        sorted_campaigns: list[DropsCampaign] = self.inventory
        if not priority_only:
            if priority_mode is PriorityMode.ENDING_SOONEST:
                sorted_campaigns.sort(key=lambda c: c.ends_at)
            elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
                sorted_campaigns.sort(key=lambda c: c.availability)
        sorted_campaigns.sort(
            key=lambda c: (
                priority.index(c.game.name) if c.game.name in priority else MAX_INT
            )
        )
        for campaign in sorted_campaigns:
            game: Game = campaign.game
            if (
                game not in self.wanted_games  # isn't already there
                # and isn't excluded by list or priority mode
                and game.name not in exclude
                and (not priority_only or game.name in priority)
                # and can be progressed within the next hour
                and campaign.can_earn_within(next_hour)
            ):
                # non-excluded games with no priority are placed last, below priority ones
                self.wanted_games.append(game)

    def _handle_idle_state(self) -> bool:
        if self.settings.dump:
            self.gui.close()
            return True
        self.gui.tray.change_icon("idle")
        self.gui.status.update(_("gui", "status", "idle"))
        self.watch_service.stop_watching()
        # clear the flag and wait until it's set again
        self._state_change.clear()
        return False

    async def _retry_inventory_after(self, generation: int, delay: float) -> None:
        current_task = asyncio.current_task()
        try:
            await self.transport.wait_for_delay(delay)
        except ExitRequest:
            return
        finally:
            if self._inventory_retry_task is current_task:
                self._inventory_retry_task = None
        if (
            self._state_generation == generation
            and self._state is State.INVENTORY_FETCH
        ):
            self.change_state(State.INVENTORY_FETCH)

    async def _fetch_inventory_state(self) -> None:
        state_generation = self._state_generation
        self.gui.tray.change_icon("maint")
        # Keep the last-good snapshot and watch assignments active while a
        # replacement is fetched. InventoryService quiesces them at commit.
        await self.websocket.start()
        try:
            await self.inventory_service.fetch_inventory()
        except (ExitRequest, LoginException, RequestInvalid):
            raise
        except RequestException as exc:
            self._inventory_retry_attempt += 1
            delay = min(
                INVENTORY_RETRY_BASE
                * (2 ** min(self._inventory_retry_attempt - 1, 10)),
                INVENTORY_RETRY_MAX,
            )
            if self._inventory_retry_attempt == 1:
                self.history_event(
                    "inventory.sync_failed",
                    severity="warning",
                    data={"error_type": type(exc).__name__},
                )
            self.gui.status.update(
                _("gui", "status", "inventory_retry").format(
                    seconds=max(1, round(delay))
                )
            )
            retry_task = self._inventory_retry_task
            if retry_task is not None:
                await cancel_tasks((retry_task,))
            self._inventory_retry_task = asyncio.create_task(
                self._retry_inventory_after(state_generation, delay)
            )
            return
        except Exception as exc:
            self.history_event(
                "inventory.sync_failed",
                severity="warning",
                data={"error_type": type(exc).__name__},
            )
            raise

        retry_attempts = self._inventory_retry_attempt
        self._inventory_retry_attempt = 0
        if retry_attempts:
            self.history_event(
                "inventory.sync_recovered",
                data={"attempts": retry_attempts},
            )
        self.history_event(
            "inventory.synced",
            data={"campaigns": len(self.inventory), "drops": len(self._drops)},
        )
        self._record_campaign_deadlines()
        self.gui.set_games(set(campaign.game for campaign in self.inventory))
        # Save state on every inventory fetch. Do not overwrite a newer state
        # request (including a manual refresh requesting INVENTORY_FETCH again).
        self.save()
        if self._state_generation == state_generation:
            self.change_state(State.GAMES_UPDATE)

    def _record_campaign_deadlines(self) -> None:
        now = datetime.now(timezone.utc)
        for campaign in self.inventory:
            remaining = (campaign.ends_at - now).total_seconds()
            if campaign.finished or not campaign.active or not 0 < remaining <= 3600:
                continue
            if campaign.id in self._history_deadline_alerts:
                continue
            self._history_deadline_alerts.add(campaign.id)
            self.history_event(
                "campaign.deadline",
                severity="warning",
                data={
                    "campaign_id": campaign.id,
                    "campaign": campaign.name,
                    "game": campaign.game.name,
                    "remaining_minutes": max(1, floor(remaining / 60)),
                },
            )

    def _switch_channel(self, channels: OrderedDict[int, Channel]) -> bool:
        if self.settings.dump:
            self.gui.close()
            return True
        self.gui.status.update(_("gui", "status", "switching"))
        # Change into the selected channel, stay in the watching channel,
        # or select a new channel that meets the required conditions
        new_watching = None
        selected_channel = self.gui.channels.get_selection()
        if selected_channel is not None and self.watch_service.can_watch(selected_channel):
            # selected channel is checked first, and set as long as we can watch it
            new_watching = selected_channel
        else:
            # other channels additionally need to have a good reason
            # for a switch (including the watching one)
            # NOTE: we need to sort the channels every time because one channel
            # can end up streaming any game - channels aren't game-tied
            for channel in sorted(
                channels.values(),
                key=self.channel_directory_service.get_priority,
            ):
                if self.watch_service.should_switch(channel):
                    new_watching = channel
                    break
        watching_channel = self.watching_channel.get_with_default(None)
        if new_watching is not None:
            # if we have a better switch target - do so
            self.watch_service.watch(new_watching)
            # break the state change chain by clearing the flag
            self._state_change.clear()
        elif watching_channel is not None and self.watch_service.can_watch(watching_channel):
            # otherwise, continue watching what we had before and refill
            # the second distinct target if one is available.
            self.watch_service.watch(watching_channel, update_status=False)
            self.gui.status.update(
                _("status", "watching").format(channel=watching_channel.name)
            )
            # break the state change chain by clearing the flag
            self._state_change.clear()
        else:
            # not watching anything and there isn't anything to watch either
            self.print(_("status", "no_channel"))
            self.history_event(
                "watch.unavailable",
                severity="warning",
                data={"reason": "no_eligible_channel"},
            )
            self.change_state(State.IDLE)
        del new_watching, selected_channel, watching_channel
        return False

    def _cleanup_channels_state(
        self, channels: OrderedDict[int, Channel], full_cleanup: bool
    ) -> bool:
        self.gui.status.update(_("gui", "status", "cleanup"))
        if not self.wanted_games or full_cleanup:
            # no games selected or we're doing full cleanup: remove everything
            to_remove_channels: list[Channel] = list(channels.values())
        else:
            # remove all channels that:
            to_remove_channels = [
                channel
                for channel in channels.values()
                if (
                    not channel.acl_based  # aren't ACL-based
                    and (
                        channel.offline  # and are offline
                        # or online but aren't streaming the game we want anymore
                        or (channel.game is None or channel.game not in self.wanted_games)
                    )
                )
            ]
        full_cleanup = False
        if to_remove_channels:
            to_remove_topics = self.channel_directory_service.channel_state_topics(
                to_remove_channels
            )
            self.websocket.remove_topics(to_remove_topics)
            for channel in to_remove_channels:
                del channels[channel.id]
                channel.remove()
            del to_remove_channels, to_remove_topics
        if self.wanted_games:
            self.change_state(State.CHANNELS_FETCH)
        else:
            # with no games available, we switch to IDLE after cleanup
            self.print(_("status", "no_campaign"))
            self.change_state(State.IDLE)
        return full_cleanup

    async def _fetch_channels(
        self, channels: OrderedDict[int, Channel]
    ) -> None:
        # Channel objects are replaced below; cancel watch tasks before
        # clearing the channel map so an old task cannot race a relink.
        self.watch_service.stop_watching()
        self.gui.status.update(_("gui", "status", "gathering"))
        # start with all current channels, clear the memory and GUI
        new_channels: set[Channel] = set(channels.values())
        channels.clear()
        self.gui.channels.clear()
        # gather and add ACL channels from campaigns
        # NOTE: we consider only campaigns that can be progressed
        # NOTE: we use another set so that we can set them online separately
        no_acl: set[Game] = set()
        acl_channels: set[Channel] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        for campaign in self.inventory:
            if (
                campaign.game in self.wanted_games
                and campaign.can_earn_within(next_hour)
            ):
                if campaign.allowed_channels:
                    acl_channels.update(campaign.allowed_channels)
                else:
                    no_acl.add(campaign.game)
        # remove all ACL channels that already exist from the other set
        acl_channels.difference_update(new_channels)
        # use the other set to set them online if possible
        await self.channel_directory_service.bulk_check_online(acl_channels)
        # finally, add them as new channels
        new_channels.update(acl_channels)
        new_channels.update(
            await self.channel_directory_service.fetch_live_streams_for_games(no_acl)
        )
        # sort them descending by viewers, by priority and by game priority
        # NOTE: Viewers sort also ensures ONLINE channels are sorted to the top
        # NOTE: We can drop using the set now, because there's no more channels being added
        ordered_channels = self.channel_directory_service.rank_channels(
            new_channels
        )
        # ensure that we won't end up with more channels than we can handle
        # NOTE: we trim from the end because that's where the non-priority,
        # offline (or online but low viewers) channels end up
        to_remove_channels = ordered_channels[MAX_CHANNELS:]
        ordered_channels = ordered_channels[:MAX_CHANNELS]
        if to_remove_channels:
            # tracked channels and gui were cleared earlier, so no need to do it here
            # just make sure to unsubscribe from their topics
            to_remove_topics = self.channel_directory_service.channel_state_topics(
                to_remove_channels
            )
            self.websocket.remove_topics(to_remove_topics)
            del to_remove_channels, to_remove_topics
        # set our new channel list
        for channel in ordered_channels:
            channels[channel.id] = channel
            channel.display(add=True)
        # subscribe to these channel's state updates
        to_add_topics: list[WebsocketTopic] = []
        for channel_id in channels:
            to_add_topics.append(
                WebsocketTopic(
                    "Channel",
                    "StreamState",
                    channel_id,
                    self.channel_event_service.process_stream_state,
                )
            )
            to_add_topics.append(
                WebsocketTopic(
                    "Channel",
                    "StreamUpdate",
                    channel_id,
                    self.channel_event_service.process_stream_update,
                )
            )
        self.websocket.add_topics(to_add_topics)
        # Pre-display the active drop with a subtracted minute.
        for channel in channels.values():
            # check if there's any channels we can watch first
            if self.watch_service.can_watch(channel):
                if (
                    (active_campaign := self.get_active_campaign(channel)) is not None
                    and (active_drop := active_campaign.first_drop) is not None
                ):
                    active_drop.display(countdown=False, subone=True)
                break
        self.change_state(State.CHANNEL_SWITCH)
        del (
            no_acl,
            acl_channels,
            new_channels,
            to_add_topics,
            ordered_channels,
        )

    async def get_auth(self) -> AuthState:
        await self._auth_state.validate()
        return self._auth_state

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        if not self.wanted_games:
            return None
        watching_channel = self.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None
        campaigns: list[DropsCampaign] = []
        for campaign in self.inventory:
            if campaign.can_earn(watching_channel):
                campaigns.append(campaign)
        if campaigns:
            campaigns.sort(key=lambda c: c.remaining_minutes)
            return campaigns[0]
        return None
