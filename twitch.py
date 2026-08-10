from __future__ import annotations

import asyncio
import logging
from time import monotonic
from functools import partial
from collections import abc, deque, OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime
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
    open_dump,
    AwaitableValue,
    redact_log_value,
)
from constants import (
    State,
    ClientType,
    WebsocketTopic,
)

if TYPE_CHECKING:
    from game import Game
    from gui_port import GuiPort
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo


logger = logging.getLogger("TwitchDrops")


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
        pending_channel_tasks = [
            channel._pending_stream_up
            for channel in self.channels.values()
            if channel._pending_stream_up is not None
        ]
        for channel in self.channels.values():
            channel.remove()
        await cancel_tasks((*background_tasks, *pending_channel_tasks))
        await self.inventory_service.close()
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
        self.inventory_service.start_session()
        refresh_history = getattr(self.gui, "history_changed", None)
        if refresh_history is not None:
            refresh_history()
        session_status: Literal["stopped", "failed"] = "stopped"
        failure_reason: str | None = None
        try:
            if self.settings.dump:
                with open_dump("w"):
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
                await self.inventory_service.sync_state()
            elif self._state is State.GAMES_UPDATE:
                await self.inventory_service.update_wanted_games()
                full_cleanup = True
                self.watch_service.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                await self.channel_directory_service.cleanup_channels(
                    channels,
                    full_cleanup=full_cleanup,
                )
                full_cleanup = False
            elif self._state is State.CHANNELS_FETCH:
                await self.channel_directory_service.fetch_channels(channels)
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

    async def get_auth(self) -> AuthState:
        await self._auth_state.validate()
        return self._auth_state
