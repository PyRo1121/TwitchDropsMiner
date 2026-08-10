from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

from channel_event_service import ChannelEventService
from constants import State
from twitch import Twitch


class _Channel:
    def __init__(self) -> None:
        self.id = 7
        self.name = "channel"
        self.online = True
        self.viewers = 10
        self.check_online = Mock()
        self.set_offline = Mock()
        self.display = Mock()


class ChannelEventServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_state_payloads_are_validated(self) -> None:
        channel = _Channel()
        twitch = cast(Any, SimpleNamespace(channels={channel.id: channel}))
        service = ChannelEventService(twitch)

        for message in (
            {},
            {"type": "viewcount"},
            {"type": "viewcount", "viewers": True},
            {"type": "viewcount", "viewers": -1},
        ):
            with self.subTest(message=message):
                await service.process_stream_state(
                    channel.id,
                    cast(Any, message),
                )

        self.assertEqual(channel.viewers, 10)
        channel.display.assert_not_called()

        await service.process_stream_state(
            channel.id,
            {"type": "viewcount", "viewers": "42"},
        )

        self.assertEqual(channel.viewers, 42)
        channel.display.assert_called_once_with()

    async def test_stream_state_transitions_delegate_to_channel(self) -> None:
        channel = _Channel()
        twitch = cast(Any, SimpleNamespace(channels={channel.id: channel}))
        service = ChannelEventService(twitch)

        await service.process_stream_state(channel.id, {"type": "stream-down"})
        await service.process_stream_state(channel.id, {"type": "stream-up"})

        channel.set_offline.assert_called_once_with()
        channel.check_online.assert_called_once_with()

    async def test_broadcast_updates_require_matching_channel_identity(self) -> None:
        channel = _Channel()
        twitch = cast(Any, SimpleNamespace(channels={channel.id: channel}))
        service = ChannelEventService(twitch)

        for message in (
            {},
            {"type": "broadcast_settings_update"},
            {
                "type": "broadcast_settings_update",
                "channel_id": "8",
                "old_game": "A",
                "game": "B",
            },
            {
                "type": "broadcast_settings_update",
                "channel_id": "7",
                "old_game": None,
                "game": "B",
            },
        ):
            with self.subTest(message=message):
                await service.process_stream_update(
                    channel.id,
                    cast(Any, message),
                )

        channel.check_online.assert_not_called()

        await service.process_stream_update(
            channel.id,
            {
                "type": "broadcast_settings_update",
                "channel_id": "7",
                "old_game": "A",
                "game": "B",
            },
        )

        channel.check_online.assert_called_once_with()

    def test_watched_channel_becoming_unavailable_requests_switch(self) -> None:
        channel = _Channel()
        states: list[State] = []
        watch_service = SimpleNamespace(
            can_watch=Mock(return_value=False),
            should_switch=Mock(return_value=False),
            watch=Mock(),
        )
        twitch = cast(
            Any,
            SimpleNamespace(
                _watching_channels={channel.id: channel},
                watch_service=watch_service,
                print=Mock(),
                change_state=states.append,
            ),
        )
        service = ChannelEventService(twitch)
        stream_before = SimpleNamespace(drops_enabled=True)

        service.on_channel_update(
            cast(Any, channel),
            cast(Any, stream_before),
            None,
        )

        self.assertEqual(states, [State.CHANNEL_SWITCH])
        twitch.print.assert_called_once()
        channel.display.assert_called_once_with()

    def test_coordinator_exposes_only_the_channel_event_service(self) -> None:
        self.assertFalse(hasattr(Twitch, "process_stream_state"))
        self.assertFalse(hasattr(Twitch, "process_stream_update"))
        self.assertFalse(hasattr(Twitch, "on_channel_update"))


if __name__ == "__main__":
    unittest.main()
