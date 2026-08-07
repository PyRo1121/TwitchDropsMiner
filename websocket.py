from __future__ import annotations

import json
import asyncio
import logging
from time import time
from contextlib import suppress
from typing import Any, Literal, TYPE_CHECKING

import aiohttp

from translate import _
from exceptions import MinerException, WebsocketClosed
from constants import PING_INTERVAL, PING_TIMEOUT, MAX_WEBSOCKETS, WS_TOPICS_LIMIT
from utils import (
    CHARS_ASCII,
    chunk,
    task_wrapper,
    create_nonce,
    json_minify,
    format_traceback,
    AwaitableValue,
    ExponentialBackoff,
    cancel_tasks,
    redact_log_value,
)

if TYPE_CHECKING:
    from collections import abc

    from twitch import Twitch
    from constants import JsonType, WebsocketTopic


WSMsgType = aiohttp.WSMsgType
logger = logging.getLogger("TwitchDrops")
ws_logger = logging.getLogger("TwitchDrops.websocket")


class Websocket:
    def __init__(self, pool: WebsocketPool, index: int):
        self._pool: WebsocketPool = pool
        self._twitch: Twitch = pool._twitch
        self._ws_gui: Any = self._twitch.gui.websockets
        self._state_lock = asyncio.Lock()
        # websocket index
        self._idx: int = index
        # current websocket connection
        self._ws: AwaitableValue[aiohttp.ClientWebSocketResponse] = AwaitableValue()
        # set when the websocket needs to be closed or reconnect
        self._closed = asyncio.Event()
        self._reconnect_requested = asyncio.Event()
        # set when the topics changed
        self._topics_changed = asyncio.Event()
        # ping timestamps
        self._next_ping: float = time()
        self._max_pong: float = self._next_ping + PING_TIMEOUT.total_seconds()
        # main task, responsible for receiving messages, sending them, and websocket ping
        self._handle_task: asyncio.Task[None] | None = None
        self._topic_tasks: set[asyncio.Task[Any]] = set()
        # topics stuff
        self.topics: dict[str, WebsocketTopic] = {}
        self._submitted: set[WebsocketTopic] = set()
        # notify GUI
        self.set_status(_("gui", "websocket", "disconnected"))

    @property
    def connected(self) -> bool:
        return self._ws.has_value()

    def wait_until_connected(self):
        return self._ws.wait()

    def set_status(self, status: str | None = None, refresh_topics: bool = False):
        self._twitch.gui.websockets.update(
            self._idx, status=status, topics=(len(self.topics) if refresh_topics else None)
        )

    def request_reconnect(self):
        # reset our ping interval, so we send a PING after reconnect right away
        self._next_ping = time()
        self._reconnect_requested.set()

    async def start(self):
        async with self._state_lock:
            self.start_nowait()
            await self._twitch.gui.coro_unless_closed(self.wait_until_connected())

    def start_nowait(self):
        if self._handle_task is None or self._handle_task.done():
            self._closed.clear()
            self._handle_task = asyncio.create_task(self._handle())

    async def stop(self, *, remove: bool = False):
        async with self._state_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            ws = self._ws.get_with_default(None)
            if ws is not None:
                self.set_status(_("gui", "websocket", "disconnecting"))
                await ws.close()
            if self._handle_task is not None:
                handle_task = self._handle_task
                try:
                    await asyncio.wait_for(handle_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception as exc:
                    ws_logger.debug(
                        "Websocket[%s] handler ended during stop: %s",
                        self._idx,
                        type(exc).__name__,
                    )
                finally:
                    await cancel_tasks([handle_task])
                    self._handle_task = None
            await cancel_tasks(self._topic_tasks)
            self._topic_tasks.clear()
            if remove:
                self.topics.clear()
                self._topics_changed.set()
                self._twitch.gui.websockets.remove(self._idx)

    def stop_nowait(self, *, remove: bool = False):
        # weird syntax but that's what we get for using a decorator for this
        # return type of 'task_wrapper' is a coro, so we need to instance it for the task
        asyncio.create_task(task_wrapper(self.stop)(remove=remove))

    async def _backoff_connect(
        self, ws_url: str, **kwargs
    ) -> abc.AsyncGenerator[aiohttp.ClientWebSocketResponse, None]:
        session = await self._twitch.get_session()
        backoff = ExponentialBackoff(**kwargs)
        if self._twitch.settings.proxy:
            proxy = self._twitch.settings.proxy
        else:
            proxy = None
        for delay in backoff:
            try:
                async with session.ws_connect(ws_url, proxy=proxy) as websocket:
                    yield websocket
                    backoff.reset()
            except (
                asyncio.TimeoutError,
                aiohttp.ClientResponseError,
                aiohttp.ClientConnectionError,
            ):
                ws_logger.info(
                    f"Websocket[{self._idx}] connection problem (sleep: {round(delay)}s)"
                )
                try:
                    await asyncio.wait_for(self._closed.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                else:
                    break
            except RuntimeError:
                ws_logger.warning(
                    f"Websocket[{self._idx}] exiting backoff connect loop "
                    "because session is closed (RuntimeError)"
                )
                break

    @task_wrapper(critical=True)
    async def _handle(self):
        # ensure we're logged in before connecting
        self.set_status(_("gui", "websocket", "initializing"))
        await self._twitch.gui.coro_unless_closed(self._twitch.wait_until_login())
        if self._closed.is_set():
            return
        self.set_status(_("gui", "websocket", "connecting"))
        ws_logger.info(f"Websocket[{self._idx}] connecting...")
        # Connect/Reconnect loop
        async for websocket in self._backoff_connect(
            "wss://pubsub-edge.twitch.tv/v1", maximum=3*60  # 3 minutes maximum backoff time
        ):
            self._ws.set(websocket)
            self._reconnect_requested.clear()
            # NOTE: _topics_changed doesn't start set,
            # because there's no initial topics we can sub to right away
            self.set_status(_("gui", "websocket", "connected"))
            ws_logger.info(f"Websocket[{self._idx}] connected.")
            try:
                try:
                    while not self._reconnect_requested.is_set():
                        await self._handle_ping()
                        await self._handle_topics()
                        await self._handle_recv()
                finally:
                    self._ws.clear()
                    self._submitted.clear()
                    # set _topics_changed to let the next WS connection resub to the topics
                    self._topics_changed.set()
                # A reconnect was requested
            except WebsocketClosed as exc:
                if exc.received:
                    # server closed the connection, not us - reconnect
                    ws_logger.warning(
                        f"Websocket[{self._idx}] closed unexpectedly: {websocket.close_code}"
                    )
                elif self._closed.is_set():
                    # we closed it - exit
                    ws_logger.info(f"Websocket[{self._idx}] stopped.")
                    self.set_status(_("gui", "websocket", "disconnected"))
                    return
            except Exception:
                ws_logger.exception(f"Exception in Websocket[{self._idx}]")
            self.set_status(_("gui", "websocket", "reconnecting"))
            ws_logger.warning(f"Websocket[{self._idx}] reconnecting...")

    async def _handle_ping(self):
        now = time()
        if now >= self._next_ping:
            self._next_ping = now + PING_INTERVAL.total_seconds()
            self._max_pong = now + PING_TIMEOUT.total_seconds()  # wait for a PONG for up to 10s
            await self.send({"type": "PING"})
        elif now >= self._max_pong:
            # it's been more than 10s and there was no PONG
            ws_logger.warning(f"Websocket[{self._idx}] didn't receive a PONG, reconnecting...")
            self.request_reconnect()

    async def _handle_topics(self):
        if not self._topics_changed.is_set():
            # nothing to do
            return
        self._topics_changed.clear()
        self.set_status(refresh_topics=True)
        auth_state = await self._twitch.get_auth()
        current: set[WebsocketTopic] = set(self.topics.values())
        # handle removed topics
        removed = self._submitted.difference(current)
        if removed:
            topics_list = list(map(str, removed))
            ws_logger.debug(f"Websocket[{self._idx}]: Removing topics: {', '.join(topics_list)}")
            for topics in chunk(topics_list, 20):
                await self.send(
                    {
                        "type": "UNLISTEN",
                        "data": {
                            "topics": topics,
                            "auth_token": auth_state.access_token,
                        }
                    }
                )
            self._submitted.difference_update(removed)
        # handle added topics
        added = current.difference(self._submitted)
        if added:
            topics_list = list(map(str, added))
            ws_logger.debug(f"Websocket[{self._idx}]: Adding topics: {', '.join(topics_list)}")
            for topics in chunk(topics_list, 20):
                await self.send(
                    {
                        "type": "LISTEN",
                        "data": {
                            "topics": topics,
                            "auth_token": auth_state.access_token,
                        }
                    }
                )
            self._submitted.update(added)

    async def _gather_recv(self, messages: list[JsonType], timeout: float = 0.5):
        """
        Gather incoming messages over the timeout specified.
        Note that there's no return value - this modifies `messages` in-place.
        """
        ws = self._ws.get_with_default(None)
        if ws is None:
            raise WebsocketClosed(received=False)
        while True:
            try:
                raw_message: aiohttp.WSMessage = await ws.receive(timeout=timeout)
            except aiohttp.ClientConnectionError as exc:
                raise WebsocketClosed(received=False) from exc
            ws_logger.debug(
                "Websocket[%s] received message type=%s",
                self._idx,
                raw_message.type,
            )
            if raw_message.type is WSMsgType.TEXT:
                try:
                    message = json.loads(raw_message.data)
                except ValueError as exc:
                    ws_logger.warning(
                        f"Websocket[{self._idx}] ignored invalid JSON message: {exc}"
                    )
                    continue
                if isinstance(message, dict):
                    messages.append(message)
                else:
                    ws_logger.warning(
                        f"Websocket[{self._idx}] ignored non-object JSON message"
                    )
            elif raw_message.type is WSMsgType.CLOSE:
                raise WebsocketClosed(received=True)
            elif raw_message.type is WSMsgType.CLOSED:
                raise WebsocketClosed(received=False)
            elif raw_message.type is WSMsgType.CLOSING:
                pass  # skip these
            elif raw_message.type is WSMsgType.ERROR:
                ws_logger.error(
                    f"Websocket[{self._idx}] error: {format_traceback(raw_message.data)}"
                )
                raise WebsocketClosed()
            else:
                ws_logger.error(
                    "Websocket[%s] error: Unknown message type=%s",
                    self._idx,
                    raw_message.type,
                )

    def _handle_message(self, message: JsonType) -> None:
        # request the assigned topic to process the response
        try:
            data = message["data"]
            topic = self.topics.get(data["topic"])
            payload = json.loads(data["message"])
        except (KeyError, TypeError, ValueError) as exc:
            ws_logger.warning(
                f"Websocket[{self._idx}] ignored malformed message: {exc}"
            )
            return
        if topic is not None:
            if not isinstance(payload, dict):
                ws_logger.warning(
                    f"Websocket[{self._idx}] ignored non-object topic payload"
                )
                return
            # use a task to not block the websocket
            task = asyncio.create_task(topic(payload))
            self._topic_tasks.add(task)
            task.add_done_callback(self._topic_task_done)

    def _topic_task_done(self, task: asyncio.Task[Any]) -> None:
        self._topic_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            ws_logger.error(
                "Websocket[%s] topic handler failed: %s",
                self._idx,
                type(exception).__name__,
            )

    async def _handle_recv(self):
        """
        Handle receiving messages from the websocket.
        """
        # listen over 0.5s for incoming messages
        messages: list[JsonType] = []
        with suppress(asyncio.TimeoutError):
            await self._gather_recv(messages, timeout=0.5)
        # process them
        for message in messages:
            msg_type = message.get("type")
            if not isinstance(msg_type, str):
                ws_logger.warning(
                    "Websocket[%s] ignored message without a valid type",
                    self._idx,
                )
                continue
            if msg_type == "MESSAGE":
                self._handle_message(message)
            elif msg_type == "PONG":
                # move the timestamp to something much later
                self._max_pong = self._next_ping
            elif msg_type == "RESPONSE":
                error = message.get("error")
                if error:
                    ws_logger.warning(
                        "Websocket[%s] PubSub request rejected: %s",
                        self._idx,
                        redact_log_value(error),
                    )
                    # LISTEN/UNLISTEN failures leave our local submitted set
                    # out of sync; force a clean resubscription pass.
                    self._submitted.clear()
                    self._topics_changed.set()
                    if error == "ERR_BADAUTH":
                        self._twitch._auth_state.invalidate()
                        self.request_reconnect()
            elif msg_type == "RECONNECT":
                # We've received a reconnect request
                ws_logger.warning(f"Websocket[{self._idx}] requested reconnect.")
                self.request_reconnect()
            else:
                ws_logger.warning(
                    "Websocket[%s] received unknown payload: %s",
                    self._idx,
                    redact_log_value(message),
                )

    def add_topics(self, topics_set: set[WebsocketTopic]):
        changed: bool = False
        while topics_set and len(self.topics) < WS_TOPICS_LIMIT:
            topic = topics_set.pop()
            self.topics[str(topic)] = topic
            changed = True
        if changed:
            self._topics_changed.set()

    def remove_topics(self, topics_set: set[str]):
        existing = topics_set.intersection(self.topics.keys())
        if not existing:
            # nothing to remove from here
            return
        topics_set.difference_update(existing)
        for topic in existing:
            del self.topics[topic]
        self._topics_changed.set()

    async def send(self, message: JsonType):
        ws = self._ws.get_with_default(None)
        if ws is None:
            raise WebsocketClosed(received=False)
        if message["type"] != "PING":
            message["nonce"] = create_nonce(CHARS_ASCII, 30)
        try:
            await ws.send_json(message, dumps=json_minify)
        except aiohttp.ClientConnectionError as exc:
            raise WebsocketClosed(received=False) from exc
        ws_logger.debug(
            "Websocket[%s] sent: %s",
            self._idx,
            redact_log_value(message),
        )


class WebsocketPool:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._running = asyncio.Event()
        self.websockets: list[Websocket] = []

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def wait_until_connected(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._running.wait()

    async def start(self):
        self._running.set()
        await asyncio.gather(*(ws.start() for ws in self.websockets))

    async def stop(self, *, clear_topics: bool = False):
        self._running.clear()
        await asyncio.gather(*(ws.stop(remove=clear_topics) for ws in self.websockets))

    def add_topics(self, topics: abc.Iterable[WebsocketTopic]):
        # ensure no topics end up duplicated
        topics_set = set(topics)
        if not topics_set:
            # nothing to add
            return
        topics_set.difference_update(*(ws.topics.values() for ws in self.websockets))
        if not topics_set:
            # none left to add
            return
        for ws_idx in range(MAX_WEBSOCKETS):
            if ws_idx < len(self.websockets):
                # just read it back
                ws = self.websockets[ws_idx]
            else:
                # create new
                ws = Websocket(self, ws_idx)
                if self.running:
                    ws.start_nowait()
                self.websockets.append(ws)
            # ask websocket to take any topics it can - this modifies the set in-place
            ws.add_topics(topics_set)
            # see if there's any leftover topics for the next websocket connection
            if not topics_set:
                return
        # if we're here, there were leftover topics after filling up all websockets
        raise MinerException("Maximum topics limit has been reached")

    def remove_topics(self, topics: abc.Iterable[str]):
        topics_set = set(topics)
        if not topics_set:
            # nothing to remove
            return
        for ws in self.websockets:
            ws.remove_topics(topics_set)
        # count up all the topics - if we happen to have more websockets connected than needed,
        # stop the last one and recycle topics from it - repeat until we have enough
        recycled_topics: list[WebsocketTopic] = []
        while True:
            count = sum(len(ws.topics) for ws in self.websockets)
            if count <= (len(self.websockets) - 1) * WS_TOPICS_LIMIT:
                ws = self.websockets.pop()
                recycled_topics.extend(ws.topics.values())
                ws.stop_nowait(remove=True)
            else:
                break
        if recycled_topics:
            self.add_topics(recycled_topics)
