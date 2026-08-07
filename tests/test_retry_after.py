from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from time import time
from typing import Any, cast
from unittest.mock import patch

from twitch import _retry_after_delay


def _response(headers: dict[str, str]) -> Any:
    return cast(Any, type("Response", (), {"headers": headers})())


class RetryAfterDelayTests(unittest.TestCase):
    def test_numeric_retry_after_wins(self) -> None:
        response = _response({"Retry-After": "12"})

        self.assertEqual(_retry_after_delay(response, 5.0), 12.0)

    def test_http_date_retry_after(self) -> None:
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        response = _response({"Retry-After": format_datetime(retry_at)})

        self.assertGreater(_retry_after_delay(response, 5.0), 20.0)

    def test_ratelimit_reset_used_when_retry_after_is_unparsable(self) -> None:
        with patch("twitch.time", return_value=1000.0):
            response = _response({"Retry-After": "not-a-date", "Ratelimit-Reset": "1030"})

            self.assertEqual(_retry_after_delay(response, 5.0), 30.0)

    def test_fallback_used_without_headers(self) -> None:
        response = _response({})

        self.assertEqual(_retry_after_delay(response, 7.5), 7.5)

    def test_delay_never_below_one_second(self) -> None:
        with patch("twitch.time", return_value=time()):
            response = _response({"Ratelimit-Reset": "0"})

            self.assertEqual(_retry_after_delay(response, 0.0), 1.0)


if __name__ == "__main__":
    unittest.main()
