from __future__ import annotations

import json
import asyncio
import logging
from collections import deque
from time import monotonic
from contextlib import suppress
from enum import Enum
from types import TracebackType
from typing import Any, TYPE_CHECKING

import aiohttp

from translate import _
from exceptions import MinerException, WebsocketClosed
from constants import (
    PING_INTERVAL,
    PING_TIMEOUT,
    MAX_WEBSOCKETS,
    WS_TOPICS_LIMIT,
    WS_TOPIC_BATCH_SIZE,
    WS_TOPIC_TASK_LIMIT,
    WS_TOPIC_PENDING_LIMIT,
    WS_STOP_TIMEOUT,
    WS_BACKOFF_MAX,
    WS_STABLE_SECONDS,
    WS_RECV_WINDOW,
    WS_RECV_BATCH_LIMIT,
)
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
ws_logger = logging.getLogger("TwitchDrops.websocket")


class TopicDispatchPolicy(Enum):
    REPLAY = "replay"
    DISCARD = "discard"


class Websocket:
    def __init__(self, pool: WebsocketPool, index: int):
        self._twitch: Twitch = pool._twitch
        self._state_lock = asyncio.Lock()
        # websocket index
        self._idx: int = index
        # current websocket connection
        self._ws: AwaitableValue[
            aiohttp.ClientWebSocketResponse[bool]
        ] = AwaitableValue()
        # set when the websocket needs to be closed or reconnect
        self._closed = asyncio.Event()
        self._reconnect_requested = asyncio.Event()
        # set when the topics changed
        self._topics_changed = asyncio.Event()
        # ping timestamps
        self._next_ping: float = monotonic()
        self._max_pong: float = self._next_ping + PING_TIMEOUT.total_seconds()
        # main task, responsible for receiving messages, sending them, and websocket ping
        self._handle_task: asyncio.Task[None] | None = None
        self._topic_tasks: set[asyncio.Task[Any]] = set()
        self._replay_tasks: set[asyncio.Task[Any]] = set()
        self._pending_topic_messages: deque[
            tuple[WebsocketTopic, JsonType, int]
        ] = deque()
        self._replay_topic_messages: deque[
            tuple[WebsocketTopic, JsonType, int]
        ] = deque()
        self._topic_generation = 0
        self._topic_dispatch_paused = pool.topic_dispatch_paused
        self._topic_dispatch_policy = pool.topic_dispatch_policy
        self._topic_replay_overflow = False
        # topics stuff
        self.topics: dict[str, WebsocketTopic] = {}
        self._submitted: set[WebsocketTopic] = set()
        # notify GUI
        self.set_status(_("gui", "websocket", "disconnected"))

    def wait_until_connected(self):
        return self._ws.wait()

    def set_status(self, status: str | None = None, refresh_topics: bool = False):
        self._twitch.gui.websockets.update(
            self._idx, status=status, topics=(len(self.topics) if refresh_topics else None)
        )

    def request_reconnect(self):
        # reset our ping interval, so we send a PING after reconnect right away
        self._next_ping = monotonic()
        self._reconnect_requested.set()

    async def start(self):
        async with self._state_lock:
            self.start_nowait()
        await self._twitch.gui.coro_unless_closed(
            self._wait_until_connected_or_stopped()
        )

    async def _wait_until_connected_or_stopped(self) -> None:
        handle_task = self._handle_task
        if handle_task is None:
            raise WebsocketClosed("Websocket handler is not running", received=False)

        async def wait_for_handler() -> None:
            await asyncio.shield(handle_task)

        connected_task = asyncio.create_task(self.wait_until_connected())
        stopped_task = asyncio.create_task(self._closed.wait())
        handler_done_task = asyncio.create_task(wait_for_handler())
        try:
            done, _ = await asyncio.wait(
                (connected_task, stopped_task, handler_done_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if handler_done_task in done:
                await handle_task
                raise WebsocketClosed(
                    "Websocket handler stopped before connecting",
                    received=False,
                )
            if self._closed.is_set():
                raise WebsocketClosed(received=False)
            await connected_task
        finally:
            await cancel_tasks((connected_task, stopped_task, handler_done_task))

    def start_nowait(self):
        if self._handle_task is None or self._handle_task.done():
            self._closed.clear()
            self._handle_task = asyncio.create_task(self._handle())

    async def stop(self, *, remove: bool = False) -> None:
        async with self._state_lock:
            self._closed.set()
            try:
                ws = self._ws.get_with_default(None)
                if ws is not None:
                    self.set_status(_("gui", "websocket", "disconnecting"))
                    try:
                        await ws.close()
                    except Exception as exc:
                        ws_logger.debug(
                            "Websocket[%s] close failed during stop: %s",
                            self._idx,
                            type(exc).__name__,
                        )
            finally:
                handle_task = self._handle_task
                if handle_task is not None:
                    if handle_task is not asyncio.current_task():
                        done, pending = await asyncio.wait(
                            (handle_task,),
                            timeout=WS_STOP_TIMEOUT,
                        )
                        if pending:
                            ws_logger.debug(
                                "Websocket[%s] handler did not stop within timeout",
                                self._idx,
                            )
                        if done and not handle_task.cancelled():
                            try:
                                await handle_task
                            except Exception as exc:
                                ws_logger.debug(
                                    "Websocket[%s] handler ended during stop: %s",
                                    self._idx,
                                    type(exc).__name__,
                                )
                        await cancel_tasks((handle_task,))
                    self._handle_task = None
                await self.cancel_topic_tasks()
                if remove:
                    self.topics.clear()
                    self._topics_changed.set()
                    self._twitch.gui.websockets.remove(self._idx)

    def _invalidate_topic_work(self) -> tuple[asyncio.Task[Any], ...]:
        self._topic_generation += 1
        replay_policy = (
            self._topic_dispatch_paused
            and self._topic_dispatch_policy is TopicDispatchPolicy.REPLAY
        )
        if (
            self._pending_topic_messages
            or self._replay_topic_messages
            or self._replay_tasks
            or (replay_policy and self._topic_tasks)
        ):
            self._topic_replay_overflow = True
        self._pending_topic_messages.clear()
        self._replay_topic_messages.clear()
        tasks = tuple(self._topic_tasks | self._replay_tasks)
        for task in tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        return tasks

    async def cancel_topic_tasks(self) -> None:
        tasks = self._invalidate_topic_work()
        current_task = asyncio.current_task()
        owned = tuple(task for task in tasks if task is not current_task)
        cancelled = False
        if owned:
            barrier = asyncio.gather(*owned, return_exceptions=True)
            while not barrier.done():
                try:
                    await asyncio.shield(barrier)
                except (asyncio.CancelledError,):
                    cancelled = True
            barrier.result()
        self._topic_tasks.difference_update(owned)
        self._replay_tasks.difference_update(owned)
        if cancelled:
            raise asyncio.CancelledError()

    async def pause_topic_dispatch(
        self,
        policy: TopicDispatchPolicy,
    ) -> None:
        """Pause dispatch and quiesce handlers under the requested policy."""
        self._topic_dispatch_paused = True
        self._topic_dispatch_policy = policy
        if policy is TopicDispatchPolicy.DISCARD:
            await self.cancel_topic_tasks()
            return
        tasks = tuple(self._topic_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def apply_topic_dispatch_policy(
        self,
        policy: TopicDispatchPolicy,
    ) -> None:
        if not self._topic_dispatch_paused:
            raise RuntimeError("Topic dispatch is not paused")
        self._topic_dispatch_policy = policy
        if policy is TopicDispatchPolicy.DISCARD:
            await self.cancel_topic_tasks()

    async def resume_topic_dispatch(self) -> None:
        """Replay a bounded pause queue, then allow live topic dispatch."""
        policy = self._topic_dispatch_policy
        if policy is TopicDispatchPolicy.REPLAY:
            self._replay_topic_messages.extend(self._pending_topic_messages)
            self._pending_topic_messages.clear()
            while self._replay_topic_messages:
                batch = tuple(
                    self._replay_topic_messages.popleft()
                    for _ in range(
                        min(
                            WS_TOPIC_TASK_LIMIT,
                            len(self._replay_topic_messages),
                        )
                    )
                )
                tasks = tuple(
                    asyncio.create_task(
                        self._dispatch_replayed_topic(topic, payload, generation)
                    )
                    for topic, payload, generation in batch
                )
                self._replay_tasks.update(tasks)
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    self._replay_tasks.difference_update(tasks)
        else:
            if self._pending_topic_messages or self._replay_topic_messages:
                self._topic_replay_overflow = True
            self._pending_topic_messages.clear()
            self._replay_topic_messages.clear()
        self._topic_dispatch_paused = False
        self._topic_dispatch_policy = None
        self._drain_pending_topic_messages()

    async def _dispatch_replayed_topic(
        self,
        topic: WebsocketTopic,
        payload: JsonType,
        generation: int,
    ) -> None:
        if (
            generation != self._topic_generation
            or self.topics.get(str(topic)) is not topic
        ):
            self._topic_replay_overflow = True
            return
        try:
            await topic(payload)
        except (asyncio.CancelledError,):
            self._topic_replay_overflow = True
            raise
        except Exception as exc:
            self._topic_replay_overflow = True
            ws_logger.error(
                "Websocket[%s] replayed topic handler failed: %s",
                self._idx,
                type(exc).__name__,
            )
        finally:
            if generation != self._topic_generation:
                self._topic_replay_overflow = True

    def consume_topic_replay_overflow(self) -> bool:
        overflow = self._topic_replay_overflow
        self._topic_replay_overflow = False
        return overflow

    def stop_nowait(self, *, remove: bool = False) -> asyncio.Task[None]:
        # Make retirement visible to the receive loop before yielding to the
        # owned asynchronous cleanup task.
        self._closed.set()
        self._reconnect_requested.set()
        self._invalidate_topic_work()
        return asyncio.create_task(task_wrapper(self.stop)(remove=remove))

    async def _wait_for_backoff(self, delay: float) -> bool:
        closed_task = asyncio.create_task(self._closed.wait())
        try:
            done, _ = await asyncio.wait((closed_task,), timeout=delay)
            return bool(done)
        finally:
            await cancel_tasks((closed_task,))

    async def _backoff_connect(
        self, ws_url: str, **kwargs
    ) -> abc.AsyncGenerator[aiohttp.ClientWebSocketResponse[bool], None]:
        session = await self._twitch.transport.get_session()
        backoff = ExponentialBackoff(**kwargs)
        if self._twitch.settings.proxy:
            proxy = self._twitch.settings.proxy
        else:
            proxy = None
        for delay in backoff:
            try:
                connected_at = monotonic()
                async with session.ws_connect(ws_url, proxy=proxy) as websocket:
                    yield websocket
                uptime = monotonic() - connected_at
                if uptime >= WS_STABLE_SECONDS:
                    backoff.reset()
                else:
                    ws_logger.warning(
                        "Websocket[%s] unstable connection (sleep: %ss)",
                        self._idx,
                        round(delay),
                    )
                    if await self._wait_for_backoff(delay):
                        break
            except RuntimeError:
                ws_logger.warning(
                    f"Websocket[{self._idx}] exiting backoff connect loop "
                    "because session is closed (RuntimeError)"
                )
                break
            except (
                asyncio.TimeoutError,
                aiohttp.ClientResponseError,
                aiohttp.ClientConnectionError,
            ):
                ws_logger.info(
                    f"Websocket[{self._idx}] connection problem (sleep: {round(delay)}s)"
                )
                if await self._wait_for_backoff(delay):
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
            "wss://pubsub-edge.twitch.tv/v1", maximum=WS_BACKOFF_MAX
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
                    await self.cancel_topic_tasks()
                    # set _topics_changed to let the next WS connection resub to the topics
                    self._topics_changed.set()
                # A reconnect was requested
            except Exception as exc:
                if isinstance(exc, WebsocketClosed):
                    if self._closed.is_set():
                        ws_logger.info(f"Websocket[{self._idx}] stopped.")
                        self.set_status(_("gui", "websocket", "disconnected"))
                        return
                    if exc.received:
                        # server closed the connection, not us - reconnect
                        ws_logger.warning(
                            f"Websocket[{self._idx}] closed unexpectedly: "
                            f"{websocket.close_code}"
                        )
                else:
                    ws_logger.exception(f"Exception in Websocket[{self._idx}]")
            self.set_status(_("gui", "websocket", "reconnecting"))
            ws_logger.warning(f"Websocket[{self._idx}] reconnecting...")

    async def _handle_ping(self):
        now = monotonic()
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
            for topics in chunk(topics_list, WS_TOPIC_BATCH_SIZE):
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
            for topics in chunk(topics_list, WS_TOPIC_BATCH_SIZE):
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

    async def _gather_recv(
        self, messages: list[JsonType], timeout: float = WS_RECV_WINDOW
    ):
        """
        Gather incoming messages over the timeout specified.
        Note that there's no return value - this modifies `messages` in-place.
        """
        ws = self._ws.get_with_default(None)
        if ws is None:
            raise WebsocketClosed(received=False)
        deadline = monotonic() + max(timeout, 0)
        while len(messages) < WS_RECV_BATCH_LIMIT:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                raw_message = await ws.receive(timeout=remaining)
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

    async def _handle_message(self, message: JsonType) -> None:
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
        if topic is None:
            return
        if not isinstance(payload, dict):
            ws_logger.warning(
                f"Websocket[{self._idx}] ignored non-object topic payload"
            )
            return

        generation = self._topic_generation
        if self._topic_dispatch_paused:
            if self._topic_dispatch_policy is TopicDispatchPolicy.DISCARD:
                return
            self._queue_topic_message(topic, payload, generation)
            return
        if len(self._topic_tasks) >= WS_TOPIC_TASK_LIMIT:
            self._queue_topic_message(topic, payload, generation)
            return
        self._start_topic_task(topic, payload, generation)

    def _queue_topic_message(
        self,
        topic: WebsocketTopic,
        payload: JsonType,
        generation: int,
    ) -> None:
        if len(self._pending_topic_messages) >= WS_TOPIC_PENDING_LIMIT:
            self._pending_topic_messages.popleft()
            if self._topic_dispatch_paused:
                self._topic_replay_overflow = True
            ws_logger.warning(
                "Websocket[%s] topic queue saturated; dropped oldest event",
                self._idx,
            )
        self._pending_topic_messages.append((topic, payload, generation))

    def _start_topic_task(
        self,
        topic: WebsocketTopic,
        payload: JsonType,
        generation: int,
    ) -> None:
        if self._topic_dispatch_paused or generation != self._topic_generation:
            return
        task = asyncio.create_task(self._dispatch_topic(topic, payload, generation))
        self._topic_tasks.add(task)
        task.add_done_callback(self._topic_task_done)

    def _drain_pending_topic_messages(self) -> None:
        if self._topic_dispatch_paused:
            return
        while (
            self._pending_topic_messages
            and len(self._topic_tasks) < WS_TOPIC_TASK_LIMIT
        ):
            topic, payload, generation = self._pending_topic_messages.popleft()
            if generation == self._topic_generation and self.topics.get(str(topic)) is topic:
                self._start_topic_task(topic, payload, generation)

    async def _dispatch_topic(
        self,
        topic: WebsocketTopic,
        payload: JsonType,
        generation: int,
    ) -> None:
        if self._topic_dispatch_paused or generation != self._topic_generation:
            return
        await topic(payload)

    def _topic_task_done(self, task: asyncio.Task[Any]) -> None:
        self._topic_tasks.discard(task)
        if task.cancelled():
            self._drain_pending_topic_messages()
            return
        exception = task.exception()
        if exception is not None:
            ws_logger.error(
                "Websocket[%s] topic handler failed: %s",
                self._idx,
                type(exception).__name__,
            )
        self._drain_pending_topic_messages()

    async def _handle_recv(self):
        """
        Handle receiving messages from the websocket.
        """
        # listen over 0.5s for incoming messages
        messages: list[JsonType] = []
        with suppress(asyncio.TimeoutError):
            await self._gather_recv(messages)
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
                await self._handle_message(message)
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


class _TopicDispatchLease:
    def __init__(
        self,
        pool: WebsocketPool,
        policy: TopicDispatchPolicy,
    ) -> None:
        self._pool = pool
        self._policy = policy
        self._acquired = False

    async def __aenter__(self) -> _TopicDispatchLease:
        await self._pool._acquire_topic_lease(self, self._policy)
        self._acquired = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._acquired:
            return
        try:
            await self._pool._release_topic_lease(self)
        finally:
            self._acquired = False


class WebsocketPool:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._running = asyncio.Event()
        self.websockets: list[Websocket] = []
        self._retirement_tasks: set[asyncio.Task[None]] = set()
        self._topic_leases: dict[
            _TopicDispatchLease, TopicDispatchPolicy
        ] = {}
        self._topic_pause_lock = asyncio.Lock()
        self._topic_policy: TopicDispatchPolicy | None = None

    @property
    def running(self) -> bool:
        return self._running.is_set()

    @property
    def topic_dispatch_paused(self) -> bool:
        return bool(self._topic_leases)

    @property
    def topic_dispatch_policy(self) -> TopicDispatchPolicy | None:
        return self._topic_policy

    async def start(self):
        self._running.set()
        await asyncio.gather(*(ws.start() for ws in self.websockets))

    async def stop(self, *, clear_topics: bool = False) -> None:
        self._running.clear()
        tasks: tuple[asyncio.Task[Any] | asyncio.Future[Any], ...] = (
            *tuple(self._retirement_tasks),
            *(asyncio.create_task(ws.stop(remove=clear_topics)) for ws in self.websockets),
        )
        self._retirement_tasks.clear()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                ws_logger.error(
                    "Websocket cleanup failed: %s",
                    type(result).__name__,
                )

    def topic_dispatch_lease(
        self,
        policy: TopicDispatchPolicy,
    ) -> _TopicDispatchLease:
        return _TopicDispatchLease(self, policy)

    def _effective_topic_policy(self) -> TopicDispatchPolicy | None:
        if not self._topic_leases:
            return None
        if TopicDispatchPolicy.DISCARD in self._topic_leases.values():
            return TopicDispatchPolicy.DISCARD
        return TopicDispatchPolicy.REPLAY

    @staticmethod
    async def _complete_topic_barrier(
        *awaitables: abc.Awaitable[Any],
    ) -> bool:
        if not awaitables:
            return False
        barrier = asyncio.gather(*awaitables, return_exceptions=True)
        cancelled = False
        while not barrier.done():
            try:
                await asyncio.shield(barrier)
            except (asyncio.CancelledError,):
                cancelled = True
        results = barrier.result()
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return cancelled

    async def _transition_topic_policy(
        self,
        previous: TopicDispatchPolicy | None,
        current: TopicDispatchPolicy | None,
    ) -> bool:
        if previous is current:
            return False
        if previous is None:
            if current is None:
                return False
            return await self._complete_topic_barrier(
                *(ws.pause_topic_dispatch(current) for ws in self.websockets)
            )
        if current is None:
            return await self._complete_topic_barrier(
                *(ws.resume_topic_dispatch() for ws in self.websockets)
            )
        return await self._complete_topic_barrier(
            *(ws.apply_topic_dispatch_policy(current) for ws in self.websockets)
        )

    async def _acquire_topic_lease(
        self,
        lease: _TopicDispatchLease,
        policy: TopicDispatchPolicy,
    ) -> None:
        async with self._topic_pause_lock:
            if lease in self._topic_leases:
                raise RuntimeError("Topic dispatch lease is already acquired")
            previous = self._topic_policy
            self._topic_leases[lease] = policy
            current = self._effective_topic_policy()
            self._topic_policy = current
            cancelled = False
            try:
                cancelled = await self._transition_topic_policy(previous, current)
            except BaseException:
                del self._topic_leases[lease]
                rollback = self._effective_topic_policy()
                self._topic_policy = rollback
                await self._transition_topic_policy(current, rollback)
                raise
            if cancelled:
                del self._topic_leases[lease]
                rollback = self._effective_topic_policy()
                self._topic_policy = rollback
                await self._transition_topic_policy(current, rollback)
                raise asyncio.CancelledError()

    async def _release_topic_lease(
        self,
        lease: _TopicDispatchLease,
    ) -> None:
        async with self._topic_pause_lock:
            policy = self._topic_leases.get(lease)
            if policy is None:
                raise RuntimeError("Topic dispatch lease is not acquired")
            previous = self._topic_policy
            del self._topic_leases[lease]
            current = self._effective_topic_policy()
            self._topic_policy = current
            cancelled = False
            try:
                cancelled = await self._transition_topic_policy(previous, current)
            except BaseException:
                self._topic_leases[lease] = policy
                rollback = self._effective_topic_policy()
                self._topic_policy = rollback
                await self._transition_topic_policy(current, rollback)
                raise
            if cancelled:
                raise asyncio.CancelledError()

    def consume_topic_replay_overflow(self) -> bool:
        overflow = False
        for websocket in self.websockets:
            overflow = websocket.consume_topic_replay_overflow() or overflow
        return overflow

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
                retirement_task = ws.stop_nowait(remove=True)
                self._retirement_tasks.add(retirement_task)
                retirement_task.add_done_callback(self._retirement_tasks.discard)
            else:
                break
        if recycled_topics:
            self.add_topics(recycled_topics)
