from __future__ import annotations

import asyncio
import logging
from math import floor
from time import monotonic
from functools import partial
from collections import abc, deque, OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import suppress
from typing import Any, Literal, Final, cast, TYPE_CHECKING

import aiohttp
from translate import _
from auth import AuthState
from channel import Channel
from websocket import WebsocketPool
from watch_service import WatchService
from inventory import DropsCampaign
from inventory_service import InventoryService
from session_history import HistoryEvent, Scalar, SessionHistory, Severity
from http_transport import HttpTransport
from exceptions import (
    ExitRequest,
    GQLException,
    ReloadRequest,
    LoginException,
    MinerException,
    RequestInvalid,
    RequestException,
)
from utils import (
    chunk,
    timestamp,
    cancel_tasks,
    task_wrapper,
    AwaitableValue,
    redact_log_value,
    atomic_write_path,
    extract_available_drops,
    require_int,
)
from constants import (
    CALL,
    MAX_INT,
    DUMP_PATH,
    COOKIES_PATH,
    MAX_CHANNELS,
    GQL_BATCH_SIZE,
    GQL_QUERIES,
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
    from channel import Stream
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo, JsonType, GQLOperation


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
        # Client type, session, transport, and auth
        self._client_type: ClientInfo = ClientType.ANDROID_APP
        self._session: aiohttp.ClientSession | None = None
        self.transport = HttpTransport(self)
        self._auth_state = AuthState(self)
        self.inventory_service = InventoryService(self)
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

    async def get_session(self) -> aiohttp.ClientSession:
        if (session := self._session) is not None:
            if session.closed:
                raise RuntimeError("Session is closed")
            return session
        # load in cookies
        cookie_jar = aiohttp.CookieJar()
        try:
            if COOKIES_PATH.exists():
                with suppress(OSError):
                    COOKIES_PATH.chmod(0o600)
                cookie_jar.load(COOKIES_PATH)
        except Exception:
            # if loading in the cookies file ends up in an error, just ignore it
            # clear the jar, just in case
            cookie_jar.clear()
        # create timeouts
        # Connection quality multiplier determines the magnitude of timeouts.
        connection_quality = self.settings.connection_quality
        if connection_quality < 1:
            connection_quality = self.settings.connection_quality = 1
        elif connection_quality > 6:
            connection_quality = self.settings.connection_quality = 6
        timeout = aiohttp.ClientTimeout(
            sock_connect=5*connection_quality,
            total=10*connection_quality,
        )
        # create session, limited to 50 connections at maximum
        connector = aiohttp.TCPConnector(limit=50)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            cookie_jar=cookie_jar,
            headers={"User-Agent": self._client_type.USER_AGENT},
        )
        return self._session

    @staticmethod
    def _save_cookie_jar(cookie_jar: aiohttp.CookieJar, path: Path) -> None:
        """Persist cookies atomically without destroying the last good file."""
        try:
            atomic_write_path(path, cookie_jar.save)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Unable to persist cookies: %s", type(exc).__name__)

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
        # stop websocket, close session and save cookies
        await self.websocket.stop(clear_topics=True)
        if self._session is not None:
            cookie_jar = cast(aiohttp.CookieJar, self._session.cookie_jar)
            # clear empty cookie entries off the cookies file before saving
            # NOTE: Unfortunately, aiohttp provides no easy way of clearing empty cookies,
            # so we need to access the private '_cookies' attribute for this.
            for cookie_key, cookie in list(cookie_jar._cookies.items()):
                if not cookie:
                    del cookie_jar._cookies[cookie_key]
            self._save_cookie_jar(cookie_jar, COOKIES_PATH)
            await self._session.close()
            self._session = None
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

    def get_priority(self, channel: Channel) -> int:
        """
        Return a priority number for a given channel.

        0 has the highest priority.
        Higher numbers -> lower priority.
        MAX_INT (a really big number) signifies the lowest possible priority.
        """
        if (
            (game := channel.game) is None  # None when OFFLINE or no game set
            or game not in self.wanted_games  # we don't care about the played game
        ):
            return MAX_INT
        return self.wanted_games.index(game)

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        if (viewers := channel.viewers) is not None:
            return viewers
        return -1

    @staticmethod
    def _channel_state_topics(channels: abc.Iterable[Channel]) -> list[str]:
        topics: list[str] = []
        for channel in channels:
            topics.append(WebsocketTopic.as_str("Channel", "StreamState", channel.id))
            topics.append(WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id))
        return topics

    def _rank_channels(self, channels: abc.Iterable[Channel]) -> list[Channel]:
        ordered = sorted(channels, key=self._viewers_key, reverse=True)
        ordered.sort(key=lambda channel: channel.acl_based, reverse=True)
        ordered.sort(key=self.get_priority)
        return ordered

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
                except ReloadRequest:
                    self.history.record("session.reload", data={"reason": "maintenance"})
                    await self.shutdown()
                except ExitRequest:
                    break
                except aiohttp.ContentTypeError as exc:
                    session_status = "failed"
                    failure_reason = "unexpected_content"
                    raise RequestException(_("login", "unexpected_content")) from exc
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
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic(
                "User", "Notifications", auth_state.user_id, self.process_notifications
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
            for channel in sorted(channels.values(), key=self.get_priority):
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
            to_remove_topics = self._channel_state_topics(to_remove_channels)
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

    async def _fetch_live_streams_for_games(
        self,
        games: abc.Iterable[Game],
    ) -> set[Channel]:
        channels: set[Channel] = set()
        for game in games:
            try:
                streams = await self.get_live_streams(game, drops_enabled=True)
            except MinerException:
                logger.warning(
                    "Unable to fetch live channels for %s; continuing",
                    game.name,
                )
                continue
            channels.update(streams)
        return channels

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
        await self.bulk_check_online(acl_channels)
        # finally, add them as new channels
        new_channels.update(acl_channels)
        new_channels.update(await self._fetch_live_streams_for_games(no_acl))
        # sort them descending by viewers, by priority and by game priority
        # NOTE: Viewers sort also ensures ONLINE channels are sorted to the top
        # NOTE: We can drop using the set now, because there's no more channels being added
        ordered_channels = self._rank_channels(new_channels)
        # ensure that we won't end up with more channels than we can handle
        # NOTE: we trim from the end because that's where the non-priority,
        # offline (or online but low viewers) channels end up
        to_remove_channels = ordered_channels[MAX_CHANNELS:]
        ordered_channels = ordered_channels[:MAX_CHANNELS]
        if to_remove_channels:
            # tracked channels and gui were cleared earlier, so no need to do it here
            # just make sure to unsubscribe from their topics
            to_remove_topics = self._channel_state_topics(to_remove_channels)
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
                    "Channel", "StreamState", channel_id, self.process_stream_state
                )
            )
            to_add_topics.append(
                WebsocketTopic(
                    "Channel", "StreamUpdate", channel_id, self.process_stream_update
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

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType):
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                try:
                    viewers = require_int(
                        message["viewers"],
                        "Invalid viewer count",
                    )
                except (KeyError, ValueError):
                    logger.warning("Ignoring invalid viewer count for %s", channel.name)
                    return
                if viewers < 0:
                    logger.warning("Ignoring invalid viewer count for %s", channel.name)
                    return
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type != "commercial":
            logger.warning(f"Unknown stream state: {msg_type}")

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType):
        # message = {
        #     "channel_id": "12345678",
        #     "type": "broadcast_settings_update",
        #     "channel": "channel._login",
        #     "old_status": "Old title",
        #     "status": "New title",
        #     "old_game": "Old game name",
        #     "game": "New game name",
        #     "old_game_id": 123456,
        #     "game_id": 123456
        # }
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return
        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ''
        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")
        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'check_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online()

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ):
        """
        Called by a Channel when it's status is updated (ONLINE, OFFLINE, title/tags change).

        NOTE: 'stream_before' gets dealocated once this function finishes.
        """
        if stream_before is None:
            if stream_after is not None:
                # Channel going ONLINE
                if self.watch_service.should_switch(channel):
                    # we can watch the channel, and we should
                    self.print(_("status", "goes_online").format(channel=channel.name))
                    self.watch_service.watch(channel)
                else:
                    logger.info(f"{channel.name} goes ONLINE")
            else:
                # Channel was OFFLINE and stays that way
                logger.log(CALL, f"{channel.name} stays OFFLINE")
        else:
            is_watching = channel.id in self._watching_channels
            if is_watching:
                if not self.watch_service.can_watch(channel):
                    if stream_after is None:
                        self.print(_("status", "goes_offline").format(channel=channel.name))
                    else:
                        logger.info(
                            f"{channel.name} status has been updated, switching... "
                            f"(🎁: {self._drops_marker(stream_before)} -> "
                            f"{self._drops_marker(stream_after)})"
                        )
                    self.change_state(State.CHANNEL_SWITCH)
            elif stream_after is None:
                logger.info(f"{channel.name} goes OFFLINE")
            else:
                logger.info(
                    f"{channel.name} status has been updated "
                    f"(🎁: {self._drops_marker(stream_before)} -> "
                    f"{self._drops_marker(stream_after)})"
                )
                if self.watch_service.should_switch(channel):
                    self.watch_service.watch(channel)
        channel.display()

    @staticmethod
    def _drops_marker(stream: Stream | None) -> str:
        return "✔" if stream is not None and stream.drops_enabled else "❌"

    def _inventory_drop_is_current(
        self,
        generation: int,
        drop: TimedDrop,
    ) -> bool:
        return (
            generation == self._inventory_generation
            and self._drops.get(drop.id) is drop
        )

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType):
        # Message examples:
        # {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
        # {"type": "drop-claim", data: {"drop_instance_id": ...}}
        inventory_generation = self._inventory_generation
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        data = message.get("data")
        if not isinstance(data, dict):
            logger.warning("Ignoring a drop event without an object data payload")
            return
        drop_id = data.get("drop_id")
        if not isinstance(drop_id, str):
            logger.warning("Ignoring a drop event without a valid drop ID")
            return
        drop: TimedDrop | None = self._drops.get(drop_id)
        watching_channels = [
            channel
            for channel in self._watching_channels.values()
            if self._watch_drop_ids.get(channel.id) == drop_id
        ]
        if not watching_channels:
            if drop_id in getattr(self, "_watch_completed_drop_ids", set()):
                logger.log(CALL, "Ignoring an event for a previously completed drop: %s", drop_id)
                return
            candidates = (
                [
                    channel
                    for channel in self._watching_channels.values()
                    if drop.can_earn(channel)
                ]
                if drop is not None
                else []
            )
            if drop is not None and len(candidates) == 1:
                channel = candidates[0]
                previous_drop_id = self._watch_drop_ids.get(channel.id)
                self._watch_drop_ids[channel.id] = drop_id
                restart_event = self._watch_restart_events.get(channel.id)
                if restart_event is not None:
                    restart_event.set()
                watching_channels = [channel]
                logger.info(
                    "Adopted unassigned drop event for %s: %s -> %s",
                    channel.name,
                    previous_drop_id,
                    drop_id,
                )
            else:
                if self.watch_service._request_watch_resync(f"unassigned-drop:{drop_id}"):
                    logger.warning("Ignoring an event for an unassigned drop: %s", drop_id)
                return
        if drop is None:
            logger.error("Received an event for an unknown drop: %s", drop_id)
            self.change_state(State.INVENTORY_FETCH)
            return
        if msg_type == "drop-claim":
            claim_id = data.get("drop_instance_id")
            if not isinstance(claim_id, str):
                logger.warning("Ignoring a drop claim without a valid instance ID")
                return
            drop.update_claim(claim_id)
            campaign = drop.campaign
            claimed = await drop.claim()
            if not self._inventory_drop_is_current(inventory_generation, drop):
                logger.info("Ignoring a claim result from a replaced inventory")
                return
            if claimed:
                self.watch_service._mark_watch_completed_drop(drop.id)
            self.watch_service._display_primary_drop(drop)

            async def wait_for_next_drop(channel: Channel) -> None:
                # About 4-20s after claiming, Twitch starts the next drop after
                # another watch payload. Check each assigned channel independently.
                for _attempt in range(8):
                    try:
                        context = await self.transport.gql_request(
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

            await asyncio.sleep(4)
            if not self._inventory_drop_is_current(inventory_generation, drop):
                return
            await asyncio.gather(
                *(wait_for_next_drop(channel) for channel in watching_channels)
            )
            if not self._inventory_drop_is_current(inventory_generation, drop):
                return
            if claimed and any(self.watch_service.can_watch(channel) for channel in self._watching_channels.values()):
                primary = self.watching_channel.get_with_default(None)
                if primary is not None:
                    self.watch_service.watch(primary, update_status=False)
                    self.watch_service.restart_watching()
                    return
            elif not claimed and any(campaign.can_earn(channel) for channel in watching_channels):
                self.watch_service.restart_watching()
                return
            self.change_state(State.INVENTORY_FETCH)
            return
        assert msg_type == "drop-progress"
        current_progress = data.get("current_progress_min")
        required_progress = data.get("required_progress_min")
        if (
            type(current_progress) is not int
            or type(required_progress) is not int
            or current_progress < 0
            or required_progress < 0
            or current_progress > required_progress
        ):
            logger.warning("Ignoring a drop event with invalid progress: %s", drop_id)
            return
        current_progress_int = current_progress
        required_progress_int = required_progress
        logger.log(
            CALL,
            "Drop update from websocket: %s (%s/%s)",
            drop.name,
            current_progress_int,
            required_progress_int,
        )
        # PubSub does not include a channel ID; the assigned drop ID is the
        # authoritative discriminator when two channels are being farmed.
        drop.update_minutes(current_progress_int, required_progress_int)
        self.watch_service._display_primary_drop(drop)

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType):
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] in (
                "user_drop_reward_reminder_notification",  # drop confirmation
                "quests_viewer_reward_campaign_earned_emote",  # emote confirmation
                # badge confirmation?
            ):
                self.change_state(State.INVENTORY_FETCH)
                try:
                    await self.transport.gql_request(
                        GQL_QUERIES["NotificationsDelete"].with_variables(
                            {"input": {"id": data["id"]}}
                        )
                    )
                except (GQLException, RequestException):
                    # Notifications can disappear or the delete request can fail
                    # after the inventory refresh; the next event can retry it.
                    logger.debug("Unable to delete Twitch notification")

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

    async def get_live_streams(
        self, game: Game, *, limit: int = 20, drops_enabled: bool = True
    ) -> list[Channel]:
        filters: list[str] = []
        if drops_enabled:
            filters.append("DROPS_ENABLED")
        try:
            response = await self.transport.gql_request(
                GQL_QUERIES["GameDirectory"].with_variables({
                    "limit": limit,
                    "slug": game.slug,
                    "options": {
                        "includeRestricted": ["SUB_ONLY_LIVE"],
                        "systemFilters": filters,
                    },
                })
            )
        except GQLException as exc:
            raise MinerException(f"Game: {game.slug}") from exc
        data = response.get("data") if isinstance(response, dict) else None
        game_data = data.get("game") if isinstance(data, dict) else None
        streams = game_data.get("streams") if isinstance(game_data, dict) else None
        edges = streams.get("edges") if isinstance(streams, dict) else []
        if not isinstance(edges, list):
            return []
        channels: list[Channel] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict) or node.get("broadcaster") is None:
                continue
            try:
                channels.append(Channel.from_directory(self, node, drops_enabled=drops_enabled))
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed directory stream response")
        return channels

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        """
        Utilize batch GQL requests to check ONLINE status for a lot of channels at once.
        The optional available-drops check is applied to channels when enabled;
        otherwise directory filtering and campaign ACLs remain the source of truth.
        """
        channels = tuple(channels)
        acl_streams_map: dict[int, JsonType] = {}
        stream_gql_ops: list[GQLOperation] = [channel.stream_gql for channel in channels]
        if not stream_gql_ops:
            # shortcut for nothing to process
            # NOTE: Have to do this here, becase "channels" can be any iterable
            return
        stream_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
            asyncio.create_task(self.transport.gql_request(stream_gql_chunk))
            for stream_gql_chunk in chunk(stream_gql_ops, GQL_BATCH_SIZE)
        ]
        try:
            for coro in asyncio.as_completed(stream_gql_tasks):
                response_list: list[JsonType] = await coro
                for response_json in response_list:
                    try:
                        channel_data = response_json["data"]["user"]
                        channel_id = (
                            require_int(
                                channel_data["id"],
                                "Invalid channel ID",
                            )
                            if channel_data is not None
                            else None
                        )
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Ignoring malformed stream lookup response")
                        continue
                    if isinstance(channel_data, dict) and channel_id is not None:
                        acl_streams_map[channel_id] = channel_data
        finally:
            await cancel_tasks(stream_gql_tasks)
        # for all channels with an active stream, check the available drops as well
        acl_available_drops_map: dict[int, list[JsonType]] = {}
        if self.settings.available_drops_check:
            available_gql_ops: list[GQLOperation] = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
                for channel_id, channel_data in acl_streams_map.items()
                if isinstance(channel_data.get("stream"), dict)  # only ONLINE channels
            ]
            available_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
                asyncio.create_task(self.transport.gql_request(available_gql_chunk))
                for available_gql_chunk in chunk(available_gql_ops, GQL_BATCH_SIZE)
            ]
            try:
                for coro in asyncio.as_completed(available_gql_tasks):
                    response_list = await coro
                    for response_json in response_list:
                        try:
                            available_info = response_json["data"]["channel"]
                            channel_id = require_int(
                                available_info["id"],
                                "Invalid channel ID",
                            )
                        except (KeyError, TypeError, ValueError):
                            logger.warning("Ignoring malformed available-drops response")
                            continue
                        acl_available_drops_map[channel_id] = extract_available_drops(
                            response_json
                        )
            finally:
                await cancel_tasks(available_gql_tasks)
        for channel in channels:
            channel_id = channel.id
            if channel_id not in acl_streams_map:
                continue
            channel_data = acl_streams_map[channel_id]
            if not isinstance(channel_data.get("stream"), dict):
                continue
            available_drops: list[JsonType] = acl_available_drops_map.get(channel_id, [])
            try:
                channel.external_update(channel_data, available_drops)
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed stream data for channel %s", channel_id)
