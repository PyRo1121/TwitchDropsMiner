from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from constants import State
from drop_event_service import DropEventService
from twitch import Twitch


class _Channel:
    id = 7
    name = "channel"


class DropEventServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_drop_events_are_ignored(self) -> None:
        twitch = cast(
            Any,
            SimpleNamespace(
                _inventory_generation=1,
                _drops={},
                watch_service=SimpleNamespace(
                    assigned_channels=Mock(return_value=[]),
                    adopt_unassigned_drop=Mock(return_value=[]),
                ),
            ),
        )
        service = DropEventService(twitch)

        for message in (
            {},
            {"type": "unknown"},
            {"type": "drop-progress"},
            {"type": "drop-progress", "data": []},
            {"type": "drop-progress", "data": {"drop_id": ""}},
        ):
            with self.subTest(message=message):
                await service.process_drops(42, cast(Any, message))

        twitch.watch_service.assigned_channels.assert_not_called()
        twitch.watch_service.adopt_unassigned_drop.assert_not_called()

    async def test_unknown_assigned_drop_requests_inventory_refresh(self) -> None:
        channel = _Channel()
        states: list[State] = []
        twitch = cast(
            Any,
            SimpleNamespace(
                _inventory_generation=1,
                _drops={},
                watch_service=SimpleNamespace(
                    assigned_channels=Mock(return_value=[channel]),
                ),
                change_state=states.append,
            ),
        )

        await DropEventService(twitch).process_drops(
            42,
            {
                "type": "drop-progress",
                "data": {
                    "drop_id": "unknown-drop",
                    "current_progress_min": 1,
                    "required_progress_min": 10,
                },
            },
        )

        self.assertEqual(states, [State.INVENTORY_FETCH])

    async def test_notifications_are_validated_before_deletion(self) -> None:
        states: list[State] = []
        gql_request = AsyncMock(return_value={})
        twitch = cast(
            Any,
            SimpleNamespace(
                change_state=states.append,
                transport=SimpleNamespace(gql_request=gql_request),
            ),
        )
        service = DropEventService(twitch)

        for message in (
            {},
            {"type": "create-notification"},
            {"type": "create-notification", "data": []},
            {"type": "create-notification", "data": {}},
            {
                "type": "create-notification",
                "data": {"notification": {"type": "unrelated", "id": "1"}},
            },
        ):
            with self.subTest(message=message):
                await service.process_notifications(42, cast(Any, message))

        self.assertEqual(states, [])
        gql_request.assert_not_awaited()

        await service.process_notifications(
            42,
            {
                "type": "create-notification",
                "data": {
                    "notification": {
                        "type": "user_drop_reward_reminder_notification",
                        "id": "notification-id",
                    }
                },
            },
        )

        self.assertEqual(states, [State.INVENTORY_FETCH])
        gql_request.assert_awaited_once()

    async def test_reward_notification_without_id_still_refreshes_inventory(self) -> None:
        states: list[State] = []
        gql_request = AsyncMock(return_value={})
        twitch = cast(
            Any,
            SimpleNamespace(
                change_state=states.append,
                transport=SimpleNamespace(gql_request=gql_request),
            ),
        )

        await DropEventService(twitch).process_notifications(
            42,
            {
                "type": "create-notification",
                "data": {
                    "notification": {
                        "type": "quests_viewer_reward_campaign_earned_emote"
                    }
                },
            },
        )

        self.assertEqual(states, [State.INVENTORY_FETCH])
        gql_request.assert_not_awaited()

    def test_coordinator_exposes_only_the_event_service(self) -> None:
        self.assertFalse(hasattr(Twitch, "process_drops"))
        self.assertFalse(hasattr(Twitch, "process_notifications"))


if __name__ == "__main__":
    unittest.main()
