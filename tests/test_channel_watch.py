from __future__ import annotations

import asyncio
import base64
import binascii
import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from channel import Channel, Stream
from constants import URLType


class StreamWatchPayloadTests(unittest.TestCase):
    def test_send_watch_snapshots_authenticated_identity(self) -> None:
        class Response:
            status = 204

            async def __aenter__(self) -> Response:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

        class Transport:
            def __init__(self) -> None:
                self.data: object = None

            def request(self, *_args: object, **kwargs: object) -> Response:
                self.data = kwargs["data"]
                return Response()

        async def exercise() -> tuple[bool, object]:
            transport = Transport()

            async def get_auth() -> object:
                return SimpleNamespace(user_id=42)

            twitch = SimpleNamespace(
                settings=SimpleNamespace(available_drops_check=False),
                gui=SimpleNamespace(
                    channels=SimpleNamespace(
                        display=lambda *_args, **_kwargs: None,
                    )
                ),
                transport=transport,
                get_auth=get_auth,
                _auth_state=SimpleNamespace(user_id=None),
            )
            channel = Channel(cast(Any, twitch), id=7, login="streamer")
            channel._stream = Stream(
                channel,
                id=123,
                game={"id": "99", "name": "Game", "displayName": "Game"},
                viewers=100,
                title="Live",
            )
            channel._spade_url = URLType("https://spade.twitch.tv/track")
            return await channel.send_watch(), transport.data

        sent, payload = asyncio.run(exercise())
        self.assertTrue(sent)
        try:
            encoded = cast(dict[str, str], payload)["data"]
            decoded = json.loads(base64.b64decode(encoded))
        except (binascii.Error, UnicodeError, TypeError, ValueError) as exc:
            self.fail(f"Unable to decode sent watch payload: {exc}")
        self.assertEqual(decoded[0]["properties"]["user_id"], 42)

    def test_each_watch_payload_has_a_fresh_timestamp_and_broadcast_identity(self) -> None:
        twitch = SimpleNamespace(
            settings=SimpleNamespace(available_drops_check=False),
        )
        channel = SimpleNamespace(_twitch=twitch, id=7, _login="streamer")
        stream = Stream(
            cast(Any, channel),
            id=123,
            game={"id": "99", "name": "Game", "displayName": "Game"},
            viewers=100,
            title="Live",
        )

        try:
            with patch("channel.isonow", side_effect=("first", "second")):
                first = json.loads(
                    base64.b64decode(stream.spade_payload(42)["data"])
                )
                second = json.loads(
                    base64.b64decode(stream.spade_payload(42)["data"])
                )
        except (binascii.Error, UnicodeError, TypeError, ValueError) as exc:
            self.fail(f"Unable to decode generated watch payload: {exc}")

        first_properties = first[0]["properties"]
        second_properties = second[0]["properties"]
        self.assertEqual(first_properties["client_time"], "first")
        self.assertEqual(second_properties["client_time"], "second")
        self.assertEqual(first_properties["broadcast_id"], "123")
        self.assertEqual(first_properties["channel_id"], "7")
        self.assertEqual(first_properties["user_id"], 42)


if __name__ == "__main__":
    unittest.main()
