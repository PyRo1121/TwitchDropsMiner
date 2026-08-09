from __future__ import annotations

import asyncio
import json
import unittest
from collections import deque
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import aiohttp

from channel import Channel
from constants import WebsocketTopic
from exceptions import ExitRequest, MinerException, WebsocketClosed
from translate import _
from websocket import Websocket, WebsocketPool


class _ChannelRows:
    def __init__(self) -> None:
        self.display_calls = 0

    def display(self, channel: Channel, *, add: bool = False) -> None:
        del channel, add
        self.display_calls += 1

    def remove(self, channel: Channel) -> None:
        del channel


class _WebsocketRows:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def update(self, index: int, **values: Any) -> None:
        del index
        status = values.get("status")
        if isinstance(status, str):
            self.statuses.append(status)

    def remove(self, index: int) -> None:
        del index


class ChannelOnlineProbeTests(unittest.TestCase):
    def test_failed_probe_retries_and_clears_pending_presentation(self) -> None:
        async def exercise() -> None:
            rows = _ChannelRows()
            twitch = SimpleNamespace(gui=SimpleNamespace(channels=rows))
            channel = Channel(cast(Any, twitch), id=1, login="channel")
            update_stream = AsyncMock(
                side_effect=(MinerException("temporary"), True)
            )

            with patch.object(
                Channel,
                "update_stream",
                update_stream,
            ), patch("channel.ONLINE_DELAY", timedelta(0)), patch(
                "channel.ONLINE_RETRY_DELAYS",
                (0.0,),
            ):
                channel.check_online()
                task = channel._pending_stream_up
                self.assertIsNotNone(task)
                assert task is not None
                await task

            self.assertEqual(update_stream.await_count, 2)
            self.assertIsNone(channel._pending_stream_up)
            self.assertEqual(rows.display_calls, 2)

        asyncio.run(exercise())

    def test_shutdown_request_from_detached_probe_is_consumed(self) -> None:
        async def exercise() -> None:
            rows = _ChannelRows()
            twitch = SimpleNamespace(gui=SimpleNamespace(channels=rows))
            channel = Channel(cast(Any, twitch), id=1, login="channel")

            with patch.object(
                Channel,
                "update_stream",
                AsyncMock(side_effect=ExitRequest()),
            ), patch("channel.ONLINE_DELAY", timedelta(0)), patch(
                "channel.ONLINE_RETRY_DELAYS",
                (),
            ):
                channel.check_online()
                task = channel._pending_stream_up
                self.assertIsNotNone(task)
                assert task is not None
                await task

            self.assertIsNone(channel._pending_stream_up)
            self.assertEqual(rows.display_calls, 2)

        asyncio.run(exercise())

    def test_invalid_spade_page_encoding_is_retryable(self) -> None:
        class Response:
            async def __aenter__(self) -> Response:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def text(self, *, encoding: str) -> str:
                del encoding
                raise UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid")

        async def exercise() -> None:
            twitch = SimpleNamespace(
                gui=SimpleNamespace(channels=_ChannelRows()),
                transport=SimpleNamespace(request=lambda *_args, **_kwargs: Response()),
                _client_type=SimpleNamespace(CLIENT_URL="https://www.twitch.tv"),
            )
            channel = Channel(cast(Any, twitch), id=1, login="channel")
            with self.assertRaises(MinerException):
                await channel.get_spade_url()

        asyncio.run(exercise())

    def test_cancelled_probe_cannot_clear_its_replacement(self) -> None:
        async def exercise() -> None:
            twitch = SimpleNamespace(gui=SimpleNamespace(channels=_ChannelRows()))
            channel = Channel(cast(Any, twitch), id=1, login="channel")

            channel.check_online()
            first = channel._pending_stream_up
            self.assertIsNotNone(first)
            await asyncio.sleep(0)

            channel.set_offline()
            channel.check_online()
            replacement = channel._pending_stream_up
            self.assertIsNotNone(replacement)
            self.assertIsNot(first, replacement)

            await asyncio.sleep(0)
            self.assertIs(channel._pending_stream_up, replacement)

            channel.remove()
            if replacement is not None:
                await asyncio.gather(replacement, return_exceptions=True)
                self.assertTrue(replacement.cancelled())

        asyncio.run(exercise())


