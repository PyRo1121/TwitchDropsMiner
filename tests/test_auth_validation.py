from __future__ import annotations

import asyncio
import tempfile
import unittest

import aiohttp
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from constants import ClientType
from oauth_storage import OAuthTokenStore
from auth import (
    AUTH_VALIDATION_INTERVAL,
    AuthState,
)
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
        self.transport = self

    def request(self, *_args: object, **_kwargs: object) -> _Response:
        self.requests += 1
        return self.response

    async def wait_for_delay(
        self,
        _delay: float,
        *,
        deadline: datetime | None = None,
    ) -> None:
        del deadline


class AuthValidationTests(unittest.TestCase):
    def test_missing_device_cookie_is_a_controlled_login_failure(self) -> None:
        class BodyResponse(_Response):
            async def read(self) -> bytes:
                return b""

        async def exercise() -> None:
            twitch = _Twitch(BodyResponse(200, {}))
            auth = AuthState(cast(Any, twitch))
            with self.assertRaisesRegex(LoginException, "device identity"):
                await auth._ensure_device_id(
                    ClientType.WEB,
                    aiohttp.CookieJar(),
                )

        asyncio.run(exercise())

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
            auth = AuthState(cast(Any, twitch))
            auth.device_id = "device-id"
            with tempfile.TemporaryDirectory() as directory:
                auth._oauth_tokens = OAuthTokenStore(Path(directory) / "oauth.json")
                with patch("twitch.asyncio.sleep", no_sleep):
                    self.assertEqual(await auth._oauth_login(), "access")
                self.assertEqual(
                    auth._oauth_tokens.load(ClientType.WEB.CLIENT_ID), "refresh"
                )

        asyncio.run(exercise())

    def test_device_login_rejects_untrusted_verification_urls(self) -> None:
        async def exercise(verification_uri: str) -> None:
            class LoginForm:
                def __init__(self) -> None:
                    self.called = False

                async def ask_enter_code(self, _uri: object, _code: str) -> None:
                    self.called = True

            login = LoginForm()

            class TwitchStub(_Twitch):
                def __init__(self) -> None:
                    super().__init__(
                        _Response(
                            200,
                            {
                                "device_code": "device",
                                "user_code": "ABCD",
                                "interval": 1,
                                "expires_in": 60,
                                "verification_uri": verification_uri,
                            },
                        )
                    )
                    self.gui = type("Gui", (), {"login": login})()

            auth = AuthState(cast(Any, TwitchStub()))
            auth.device_id = "device-id"
            with self.assertRaises(LoginException):
                await auth._oauth_login()
            self.assertFalse(login.called)

        for verification_uri in (
            "file:///tmp/fake-login",
            "https://phishing.example/activate",
        ):
            with self.subTest(verification_uri=verification_uri):
                asyncio.run(exercise(verification_uri))

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

            auth = AuthState(cast(Any, TwitchStub()))
            auth.device_id = "device-id"
            with patch("twitch.asyncio.sleep", no_sleep):
                with self.assertRaises(LoginException):
                    await auth._oauth_login()

        asyncio.run(exercise())

    def test_valid_token_is_revalidated_after_the_hourly_interval(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(200, {"client_id": ClientType.WEB.CLIENT_ID, "user_id": "42"}))
            auth = AuthState(cast(Any, twitch))
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
            auth = AuthState(cast(Any, twitch))
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
            auth = AuthState(cast(Any, twitch))
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
            auth = AuthState(cast(Any, twitch))
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
            auth = AuthState(cast(Any, twitch))
            auth.device_id = "device-id"
            result = await auth._refresh_access_token(ClientType.WEB, "old-refresh")
            self.assertIsNone(result)

        asyncio.run(exercise())

    def test_validation_server_error_is_not_treated_as_logout(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(503, {}))
            auth = AuthState(cast(Any, twitch))
            auth.access_token = "access-token"

            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                await auth._validate_access_token(ClientType.WEB)

        asyncio.run(exercise())

    def test_expired_token_is_reported_as_invalid(self) -> None:
        async def exercise() -> None:
            twitch = _Twitch(_Response(401, {"message": "invalid token"}))
            auth = AuthState(cast(Any, twitch))
            auth.access_token = "access-token"
            result = await auth._validate_access_token(ClientType.WEB)
            self.assertFalse(result)
            self.assertEqual(twitch.requests, 1)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
