from __future__ import annotations

import asyncio
import logging
from time import monotonic
from functools import partial
from collections import abc, OrderedDict
from collections.abc import Callable, Mapping
from typing import Any, Literal, Final, TYPE_CHECKING

import aiohttp
from translate import _
from auth import AuthState
from channel import Channel
from channel_directory_service import ChannelDirectoryService
from channel_event_service import ChannelEventService
from websocket import TopicDispatchPolicy, WebsocketPool
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
from utils import open_dump
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


_STATE_PRIORITIES: Final[dict[State, int]] = {
    State.EXIT: 0,
    State.RESTART: 1,
    State.GAMES_UPDATE: 2,
    State.INVENTORY_FETCH: 3,
    State.CHANNELS_CLEANUP: 4,
    State.CHANNELS_FETCH: 5,
    State.CHANNEL_SWITCH: 6,
    State.IDLE: 7,
}


class _StateIntentMailbox:
    """Bounded, priority-ordered coordinator intents with pending deduplication."""

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[tuple[int, int, State]] = (
            asyncio.PriorityQueue()
        )
        self._pending: set[State] = set()
        self._sequence = 0
        self._terminal: State | None = None

    @property
    def terminal(self) -> State | None:
        return self._terminal

    @property
    def pending(self) -> frozenset[State]:
        return frozenset(self._pending)

    def put(self, state: State) -> None:
        if self._terminal is State.EXIT:
            return
        if self._terminal is State.RESTART and state is not State.EXIT:
            return
        if state is State.EXIT:
            self._terminal = state
        elif state is State.RESTART:
            self._terminal = state
        if state in self._pending:
            return
        self._sequence += 1
        self._pending.add(state)
        self._queue.put_nowait((_STATE_PRIORITIES[state], self._sequence, state))

    async def get(self) -> State:
        _, _, state = await self._queue.get()
        self._queue.task_done()
        self._pending.remove(state)
        return state

    def reset(self) -> None:
        self._queue = asyncio.PriorityQueue()
        self._pending.clear()
        self._sequence = 0
        self._terminal = None


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
        self._state_intents = _StateIntentMailbox()
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._inventory_generation = 0
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
        self._history_auth_recorded = False
        self.watch_service: WatchService = WatchService(self)
        # Websocket
        self.websocket = WebsocketPool(self)

    async def shutdown(self) -> None:
        start_time = monotonic()
        self._inventory_generation += 1
        async with self.websocket.topic_dispatch_lease(
            TopicDispatchPolicy.DISCARD
        ):
            # Stop socket production, then detached probes, before stopping any
            # watch consumer they could otherwise recreate.
            await self.websocket.stop(clear_topics=True)
            await self.channel_directory_service.quiesce_probes(restart=False)
            await self.inventory_service.close()
            await self.watch_service.close()
            for channel in self.channels.values():
                channel.remove()
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
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(max(0, start_time + 0.5 - monotonic()))

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        self._state_intents.put(state)

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
        self._state_intents.reset()
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
                        if self._state_intents.terminal is State.EXIT:
                            break
                        self._state_intents.reset()
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
        self.inventory_service.start_session()
        self.channel_directory_service.start_session()
        self.watch_service.start_session()
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
            state = await self._state_intents.get()
            if state is State.IDLE:
                if self.watch_service.handle_idle_state():
                    continue
            elif state is State.INVENTORY_FETCH:
                await self.inventory_service.sync_state()
            elif state is State.GAMES_UPDATE:
                await self.inventory_service.update_wanted_games()
                full_cleanup = True
                self.watch_service.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif state is State.CHANNELS_CLEANUP:
                await self.channel_directory_service.cleanup_channels(
                    channels,
                    full_cleanup=full_cleanup,
                )
                full_cleanup = False
            elif state is State.CHANNELS_FETCH:
                await self.channel_directory_service.fetch_channels(channels)
            elif state is State.CHANNEL_SWITCH:
                if self.watch_service.switch_channel(channels):
                    continue
            elif state is State.RESTART:
                raise ReloadRequest()
            elif state is State.EXIT:
                self.gui.tray.change_icon("pickaxe")
                self.gui.status.update(_("gui", "status", "exiting"))
                break

    async def get_auth(self) -> AuthState:
        await self._auth_state.validate()
        return self._auth_state
