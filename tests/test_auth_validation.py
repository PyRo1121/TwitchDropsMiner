from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from constants import ClientType
from oauth_storage import OAuthTokenStore
from twitch import AUTH_VALIDATION_INTERVAL, _AuthState
from exceptions import LoginException


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, **_kwargs: object) -> object:
        return self.payload


class _Twitch:
    _client_type = ClientType.WEB

    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests = 0

    def request(self, *_args: object, **_kwargs: object) -> _Response:
        self.requests += 1
        return self.response


class AuthValidationTests(unittest.TestCase):
    def test_device_login_validates_and_persists_success_response(self) -> None:
        async def no_sleep(_seconds: float) -> None:
            return None

        async def exercise() -> None:
            class LoginForm:
                async def ask_enter_code(self, _uri: object, _code: str) -> None:
                    return None

            class TwitchStub(_Twitch):
                def __init__(self) -> None:
                    super().__init__(_Response(200, {}))
                    self.gui = type("Gui", (), {"login": LoginForm()})()
                    self.responses = [
                        _Response(
                            200,
                            {
                                "device_code": "device",
                                "user_code": "ABCD",
                                "interval": 1,
                                "expires_in": 60,
                                "verification_uri": "https://www.twitch.tv/activate",
                            },
                        ),
                        _Response(
                            200,
                            {
                                "access_token": "access",
                                "refresh_token": "refresh",
                            },
                        ),
                    ]

                def request(self, *_args: object, **_kwargs: object) -> _Response:
                    self.requests += 1
                    return self.responses.pop(0)

            twitch = TwitchStub()
            auth = _AuthState(cast(Any, twitch))
            auth.device_id = "device-id"
            with tempfile.TemporaryDirectory() as directory:
                auth._oauth_tokens = OAuthTokenStore(Path(directory) / "oauth.json")
                with patch("twitch.asyncio.sleep", no_sleep):
                    self.assertEqual(await auth._oauth_login(), "access")
                self.assertEqual(
                    auth._oauth_tokens.load(ClientType.WEB.CLIENT_ID), "refresh"
                )

        asyncio.run(exercise())

    def test_device_login_surfaces_access_denied(self) -> None:
        async def no_sleep(_seconds: float) -> None:
            return None

        async def exercise() -> None:
            class LoginForm:
                async def ask_enter_code(self, _uri: object, _code: str) -> None:
                    return None

            class TwitchStub(_Twitch):
                def __init__(self) -> None:
                    super().__init__(_Response(200, {}))
                    self.gui = type("Gui", (), {"login": LoginForm()})()
                    self.responses = [
                        _Response(
                            200,
                            {
                                "device_code": "device",
                                "user_code": "ABCD",
                                "interval": 1,
                                "expires_in": 60,
                                "verification_uri": "https://www.twitch.tv/activate",
                            },
                        ),
                        _Response(400, {"error": "access_denied"}),
                    ]

                def request(self, *_args: object, **_kwargs: object) -> _Response:
                    return self.responses.pop(0)

            auth = _AuthState(cast(Any, TwitchStub()))
            auth.device_id = "device-id"
            with patch("twitch.asyncio.sleep", no_sleep):
                with self.assertRaises(LoginException):
                    await auth._oauth_login()

        asyncio.run(exercise())

    def test_valid_token_is_revalidated_after_the_hourly_interval(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(200, {"client_id": ClientType.WEB.CLIENT_ID, "user_id": "42"}))
            auth = _AuthState(cast(Any, twitch))
            auth.access_token = "access-token"
            auth.user_id = 42
            auth._last_validated = datetime.now(timezone.utc) - AUTH_VALIDATION_INTERVAL - timedelta(seconds=1)

            await auth.validate()

            self.assertEqual(twitch.requests, 1)
            self.assertTrue(auth._logged_in.is_set())

        asyncio.run(exercise())

    def test_recent_validation_does_not_repeat_the_request(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(500, {}))
            auth = _AuthState(cast(Any, twitch))
            auth.access_token = "access-token"
            auth.user_id = 42
            auth._last_validated = datetime.now(timezone.utc)

            await auth.validate()

            self.assertEqual(twitch.requests, 0)
            self.assertTrue(auth._logged_in.is_set())

        asyncio.run(exercise())

    def test_refresh_rotates_and_persists_the_new_refresh_token(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(200, {"access_token": "new-access", "refresh_token": "new-refresh"}))
            auth = _AuthState(cast(Any, twitch))
            auth.device_id = "device-id"
            with tempfile.TemporaryDirectory() as directory:
                auth._oauth_tokens = OAuthTokenStore(Path(directory) / "oauth.json")
                result = await auth._refresh_access_token(ClientType.WEB, "old-refresh")
                self.assertEqual(result, "new-access")
                self.assertEqual(
                    auth._oauth_tokens.load(ClientType.WEB.CLIENT_ID), "new-refresh"
                )

        asyncio.run(exercise())

    def test_refresh_without_rotation_keeps_the_existing_refresh_token(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(200, {"access_token": "new-access"}))
            auth = _AuthState(cast(Any, twitch))
            auth.device_id = "device-id"
            with tempfile.TemporaryDirectory() as directory:
                auth._oauth_tokens = OAuthTokenStore(Path(directory) / "oauth.json")
                auth._oauth_tokens.save(ClientType.WEB.CLIENT_ID, "old-refresh")
                result = await auth._refresh_access_token(ClientType.WEB, "old-refresh")
                self.assertEqual(result, "new-access")
                self.assertEqual(
                    auth._oauth_tokens.load(ClientType.WEB.CLIENT_ID), "old-refresh"
                )

        asyncio.run(exercise())

    def test_invalid_refresh_token_falls_back_to_device_login(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(400, {"message": "Invalid refresh token"}))
            auth = _AuthState(cast(Any, twitch))
            auth.device_id = "device-id"
            result = await auth._refresh_access_token(ClientType.WEB, "old-refresh")
            self.assertIsNone(result)

        asyncio.run(exercise())

    def test_validation_server_error_is_not_treated_as_logout(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(503, {}))
            auth = _AuthState(cast(Any, twitch))
            auth.access_token = "access-token"

            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                await auth._validate_access_token(ClientType.WEB)

        asyncio.run(exercise())

    def test_expired_token_is_reported_as_invalid(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(401, {"message": "invalid token"}))
            auth = _AuthState(cast(Any, twitch))
            auth.access_token = "access-token"
            result = await auth._validate_access_token(ClientType.WEB)
            self.assertFalse(result)
            self.assertEqual(twitch.requests, 1)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
