from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from channel import Stream


class StreamWatchPayloadTests(unittest.TestCase):
    def test_each_watch_payload_has_a_fresh_timestamp_and_broadcast_identity(self) -> None:
        twitch = SimpleNamespace(
            settings=SimpleNamespace(available_drops_check=False),
            _auth_state=SimpleNamespace(user_id=42),
        )
        channel = SimpleNamespace(_twitch=twitch, id=7, _login="streamer")
        stream = Stream(
            cast(Any, channel),
            id=123,
            game={"id": "99", "name": "Game", "displayName": "Game"},
            viewers=100,
            title="Live",
        )

        with patch("channel.isonow", side_effect=("first", "second")):
            first = json.loads(base64.b64decode(stream.spade_payload["data"]))
            second = json.loads(base64.b64decode(stream.spade_payload["data"]))

        first_properties = first[0]["properties"]
        second_properties = second[0]["properties"]
        self.assertEqual(first_properties["client_time"], "first")
        self.assertEqual(second_properties["client_time"], "second")
        self.assertEqual(first_properties["broadcast_id"], "123")
        self.assertEqual(first_properties["channel_id"], "7")
        self.assertEqual(first_properties["user_id"], 42)


if __name__ == "__main__":
    unittest.main()
