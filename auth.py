from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from yarl import URL

from constants import COOKIES_PATH, OAUTH_TOKEN_PATH
from exceptions import LoginException, RequestInvalid
from oauth_storage import CredentialStorageError, OAuthTokenStore
from translate import _
from http_transport import (
    read_json,
    retry_after_delay,
)
from utils import (
    CHARS_HEX_LOWER,
    create_nonce,
    redact_log_value,
    remove_file,
    require_int,
)

if TYPE_CHECKING:
    from constants import ClientInfo, JsonType
    from gui_port import LoginPort
    from twitch import Twitch

logger = logging.getLogger(__name__)

AUTH_VALIDATION_INTERVAL = timedelta(hours=1)


class AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._transport = twitch.transport
        self._lock = asyncio.Lock()
        self._oauth_tokens = OAuthTokenStore(OAUTH_TOKEN_PATH)
        self._logged_in = asyncio.Event()
        self._last_validated: datetime | None = None
        self._generation = 0
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self._access_token: str

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def access_token(self) -> str:
        return self._access_token

    @access_token.setter
    def access_token(self, value: str) -> None:
        self._access_token = value
        self._generation += 1

    @access_token.deleter
    def access_token(self) -> None:
        del self._access_token
        self._generation += 1

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            with suppress(AttributeError):
                delattr(self, attr)

    def invalidate(
        self,
        *,
        delete_cookies: bool = False,
    ) -> None:
        self._delattrs("access_token", "user_id")
        self._last_validated = None
        self._logged_in.clear()
        self._twitch.gui.set_authenticated(False)
        if delete_cookies:
            self._transport.clear_cookies()
            remove_file(COOKIES_PATH)

    async def logout(self) -> None:
        """Drain authentication, tombstone credentials, and clear cookies."""
        # Invalidate post-await writers immediately, including callers that did
        # not originate under ``validate()`` and therefore do not hold _lock.
        self._generation += 1
        async with self._lock:
            self._delattrs("access_token", "user_id")
            self._last_validated = None
            self._logged_in.clear()
            self._twitch.gui.set_authenticated(False)

            credential_error: CredentialStorageError | OSError | None = None
            try:
                self._oauth_tokens.clear()
            except (CredentialStorageError, OSError) as exc:
                credential_error = exc
                logger.warning(
                    "Local authentication cleanup failed: %s",
                    type(exc).__name__,
                )

            cookie_error: OSError | None = None
            self._transport.clear_cookies()
            try:
                remove_file(COOKIES_PATH)
            except OSError as exc:
                cookie_error = exc
                logger.warning(
                    "Local cookie cleanup failed: %s",
                    type(exc).__name__,
                )

            if credential_error is not None:
                raise credential_error
            if cookie_error is not None:
                raise LoginException(
                    "Local authentication cookie cleanup was incomplete"
                ) from None

    def invalidate_if_current(self, generation: int) -> bool:
        if generation != self._generation:
            return False
        self.invalidate()
        return True

    def clear(self) -> None:
        self._delattrs(
            "user_id",
            "device_id",
            "session_id",
            "access_token",
        )
        self._last_validated = None
        self._logged_in.clear()
        self._twitch.gui.set_authenticated(False)

    def _clear_refresh_token(self) -> None:
        try:
            self._oauth_tokens.clear()
        except (CredentialStorageError, OSError) as exc:
            logger.warning(
                "Local authentication cleanup failed: %s",
                type(exc).__name__,
            )
            raise LoginException(
                "Local OAuth credential cleanup was incomplete"
            ) from None

    def _save_refresh_token(
        self,
        client_id: str,
        refresh_token: str,
        *,
        rotated: bool = False,
        new_session: bool = False,
    ) -> None:
        description = (
            "rotated OAuth refresh token"
            if rotated
            else "OAuth refresh token"
        )
        try:
            self._oauth_tokens.save(
                client_id,
                refresh_token,
                new_session=new_session,
            )
        except (CredentialStorageError, OSError, TypeError, ValueError) as exc:
            logger.warning(
                "Unable to persist %s: %s",
                description,
                type(exc).__name__,
            )
            raise LoginException(
                f"Unable to persist {description} safely"
            ) from None

    def _require_generation(self, generation: int) -> None:
        if generation != self._generation:
            raise LoginException(
                "Authentication state changed while a token request was pending"
            )

    def _oauth_headers(self, client_info: ClientInfo) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
            "Host": "id.twitch.tv",
            "Origin": str(client_info.CLIENT_URL),
            "Pragma": "no-cache",
            "Referer": str(client_info.CLIENT_URL),
            "User-Agent": client_info.USER_AGENT,
            "X-Device-Id": self.device_id,
        }

    async def _refresh_access_token(
        self,
        client_info: ClientInfo,
        refresh_token: str,
    ) -> str | None:
        generation = self._generation
        payload = {
            "client_id": client_info.CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        async with self._transport.request(
            "POST",
            "https://id.twitch.tv/oauth2/token",
            headers=self._oauth_headers(client_info),
            data=payload,
        ) as response:
            self._require_generation(generation)
            if response.status in (400, 401):
                return None
            if response.status != 200:
                raise RuntimeError(
                    f"OAuth refresh failed (HTTP {response.status})"
                )
            response_json: JsonType = await read_json(
                response,
                RuntimeError,
                "OAuth refresh returned invalid data",
            )
            if not isinstance(response_json, dict):
                raise RuntimeError("OAuth refresh returned invalid data")
            self._require_generation(generation)
            access_token = response_json.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise LoginException(
                    "OAuth refresh response omitted access_token"
                )
            rotated_token = response_json.get("refresh_token")
            if isinstance(rotated_token, str) and rotated_token:
                self._save_refresh_token(
                    client_info.CLIENT_ID,
                    rotated_token,
                    rotated=True,
                )
            else:
                logger.debug(
                    "Twitch refresh response did not rotate the refresh token"
                )
            return access_token

    async def _poll_device_token(
        self,
        headers: dict[str, str],
        payload: JsonType,
        interval: float,
        expires_at: datetime,
        client_info: ClientInfo,
        generation: int,
    ) -> str:
        while True:
            self._require_generation(generation)
            await self._transport.wait_for_delay(
                interval,
                deadline=expires_at,
            )
            self._require_generation(generation)
            async with self._transport.request(
                "POST",
                "https://id.twitch.tv/oauth2/token",
                headers=headers,
                data=payload,
                invalidate_after=expires_at,
            ) as response:
                self._require_generation(generation)
                if response.status == 200:
                    response_json = await read_json(
                        response,
                        LoginException,
                        "OAuth token response was not valid JSON",
                    )
                    if not isinstance(response_json, dict):
                        raise LoginException(
                            "OAuth token response was malformed"
                        )
                    self._require_generation(generation)
                    access_token = response_json.get("access_token")
                    if not isinstance(access_token, str) or not access_token:
                        raise LoginException(
                            "OAuth token response omitted access_token"
                        )
                    refresh_token = response_json.get("refresh_token")
                    if isinstance(refresh_token, str) and refresh_token:
                        self._save_refresh_token(
                            client_info.CLIENT_ID,
                            refresh_token,
                            new_session=True,
                        )
                    else:
                        logger.debug(
                            "Twitch device login response did not include a "
                            "refresh token"
                        )
                    self.access_token = access_token
                    return self.access_token

                try:
                    response_json = await response.json(loads=json.loads)
                except (
                    aiohttp.ContentTypeError,
                    TypeError,
                    UnicodeError,
                    ValueError,
                ):
                    response_json = {}
                if not isinstance(response_json, dict):
                    response_json = {}
                error = response_json.get("message") or response_json.get("error")
                if error == "slow_down":
                    retry_delay = retry_after_delay(response, interval + 5)
                    interval = max(interval + 5, retry_delay)
                    continue
                if error == "authorization_pending":
                    continue
                if error == "expired_token":
                    raise RequestInvalid()
                if error == "access_denied":
                    raise LoginException(
                        "OAuth device authorization was denied"
                    )
                raise LoginException(
                    "OAuth device authorization failed "
                    f"(HTTP {response.status}): "
                    f"{redact_log_value(response_json)}"
                )

    async def _oauth_login(self) -> str:
        generation = self._generation
        login_form: LoginPort = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type
        headers = self._oauth_headers(client_info)
        payload = {
            "client_id": client_info.CLIENT_ID,
            "scopes": "",
        }
        while True:
            self._require_generation(generation)
            try:
                now = datetime.now(timezone.utc)
                async with self._transport.request(
                    "POST",
                    "https://id.twitch.tv/oauth2/device",
                    headers=headers,
                    data=payload,
                ) as response:
                    response_json: Any = await read_json(
                        response,
                        LoginException,
                        "OAuth device response was not valid JSON",
                    )
                    if not isinstance(response_json, dict):
                        raise LoginException(
                            "OAuth device response was malformed"
                        )
                    device_code = response_json.get("device_code")
                    user_code = response_json.get("user_code")
                    verification_uri_value = response_json.get(
                        "verification_uri"
                    )
                    if not all(
                        isinstance(value, str) and value
                        for value in (
                            device_code,
                            user_code,
                            verification_uri_value,
                        )
                    ):
                        raise LoginException(
                            "OAuth device response was incomplete"
                        )
                    device_code = cast(str, device_code)
                    user_code = cast(str, user_code)
                    verification_uri_value = cast(
                        str,
                        verification_uri_value,
                    )
                    try:
                        interval = max(
                            1,
                            require_int(
                                response_json["interval"],
                                "Invalid OAuth polling interval",
                            ),
                        )
                        expires_in = require_int(
                            response_json["expires_in"],
                            "Invalid OAuth expiration",
                        )
                        if expires_in <= 0:
                            raise ValueError("Invalid OAuth expiration")
                        verification_uri = URL(verification_uri_value)
                        if (
                            verification_uri.scheme != "https"
                            or verification_uri.host
                            not in {"twitch.tv", "www.twitch.tv"}
                        ):
                            raise ValueError("Untrusted OAuth verification URL")
                    except (KeyError, TypeError, ValueError) as exc:
                        raise LoginException(
                            "OAuth device response had invalid values"
                        ) from exc
                    expires_at = now + timedelta(seconds=expires_in)

                await login_form.ask_enter_code(
                    verification_uri,
                    user_code,
                )
                self._require_generation(generation)
                token_payload = {
                    "client_id": client_info.CLIENT_ID,
                    "scopes": "",
                    "device_code": device_code,
                    "grant_type": (
                        "urn:ietf:params:oauth:grant-type:device_code"
                    ),
                }
                return await self._poll_device_token(
                    headers,
                    token_payload,
                    interval,
                    expires_at,
                    client_info,
                    generation,
                )
            except RequestInvalid:
                continue

    def headers(
        self,
        *,
        user_agent: str = "",
        gql: bool = False,
    ) -> JsonType:
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"):
            headers["Client-Session-Id"] = self.session_id
        if hasattr(self, "device_id"):
            headers["X-Device-Id"] = self.device_id
        if gql:
            headers["Origin"] = str(client_info.CLIENT_URL)
            headers["Referer"] = str(client_info.CLIENT_URL)
            headers["Authorization"] = f"OAuth {self.access_token}"
        return headers

    async def validate(self) -> None:
        async with self._lock:
            await self._validate()

    async def _validate_access_token(
        self,
        client_info: ClientInfo,
    ) -> bool:
        async with self._transport.request(
            "GET",
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {self.access_token}"},
        ) as response:
            if response.status == 401:
                return False
            if response.status != 200:
                raise RuntimeError(
                    f"Token validation failed (HTTP {response.status})"
                )
            payload: JsonType = await read_json(
                response,
                RuntimeError,
                "Token validation returned invalid data",
            )
            try:
                validated_user_id = require_int(
                    payload["user_id"],
                    "Token validation returned an invalid user ID",
                )
                validated_client_id = str(payload["client_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Token validation returned invalid data"
                ) from exc
            return (
                validated_client_id == client_info.CLIENT_ID
                and validated_user_id == self.user_id
            )

    async def _ensure_device_id(
        self,
        client_info: ClientInfo,
        jar: aiohttp.CookieJar,
    ) -> None:
        async with self._transport.request(
            "GET",
            client_info.CLIENT_URL,
            headers=self.headers(),
        ) as response:
            await response.read()
        cookies = jar.filter_cookies(client_info.CLIENT_URL)
        device_cookie = cookies.get("unique_id")
        if device_cookie is None or not device_cookie.value:
            raise LoginException("Twitch did not provide a device identity cookie")
        self.device_id = device_cookie.value

    async def _reuse_valid_session(
        self,
        client_info: ClientInfo,
        now: datetime,
    ) -> bool:
        if not self._hasattrs("access_token", "user_id"):
            return False
        if (
            self._last_validated is not None
            and now - self._last_validated < AUTH_VALIDATION_INTERVAL
        ):
            self._logged_in.set()
            return True
        if await self._validate_access_token(client_info):
            self._last_validated = now
            self._logged_in.set()
            return True
        self.invalidate(delete_cookies=True)
        return False

    async def _authenticate_session(
        self,
        client_info: ClientInfo,
        jar: aiohttp.CookieJar,
    ) -> None:
        login_form: LoginPort = self._twitch.gui.login
        logger.info("Checking login")
        login_form.update(_("gui", "login", "logging_in"), None)
        validate_response: JsonType = {}
        cookie: Any = None
        try:
            stored_refresh_token = self._oauth_tokens.load(
                client_info.CLIENT_ID
            )
        except (CredentialStorageError, OSError) as exc:
            logger.warning(
                "Unable to load stored OAuth credentials: %s",
                type(exc).__name__,
            )
            raise LoginException(
                "Stored OAuth credentials could not be read safely"
            ) from None
        for _client_mismatch_attempt in range(2):
            for _invalid_token_attempt in range(2):
                cookie = jar.filter_cookies(client_info.CLIENT_URL)
                if "auth-token" not in cookie:
                    refresh_token = stored_refresh_token
                    refreshed_token = None
                    if refresh_token is not None:
                        logger.info("Refreshing Twitch OAuth session")
                        refreshed_token = await self._refresh_access_token(
                            client_info,
                            refresh_token,
                        )
                    if refreshed_token is None:
                        if refresh_token is not None:
                            logger.info(
                                "Stored Twitch refresh token is invalid"
                            )
                            self._clear_refresh_token()
                            stored_refresh_token = None
                        self.access_token = await self._oauth_login()
                    else:
                        self.access_token = refreshed_token
                    cookie["auth-token"] = self.access_token
                elif not hasattr(self, "access_token"):
                    logger.info("Restoring session from cookie")
                    self.access_token = cookie["auth-token"].value

                async with self._transport.request(
                    "GET",
                    "https://id.twitch.tv/oauth2/validate",
                    headers={
                        "Authorization": f"OAuth {self.access_token}"
                    },
                ) as response:
                    if response.status == 401:
                        logger.info("Restored session is invalid")
                        client_host = client_info.CLIENT_URL.host
                        if client_host is None:
                            raise RuntimeError("Twitch client URL has no host")
                        jar.clear_domain(client_host)
                        continue
                    if response.status == 200:
                        validate_response = await read_json(
                            response,
                            RuntimeError,
                            "Login validation returned invalid JSON",
                        )
                        if not isinstance(validate_response, dict):
                            raise RuntimeError(
                                "Login validation returned malformed data"
                            )
                        break
            else:
                raise RuntimeError("Login verification failure (step #2)")
            if validate_response.get("client_id") == client_info.CLIENT_ID:
                break
            logger.info("Cookie client ID mismatch")
            jar.clear()
            remove_file(COOKIES_PATH)
            self._clear_refresh_token()
            stored_refresh_token = None
        else:
            raise RuntimeError("Login verification failure (step #1)")

        try:
            self.user_id = require_int(
                validate_response["user_id"],
                "Login verification returned an invalid user ID",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Login verification returned an invalid user ID"
            ) from exc
        if cookie is None:
            raise RuntimeError("Authentication cookie was not initialized")
        cookie["persistent"] = str(self.user_id)
        logger.info("Login successful, user ID: %s", self.user_id)
        login_form.update(_("gui", "login", "logged_in"), self.user_id)
        jar.update_cookies(cookie, client_info.CLIENT_URL)
        self._transport.save_cookie_jar(jar, COOKIES_PATH)

    async def _validate(self) -> None:
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        client_info: ClientInfo = self._twitch._client_type
        now = datetime.now(timezone.utc)
        if await self._reuse_valid_session(client_info, now):
            return
        jar: aiohttp.CookieJar | None = None
        if (
            not self._hasattrs("device_id")
            or not self._hasattrs("access_token", "user_id")
        ):
            session = await self._transport.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
        if not self._hasattrs("device_id"):
            if jar is None:
                raise RuntimeError(
                    "Authentication cookie jar is unavailable"
                )
            await self._ensure_device_id(client_info, jar)
        if not self._hasattrs("access_token", "user_id"):
            if jar is None:
                raise RuntimeError(
                    "Authentication cookie jar is unavailable"
                )
            await self._authenticate_session(client_info, jar)
        self._twitch.gui.set_authenticated(True)
        self._last_validated = datetime.now(timezone.utc)
        self._logged_in.set()