class WebsocketLifecycleTests(unittest.TestCase):
    def test_unstable_connections_apply_exponential_protocol_backoff(self) -> None:
        class Connection:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *_args: object) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.calls = 0

            def ws_connect(self, _url: str, *, proxy: object) -> Connection:
                del proxy
                self.calls += 1
                return Connection()

        async def exercise() -> None:
            session = Session()

            async def get_session() -> Session:
                return session

            websocket = Websocket.__new__(Websocket)
            websocket._idx = 0
            websocket._closed = asyncio.Event()
            websocket._twitch = cast(
                Any,
                SimpleNamespace(
                    transport=SimpleNamespace(get_session=get_session),
                    settings=SimpleNamespace(proxy=None),
                ),
            )
            wait = AsyncMock(return_value=False)
            websocket._wait_for_backoff = wait  # type: ignore[method-assign]

            with patch("websocket.monotonic", return_value=0.0):
                connections = websocket._backoff_connect(
                    "wss://example.test",
                    variance=0,
                    maximum=30,
                )
                await anext(connections)
                await anext(connections)
                await anext(connections)
                await connections.aclose()

            self.assertEqual(session.calls, 3)
            self.assertEqual(
                [call.args[0] for call in wait.await_args_list],
                [1.0, 2.0],
            )

        asyncio.run(exercise())

    def test_local_close_is_not_misclassified_as_server_reconnect(self) -> None:
        async def exercise() -> None:
            rows = _WebsocketRows()

            async def wait_until_login() -> None:
                return None

            async def coro_unless_closed(awaitable: Any) -> Any:
                return await awaitable

            twitch = SimpleNamespace(
                gui=SimpleNamespace(
                    coro_unless_closed=coro_unless_closed,
                    websockets=rows,
                ),
                wait_until_login=wait_until_login,
            )
            websocket = Websocket(
                cast(Any, SimpleNamespace(_twitch=twitch)),
                0,
            )
            socket = SimpleNamespace(close_code=1000)

            async def connections(*_args: object, **_kwargs: object):
                yield socket

            async def close_during_ping() -> None:
                websocket._closed.set()
                raise WebsocketClosed(received=True)

            websocket._backoff_connect = connections  # type: ignore[method-assign]
            websocket._handle_ping = close_during_ping  # type: ignore[method-assign]

            await websocket._handle()

            self.assertEqual(
                rows.statuses[-1],
                _("gui", "websocket", "disconnected"),
            )
            self.assertNotIn(
                _("gui", "websocket", "reconnecting"),
                rows.statuses,
            )

        asyncio.run(exercise())

    def test_receive_batch_is_bounded_under_continuous_traffic(self) -> None:
        class BurstWebsocket:
            def __init__(self) -> None:
                self.calls = 0

            async def receive(self, timeout: float) -> aiohttp.WSMessage:
                del timeout
                self.calls += 1
                return aiohttp.WSMessage(
                    aiohttp.WSMsgType.TEXT,
                    json.dumps({"type": "PONG"}),
                    "",
                )

        async def exercise() -> None:
            burst = BurstWebsocket()
            websocket = Websocket.__new__(Websocket)
            websocket._idx = 0
            websocket._ws = cast(
                Any,
                SimpleNamespace(get_with_default=lambda _default: burst),
            )
            messages: list[dict[str, Any]] = []

            with patch("websocket.WS_RECV_BATCH_LIMIT", 3):
                await websocket._gather_recv(messages, timeout=1)

            self.assertEqual(len(messages), 3)
            self.assertEqual(burst.calls, 3)

        asyncio.run(exercise())

    def test_receive_window_uses_one_absolute_deadline(self) -> None:
        class RecordingWebsocket:
            def __init__(self) -> None:
                self.timeouts: list[float] = []

            async def receive(self, timeout: float) -> aiohttp.WSMessage:
                self.timeouts.append(timeout)
                return aiohttp.WSMessage(
                    aiohttp.WSMsgType.TEXT,
                    json.dumps({"type": "PONG"}),
                    "",
                )

        async def exercise() -> None:
            recording = RecordingWebsocket()
            websocket = Websocket.__new__(Websocket)
            websocket._idx = 0
            websocket._ws = cast(
                Any,
                SimpleNamespace(get_with_default=lambda _default: recording),
            )
            messages: list[dict[str, Any]] = []

            with patch(
                "websocket.monotonic",
                side_effect=(0.0, 0.1, 0.3, 0.6),
            ):
                with self.assertRaises(asyncio.TimeoutError):
                    await websocket._gather_recv(messages, timeout=0.5)

            self.assertEqual(len(messages), 2)
            self.assertAlmostEqual(recording.timeouts[0], 0.4)
            self.assertAlmostEqual(recording.timeouts[1], 0.2)

        asyncio.run(exercise())

    def test_topic_dispatch_queues_without_blocking_control_plane(self) -> None:
        async def exercise() -> None:
            release = asyncio.Event()
            active = 0
            max_active = 0
            calls = 0

            async def process(_target_id: int, _payload: dict[str, Any]) -> None:
                nonlocal active, calls, max_active
                active += 1
                calls += 1
                max_active = max(max_active, active)
                try:
                    await release.wait()
                finally:
                    active -= 1

            topic = WebsocketTopic("User", "Drops", 1, process)
            message = {
                "data": {
                    "topic": str(topic),
                    "message": json.dumps({"type": "drop-progress"}),
                }
            }
            websocket = Websocket.__new__(Websocket)
            websocket._idx = 0
            websocket.topics = {str(topic): topic}
            websocket._topic_tasks = set()
            websocket._pending_topic_messages = deque()
            websocket._topic_generation = 0

            with patch("websocket.WS_TOPIC_TASK_LIMIT", 1):
                await websocket._handle_message(message)
                await asyncio.sleep(0)
                await asyncio.wait_for(websocket._handle_message(message), timeout=0.1)
                self.assertEqual(len(websocket._pending_topic_messages), 1)
                self.assertEqual(calls, 1)

                release.set()
                for _ in range(10):
                    if calls == 2:
                        break
                    await asyncio.sleep(0)
                await websocket.cancel_topic_tasks()

            self.assertEqual(calls, 2)
            self.assertEqual(max_active, 1)
            self.assertEqual(websocket._topic_tasks, set())

        asyncio.run(exercise())

    def test_start_propagates_handler_failure_before_connection(self) -> None:
        async def exercise() -> None:
            async def coro_unless_closed(awaitable: Any) -> Any:
                return await awaitable

            gui = SimpleNamespace(
                websockets=_WebsocketRows(),
                coro_unless_closed=coro_unless_closed,
            )
            websocket = Websocket(
                cast(Any, SimpleNamespace(_twitch=SimpleNamespace(gui=gui))),
                0,
            )

            async def fail_before_connect() -> None:
                raise RuntimeError("injected handler failure")

            websocket._handle = fail_before_connect  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "injected handler failure"):
                await asyncio.wait_for(websocket.start(), timeout=0.25)
            await websocket.stop()
            self.assertIsNone(websocket._handle_task)

        asyncio.run(exercise())

    def test_stop_cleans_up_after_socket_close_failure(self) -> None:
        async def exercise() -> None:
            rows = _WebsocketRows()
            gui = SimpleNamespace(websockets=rows)
            websocket = Websocket(
                cast(Any, SimpleNamespace(_twitch=SimpleNamespace(gui=gui))),
                0,
            )
            socket = SimpleNamespace(close=AsyncMock(side_effect=OSError("close failed")))
            websocket._ws.set(cast(Any, socket))

            async def handle_until_closed() -> None:
                await websocket._closed.wait()

            websocket._handle_task = asyncio.create_task(handle_until_closed())
            websocket.topics["topic"] = cast(Any, object())

            await websocket.stop(remove=True)

            self.assertIsNone(websocket._handle_task)
            self.assertEqual(websocket.topics, {})
            self.assertTrue(websocket._closed.is_set())

        asyncio.run(exercise())

    def test_pool_stop_awaits_retirement_tasks(self) -> None:
        async def exercise() -> None:
            pool = WebsocketPool(cast(Any, SimpleNamespace()))
            completed = False

            async def retire() -> None:
                nonlocal completed
                await asyncio.sleep(0)
                completed = True

            pool._retirement_tasks.add(asyncio.create_task(retire()))
            await pool.stop()

            self.assertTrue(completed)
            self.assertEqual(pool._retirement_tasks, set())

        asyncio.run(exercise())

    def test_stop_can_interrupt_start_waiting_for_connection(self) -> None:
        async def exercise() -> None:
            async def coro_unless_closed(awaitable: Any) -> Any:
                return await awaitable

            gui = SimpleNamespace(
                websockets=_WebsocketRows(),
                coro_unless_closed=coro_unless_closed,
            )
            twitch = SimpleNamespace(gui=gui)
            pool = SimpleNamespace(_twitch=twitch)
            websocket = Websocket(cast(Any, pool), 0)

            async def wait_until_stopped() -> None:
                await websocket._closed.wait()

            websocket._handle = wait_until_stopped  # type: ignore[method-assign]
            start_task = asyncio.create_task(websocket.start())
            await asyncio.sleep(0)

            await asyncio.wait_for(websocket.stop(), timeout=0.25)
            result = await asyncio.gather(start_task, return_exceptions=True)

            self.assertIsInstance(result[0], WebsocketClosed)
            self.assertIsNone(websocket._handle_task)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
