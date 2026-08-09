from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from time import monotonic, time
from typing import Any, cast
from unittest.mock import patch

from constants import HTTP_RETRY_MAX_DELAY
from exceptions import ExitRequest, RequestInvalid
from http_transport import (
    HttpTransport,
    read_json,
    retry_after_delay,
)
from twitch import Twitch


def _response(headers: dict[str, str]) -> Any:
    return cast(Any, type("Response", (), {"headers": headers})())


class RetryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_decoder_rejects_trailing_garbage(self) -> None:
        class Response:
            async def json(self, *, loads):
                return loads('{"ok": true} trailing-data')

        with self.assertRaisesRegex(ValueError, "Invalid response"):
            await read_json(
                cast(Any, Response()),
                ValueError,
                "Invalid response",
            )

    async def test_close_interrupts_a_retry_delay(self) -> None:
        miner = Twitch.__new__(Twitch)
        closed = asyncio.Event()
        closed.set()
        miner.gui = cast(
            Any,
            type(
                "Gui",
                (),
                {
                    "close_requested": True,
                    "wait_until_closed": closed.wait,
                },
            )(),
        )

        with self.assertRaises(ExitRequest):
            await HttpTransport(miner).wait_for_delay(60)

    async def test_deadline_caps_a_retry_delay(self) -> None:
        miner = Twitch.__new__(Twitch)
        closed = asyncio.Event()
        miner.gui = cast(
            Any,
            type(
                "Gui",
                (),
                {
                    "close_requested": False,
                    "wait_until_closed": closed.wait,
                },
            )(),
        )
        started = monotonic()

        with self.assertRaises(RequestInvalid):
            await HttpTransport(miner).wait_for_delay(
                60,
                deadline=datetime.now(timezone.utc) + timedelta(milliseconds=5),
            )

        self.assertLess(monotonic() - started, 0.2)


class RetryAfterDelayTests(unittest.TestCase):
    def test_numeric_retry_after_wins(self) -> None:
        response = _response({"Retry-After": "12"})

        self.assertEqual(retry_after_delay(response, 5.0), 12.0)

    def test_http_date_retry_after(self) -> None:
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        response = _response({"Retry-After": format_datetime(retry_at)})

        self.assertGreater(retry_after_delay(response, 5.0), 20.0)

    def test_ratelimit_reset_used_when_retry_after_is_unparsable(self) -> None:
        with patch("http_transport.time", return_value=1000.0):
            response = _response({"Retry-After": "not-a-date", "Ratelimit-Reset": "1030"})

            self.assertEqual(retry_after_delay(response, 5.0), 30.0)

    def test_untrusted_retry_hints_are_bounded(self) -> None:
        self.assertEqual(
            retry_after_delay(_response({"Retry-After": "999999999"}), 5.0),
            HTTP_RETRY_MAX_DELAY,
        )
        self.assertEqual(
            retry_after_delay(_response({"Retry-After": "inf"}), 5.0),
            5.0,
        )

    def test_fallback_used_without_headers(self) -> None:
        response = _response({})

        self.assertEqual(retry_after_delay(response, 7.5), 7.5)

    def test_delay_never_below_one_second(self) -> None:
        with patch("http_transport.time", return_value=time()):
            response = _response({"Ratelimit-Reset": "0"})

            self.assertEqual(retry_after_delay(response, 0.0), 1.0)


if __name__ == "__main__":
    unittest.main()
