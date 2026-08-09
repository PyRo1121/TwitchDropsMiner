from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from constants import ClientType
from http_transport import HttpTransport


class HttpTransportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_creation_is_single_flight_and_transport_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.jar"
            cookie_path.write_bytes(b"invalid cookie state")
            settings = SimpleNamespace(connection_quality=0, proxy=None)
            twitch = cast(
                Any,
                SimpleNamespace(
                    settings=settings,
                    _client_type=ClientType.WEB,
                ),
            )
            transport = HttpTransport(twitch)

            with patch("http_transport.COOKIES_PATH", cookie_path):
                first, second = await asyncio.gather(
                    transport.get_session(),
                    transport.get_session(),
                )
                self.assertIs(first, second)
                self.assertIs(transport.session, first)
                self.assertEqual(settings.connection_quality, 1)
                self.assertEqual(first.timeout.total, 10)

                await transport.close()

            self.assertTrue(first.closed)
            self.assertIsNone(transport.session)
            self.assertTrue(cookie_path.exists())
            self.assertEqual(cookie_path.stat().st_mode & 0o777, 0o600)

    async def test_close_without_a_session_is_idempotent(self) -> None:
        transport = HttpTransport(
            cast(
                Any,
                SimpleNamespace(
                    settings=SimpleNamespace(connection_quality=1, proxy=None),
                    _client_type=ClientType.WEB,
                ),
            )
        )

        await transport.close()
        await transport.close()

        self.assertIsNone(transport.session)


if __name__ == "__main__":
    unittest.main()
