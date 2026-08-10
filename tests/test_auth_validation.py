from __future__ import annotations

import asyncio
import tempfile
import unittest

import aiohttp  # pyright: ignore[reportMissingImports]
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from constants import ClientType
from oauth_storage import (
    CredentialCleanupError,
    CredentialStorageError,
    OAuthTokenStore,
)
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
    def test_cookie_invalidation_removes_disk_state_without_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.jar"
            cookie_path.write_text("stale", encoding="utf8")
            transport = SimpleNamespace(clear_cookies=Mock())
            twitch = SimpleNamespace(
                transport=transport,
                gui=SimpleNamespace(set_authenticated=lambda _value: None),
            )
            auth = AuthState(cast(Any, twitch))
            auth.access_token = "token"
            auth.user_id = 42

            with patch("auth.COOKIES_PATH", cookie_path):
                auth.invalidate(delete_cookies=True)

            transport.clear_cookies.assert_called_once_with()
            self.assertFalse(cookie_path.exists())
            self.assertFalse(hasattr(auth, "access_token"))

    def test_async_logout_clears_refresh_token_fallback(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "oauth.json"
                cookie_path = Path(directory) / "cookies.jar"
                cookie_path.write_text("cookie", encoding="utf8")
                transport = SimpleNamespace(clear_cookies=Mock())
                twitch = SimpleNamespace(
                    transport=transport,
                    gui=SimpleNamespace(
                        set_authenticated=lambda _value: None
                    ),
                )
                auth = AuthState(cast(Any, twitch))
                auth._oauth_tokens = OAuthTokenStore(
                    path,
                    use_system_vault=False,
                )
                auth._oauth_tokens.save("client-a", "refresh-secret")

                with patch("auth.COOKIES_PATH", cookie_path):
                    await auth.logout()

                self.assertFalse(path.exists())
                self.assertFalse(cookie_path.exists())
                transport.clear_cookies.assert_called_once_with()
                self.assertIsNone(
                    auth._oauth_tokens.load("client-a")
                )

        asyncio.run(exercise())

    def test_failed_logout_propagates_and_tombstones_reuse(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "oauth.json"
                encoded = OAuthTokenStore._encode_record(
                    "client-a",
                    "refresh-secret",
                )
                vault = SimpleNamespace(
                    get_password=Mock(return_value=encoded),
                    set_password=Mock(),
                    delete_password=Mock(side_effect=RuntimeError("locked")),
                )
                transport = SimpleNamespace(clear_cookies=Mock())
                twitch = SimpleNamespace(
                    transport=transport,
                    gui=SimpleNamespace(
                        set_authenticated=lambda _value: None
                    ),
                )
                auth = AuthState(cast(Any, twitch))
                auth._oauth_tokens = OAuthTokenStore(
                    path,
                    vault=cast(Any, vault),
                )

                with self.assertLogs("auth", level="WARNING") as captured:
                    with self.assertRaises(CredentialCleanupError):
                        await auth.logout()

                self.assertNotIn(
                    "refresh-secret",
                    "\n".join(captured.output),
                )
                with self.assertRaises(CredentialCleanupError):
                    auth._oauth_tokens.load("client-a")

        asyncio.run(exercise())

    def test_logout_racing_refresh_cannot_recreate_rotated_token(self) -> None:
        async def exercise() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            class BlockingResponse(_Response):
                async def __aenter__(self) -> BlockingResponse:
                    started.set()
                    await release.wait()
                    return self

            response = BlockingResponse(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "post-logout-refresh",
                },
            )
            class TwitchStub(_Twitch):
                def __init__(self) -> None:
                    super().__init__(response)
                    self.gui = SimpleNamespace(
                        set_authenticated=lambda _value: None
                    )
                    self.clear_cookies = Mock()

            twitch = TwitchStub()
            auth = AuthState(cast(Any, twitch))
            auth.device_id = "device-id"
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "oauth.json"
                cookie_path = Path(directory) / "cookies.jar"
                auth._oauth_tokens = OAuthTokenStore(
                    path,
                    use_system_vault=False,
                )
                auth._oauth_tokens.save("client-a", "old-refresh")
                refresh = asyncio.create_task(
                    auth._refresh_access_token(
                        ClientType.WEB,
                        "old-refresh",
                    )
                )
                await started.wait()

                with patch("auth.COOKIES_PATH", cookie_path):
                    await auth.logout()
                release.set()

                with self.assertRaises(LoginException):
                    await refresh
                self.assertIsNone(
                    auth._oauth_tokens.load(ClientType.WEB.CLIENT_ID)
                )
                with self.assertRaises(CredentialStorageError):
                    auth._oauth_tokens.save(
                        ClientType.WEB.CLIENT_ID,
                        "post-logout-refresh",
                    )

        asyncio.run(exercise())

    def test_authentication_migrates_refresh_token_with_a_valid_cookie(
        self,
    ) -> None:
        async def exercise() -> None:
            class LoginForm:
                def update(self, _status: str, _user_id: int | None) -> None:
                    return None

            class TwitchStub(_Twitch):
                def __init__(self) -> None:
                    super().__init__(
                        _Response(
                            200,
                            {
                                "client_id": ClientType.WEB.CLIENT_ID,
                                "user_id": "42",
                            },
                        )
                    )
                    self.gui = SimpleNamespace(login=LoginForm())

                def save_cookie_jar(
                    self,
                    _jar: aiohttp.CookieJar,
                    _path: Path,
                ) -> None:
                    return None

            twitch = TwitchStub()
            auth = AuthState(cast(Any, twitch))
            load = Mock(return_value="refresh-secret")
            auth._oauth_tokens = cast(Any, SimpleNamespace(load=load))
            jar = aiohttp.CookieJar()
            jar.update_cookies(
                {"auth-token": "access-token"},
                ClientType.WEB.CLIENT_URL,
            )

            await auth._authenticate_session(ClientType.WEB, jar)

            load.assert_called_once_with(ClientType.WEB.CLIENT_ID)
            self.assertEqual(auth.access_token, "access-token")

        asyncio.run(exercise())

    def test_refresh_token_persistence_failure_is_controlled_and_redacted(
        self,
    ) -> None:
        twitch = SimpleNamespace(
            transport=SimpleNamespace(),
            gui=SimpleNamespace(set_authenticated=lambda _value: None),
        )
        auth = AuthState(cast(Any, twitch))
        save = Mock(
            side_effect=CredentialStorageError(
                "backend exposed refresh-secret in its error"
            )
        )
        auth._oauth_tokens = cast(Any, SimpleNamespace(save=save))

        with self.assertLogs("auth", level="WARNING") as captured:
            with self.assertRaisesRegex(LoginException, "persist.*safely"):
                auth._save_refresh_token("client-a", "refresh-secret")

        output = "\n".join(captured.output)
        self.assertNotIn("refresh-secret", output)
        self.assertIn("CredentialStorageError", output)

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
                auth._oauth_tokens = OAuthTokenStore(
                    Path(directory) / "oauth.json",
                    use_system_vault=False,
                )
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
                auth._oauth_tokens = OAuthTokenStore(
                    Path(directory) / "oauth.json",
                    use_system_vault=False,
                )
                result = await auth._refresh_access_token(
                    ClientType.WEB,
                    "old-refresh",
                )
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
                auth._oauth_tokens = OAuthTokenStore(
                    Path(directory) / "oauth.json",
                    use_system_vault=False,
                )
                auth._oauth_tokens.save(
                    ClientType.WEB.CLIENT_ID,
                    "old-refresh",
                )
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
