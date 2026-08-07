from __future__ import annotations

import asyncio
import unittest
from typing import Any, cast

from yarl import URL

from utils import redact_log_value
from websocket import Websocket


class LogRedactionTests(unittest.TestCase):
    def test_redacts_nested_credentials_without_mutating_input(self) -> None:
        value = {
            "headers": {
                "Authorization": "OAuth access-secret",
                "Client-Id": "public-client-id",
            },
            "json": {
                "username": "twitch-user",
                "password": "password-secret",
                "authy_token": "two-factor-secret",
            },
            "proxy": URL("http://proxy-user:proxy-secret@example.test:8080"),
            "url": URL("https://example.test/live?token=stream-secret&sig=signature-secret"),
        }

        redacted = redact_log_value(value)

        self.assertEqual(value["json"]["password"], "password-secret")
        rendered = repr(redacted)
        for secret in (
            "access-secret",
            "password-secret",
            "two-factor-secret",
            "proxy-secret",
            "stream-secret",
            "signature-secret",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(redacted["headers"]["Client-Id"], "public-client-id")
        self.assertEqual(redacted["json"]["password"], "<redacted>")
        self.assertEqual(redacted["json"]["authy_token"], "<redacted>")

    def test_redacts_sensitive_data_strings(self) -> None:
        redacted = redact_log_value({"data": "device-code-secret", "json": b"raw-secret"})

        self.assertEqual(redacted, {"data": "<redacted>", "json": "<redacted>"})

    def test_ignores_websocket_messages_without_a_type(self) -> None:
        socket = Websocket.__new__(Websocket)
        socket._idx = 0

        async def gather_messages(messages: list[dict[str, Any]], timeout: float = 0.5) -> None:
            messages.append({"data": {"unexpected": True}})

        cast(Any, socket)._gather_recv = gather_messages
        asyncio.run(socket._handle_recv())


if __name__ == "__main__":
    unittest.main()
