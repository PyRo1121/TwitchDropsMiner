from __future__ import annotations

import json
import asyncio
import logging
from time import time
from copy import deepcopy
from itertools import chain
from functools import partial
from collections import abc, deque, OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import suppress, asynccontextmanager
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Final, overload, cast, TYPE_CHECKING

import aiohttp
from yarl import URL

from translate import _
from oauth_storage import OAuthTokenStore
from channel import Channel
from websocket import WebsocketPool
from inventory import DropsCampaign
from exceptions import (
    ExitRequest,
    GQLException,
    ReloadRequest,
    LoginException,
    MinerException,
    RequestInvalid,
    CaptchaRequired,
    RequestException,
)
from utils import (
    CHARS_HEX_LOWER,
    chunk,
    timestamp,
    cancel_tasks,
    create_nonce,
    task_wrapper,
    RateLimiter,
    AwaitableValue,
    ExponentialBackoff,
    redact_log_value,
    atomic_write,
    remove_stale_new,
)
from constants import (
    CALL,
    MAX_INT,
    DUMP_PATH,
    COOKIES_PATH,
    OAUTH_TOKEN_PATH,
    MAX_CHANNELS,
    MAX_WATCH_CHANNELS,
    GQL_QUERIES,
    WATCH_INTERVAL,
    State,
    ClientType,
    PriorityMode,
    WebsocketTopic,
)

if TYPE_CHECKING:
    from utils import Game
    from gui_qt.subs import QtLoginForm as LoginForm
    from channel import Stream
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo, JsonType, GQLOperation


logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")
AUTH_VALIDATION_INTERVAL = timedelta(hours=1)


class SkipExtraJsonDecoder(json.JSONDecoder):
    def decode(self, s: str, _w: Any = None) -> Any:
        # skip whitespace check
        obj, end = self.raw_decode(s)
        return obj


def safe_loads(s: str) -> Any:
    try:
        return json.loads(s, cls=SkipExtraJsonDecoder)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid JSON response") from exc


SAFE_LOADS = safe_loads


def _open_dump(mode: Literal["w", "a"]) -> Any:
    try:
        return open(DUMP_PATH, mode, encoding="utf8")
    except OSError as exc:
        raise RuntimeError(f"Unable to open dump file: {DUMP_PATH}") from exc


class _AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._oauth_tokens = OAuthTokenStore(OAUTH_TOKEN_PATH)
        self._logged_in = asyncio.Event()
        self._last_validated: datetime | None = None
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self.access_token: str

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def invalidate(
        self, *, delete_cookies: bool = False, delete_refresh_token: bool = False
    ) -> None:
        self._delattrs("access_token", "user_id")
        self._last_validated = None
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")
        if delete_cookies:
            session = self._twitch._session
            if session is not None:
                jar = cast(aiohttp.CookieJar, session.cookie_jar)
                jar.clear()
                remove_stale_new(COOKIES_PATH)
        if delete_refresh_token:
            self._clear_refresh_token()

    def clear(self) -> None:
        self._delattrs(
            "user_id",
            "device_id",
            "session_id",
            "access_token",
        )
        self._last_validated = None
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")

    def _clear_refresh_token(self) -> None:
        try:
            self._oauth_tokens.clear()
        except OSError as exc:
            logger.warning("Unable to clear OAuth refresh token: %s", type(exc).__name__)

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
        self, client_info: ClientInfo, refresh_token: str
    ) -> str | None:
        payload = {
            "client_id": client_info.CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        async with self._twitch.request(
            "POST",
            "https://id.twitch.tv/oauth2/token",
            headers=self._oauth_headers(client_info),
            data=payload,
        ) as response:
            if response.status in (400, 401):
                return None
            if response.status != 200:
                raise RuntimeError(
                    f"OAuth refresh failed (HTTP {response.status})"
                )
            try:
                response_json: JsonType = await response.json(loads=SAFE_LOADS)
            except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError) as exc:
                raise RuntimeError("OAuth refresh returned invalid data") from exc
            if not isinstance(response_json, dict):
                raise RuntimeError("OAuth refresh returned invalid data")
            access_token = response_json.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise LoginException("OAuth refresh response omitted access_token")
            rotated_token = response_json.get("refresh_token")
            if isinstance(rotated_token, str) and rotated_token:
                try:
                    self._oauth_tokens.save(client_info.CLIENT_ID, rotated_token)
                except (OSError, TypeError, ValueError) as exc:
                    logger.warning(
                        "Unable to persist rotated OAuth refresh token: %s",
                        type(exc).__name__,
                    )
            else:
                logger.debug("Twitch refresh response did not rotate the refresh token")
            return access_token

    async def _oauth_login(self) -> str:
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type
        headers = self._oauth_headers(client_info)
        payload = {
            "client_id": client_info.CLIENT_ID,
            "scopes": "",  # no scopes needed
        }
        while True:
            try:
                now = datetime.now(timezone.utc)
                async with self._twitch.request(
                    "POST", "https://id.twitch.tv/oauth2/device", headers=headers, data=payload
                ) as response:
                    # {
                    #     "device_code": "40 chars [A-Za-z0-9]",
                    #     "expires_in": 1800,
                    #     "interval": 5,
                    #     "user_code": "8 chars [A-Z]",
                    #     "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH"
                    # }
                    try:
                        response_json: Any = await response.json(loads=SAFE_LOADS)
                    except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError) as exc:
                        raise LoginException("OAuth device response was not valid JSON") from exc
                    if not isinstance(response_json, dict):
                        raise LoginException("OAuth device response was malformed")
                    device_code = response_json.get("device_code")
                    user_code = response_json.get("user_code")
                    verification_uri_value = response_json.get("verification_uri")
                    if not all(
                        isinstance(value, str) and value
                        for value in (device_code, user_code, verification_uri_value)
                    ):
                        raise LoginException("OAuth device response was incomplete")
                    device_code = cast(str, device_code)
                    user_code = cast(str, user_code)
                    verification_uri_value = cast(str, verification_uri_value)
                    try:
                        interval = max(1, int(response_json["interval"]))
                        expires_in = int(response_json["expires_in"])
                        verification_uri = URL(verification_uri_value)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise LoginException("OAuth device response had invalid timing") from exc
                    expires_at = now + timedelta(seconds=expires_in)

                # Print the code to the user, open them the activate page so they can type it in
                await login_form.ask_enter_code(verification_uri, user_code)

                payload = {
                    "client_id": self._twitch._client_type.CLIENT_ID,
                    "scopes": "",
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
                while True:
                    # sleep first, not like the user is gonna enter the code *that* fast
                    await asyncio.sleep(interval)
                    async with self._twitch.request(
                        "POST",
                        "https://id.twitch.tv/oauth2/token",
                        headers=headers,
                        data=payload,
                        invalidate_after=expires_at,
                    ) as response:
                        if response.status == 200:
                            try:
                                response_json = await response.json(loads=SAFE_LOADS)
                            except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError) as exc:
                                raise LoginException("OAuth token response was not valid JSON") from exc
                            if not isinstance(response_json, dict):
                                raise LoginException("OAuth token response was malformed")
                            # {
                            #     "access_token": "40 chars [A-Za-z0-9]",
                            #     "refresh_token": "40 chars [A-Za-z0-9]",
                            #     "scope": [...],
                            #     "token_type": "bearer"
                            # }
                            access_token = response_json.get("access_token")
                            if not isinstance(access_token, str) or not access_token:
                                raise LoginException("OAuth token response omitted access_token")
                            refresh_token = response_json.get("refresh_token")
                            if isinstance(refresh_token, str) and refresh_token:
                                try:
                                    self._oauth_tokens.save(client_info.CLIENT_ID, refresh_token)
                                except (OSError, TypeError, ValueError) as exc:
                                    logger.warning(
                                        "Unable to persist OAuth refresh token: %s",
                                        type(exc).__name__,
                                    )
                            else:
                                logger.debug(
                                    "Twitch device login response did not include a refresh token"
                                )
                            self.access_token = access_token
                            return self.access_token
                        try:
                            response_json = await response.json(loads=SAFE_LOADS)
                        except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError):
                            response_json = {}
                        if not isinstance(response_json, dict):
                            response_json = {}
                        error = response_json.get("message") or response_json.get("error")
                        if response.status == 429 or error == "slow_down":
                            retry_after = response.headers.get("Retry-After")
                            if retry_after is None:
                                retry_delay = interval + 5
                            else:
                                try:
                                    retry_delay = max(1, int(float(retry_after)))
                                except ValueError:
                                    retry_delay = interval + 5
                            interval = max(interval + 5, retry_delay)
                            continue
                        if error == "authorization_pending":
                            continue
                        if error == "expired_token":
                            raise RequestInvalid()
                        if error == "access_denied":
                            raise LoginException("OAuth device authorization was denied")
                        raise LoginException(
                            "OAuth device authorization failed "
                            f"(HTTP {response.status}): {redact_log_value(response_json)}"
                        )
            except RequestInvalid:
                # the device_code has expired, request a new code
                continue

    async def _login(self) -> str:
        logger.info("Login flow started")
        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type

        token_kind: str = ''
        payload: JsonType = {
            # username and password are added later
            # "username": str,
            # "password": str,
            # client ID to-be associated with the access token
            "client_id": client_info.CLIENT_ID,
            "undelete_user": False,  # purpose unknown
            "remember_me": True,  # persist the session via the cookie
            # "authy_token": str,  # 2FA token
            # "twitchguard_code": str,  # email code
            # "captcha": str,  # self-fed captcha
            # 'force_twitchguard': False,  # force email code confirmation
        }

        while True:
            login_data = await login_form.ask_login()
            payload["username"] = login_data.username
            payload["password"] = login_data.password
            # reinstate the 2FA token, if present
            payload.pop("authy_token", None)
            payload.pop("twitchguard_code", None)
            if login_data.token:
                # if there's no token kind set yet, and the user has entered a token,
                # we can immediately assume it's an authenticator token and not an email one
                if not token_kind:
                    token_kind = "authy"
                if token_kind == "authy":
                    payload["authy_token"] = login_data.token
                elif token_kind == "email":
                    payload["twitchguard_code"] = login_data.token

            # use fancy headers to mimic the twitch android app
            headers = {
                "Accept": "application/vnd.twitchtv.v3+json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US",
                "Client-Id": client_info.CLIENT_ID,
                "Content-Type": "application/json; charset=UTF-8",
                "Host": "passport.twitch.tv",
                "User-Agent": client_info.USER_AGENT,
                "X-Device-Id": self.device_id,
                # "X-Device-Id": ''.join(random.choices('0123456789abcdef', k=32)),
            }
            async with self._twitch.request(
                "POST", "https://passport.twitch.tv/login", headers=headers, json=payload
            ) as response:
                try:
                    login_response: Any = await response.json(loads=SAFE_LOADS)
                except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError) as exc:
                    raise LoginException("Twitch login response was not valid JSON") from exc
            if not isinstance(login_response, dict):
                raise LoginException("Twitch login response was malformed")

            # Feed this back in to avoid running into CAPTCHA if possible
            if "captcha_proof" in login_response:
                payload["captcha"] = {"proof": login_response["captcha_proof"]}

            # Error handling
            if "error_code" in login_response:
                error_code_value = login_response["error_code"]
                if not isinstance(error_code_value, int):
                    raise LoginException("Twitch login response had an invalid error code")
                error_code = error_code_value
                logger.info(f"Login error code: {error_code}")
                if error_code == 1000:
                    logger.info("1000: CAPTCHA is required")
                    raise CaptchaRequired()
                elif error_code in (2004, 3001):
                    logger.info("3001: Login failed due to incorrect username or password")
                    gui_print(_("login", "incorrect_login_pass"))
                    if error_code == 2004:
                        # invalid username
                        login_form.clear(login=True)
                    login_form.clear(password=True)
                    continue
                elif error_code in (
                    3012,  # Invalid authy token
                    3023,  # Invalid email code
                ):
                    logger.info("3012/23: Login failed due to incorrect 2FA code")
                    if error_code == 3023:
                        token_kind = "email"
                        gui_print(_("login", "incorrect_email_code"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "incorrect_twofa_code"))
                    login_form.clear(token=True)
                    continue
                elif error_code in (
                    3011,  # Authy token needed
                    3022,  # Email code needed
                ):
                    # 2FA handling
                    logger.info("3011/22: 2FA token required")
                    # user didn't provide a token, so ask them for it
                    if error_code == 3022:
                        token_kind = "email"
                        gui_print(_("login", "email_code_required"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "twofa_code_required"))
                    continue
                elif error_code >= 5000:
                    # Special errors, usually from Twitch telling the user to "go away"
                    # We print the code out to inform the user, and just use chrome flow instead
                    # {
                    #     "error_code":5023,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client is not supported for this feature"
                    # }
                    # {
                    #     "error_code":5027,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client blocked from this operation"
                    # }
                    gui_print(_("login", "error_code").format(error_code=error_code))
                    logger.info("Login response: %s", redact_log_value(login_response))
                    raise CaptchaRequired()
                else:
                    ext_msg = str(redact_log_value(login_response))
                    logger.info("Login response: %s", ext_msg)
                    raise LoginException(ext_msg)
            # Success handling
            if "access_token" in login_response:
                access_token = login_response["access_token"]
                if not isinstance(access_token, str) or not access_token:
                    raise LoginException("Twitch login response omitted access_token")
                self.access_token = access_token
                logger.info("Access token granted")
                login_form.clear()
                break

        if hasattr(self, "access_token"):
            return self.access_token
        raise LoginException("Login flow finished without setting the access token")

    def headers(self, *, user_agent: str = '', gql: bool = False) -> JsonType:
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

    async def validate(self):
        async with self._lock:
            await self._validate()

    async def _validate_access_token(self, client_info: ClientInfo) -> bool:
        async with self._twitch.request(
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
            try:
                payload: JsonType = await response.json(loads=SAFE_LOADS)
                validated_user_id = int(payload["user_id"])
                validated_client_id = str(payload["client_id"])
            except (KeyError, TypeError, ValueError, aiohttp.ContentTypeError) as exc:
                raise RuntimeError("Token validation returned invalid data") from exc
            return (
                validated_client_id == client_info.CLIENT_ID
                and validated_user_id == self.user_id
            )

    async def _validate(self):
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        client_info: ClientInfo = self._twitch._client_type
        now = datetime.now(timezone.utc)
        if self._hasattrs("access_token", "user_id"):
            if (
                self._last_validated is not None
                and now - self._last_validated < AUTH_VALIDATION_INTERVAL
            ):
                self._logged_in.set()
                return
            if await self._validate_access_token(client_info):
                self._last_validated = now
                self._logged_in.set()
                return
            self.invalidate(delete_cookies=True)
        jar: aiohttp.CookieJar | None = None
        if (
            not self._hasattrs("device_id")
            or not self._hasattrs("access_token", "user_id")
        ):
            session = await self._twitch.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
        if not self._hasattrs("device_id"):
            if jar is None:
                raise RuntimeError("Authentication cookie jar is unavailable")
            async with self._twitch.request(
                "GET", client_info.CLIENT_URL, headers=self.headers()
            ) as response:
                page_html = await response.text("utf8")
                assert page_html is not None
            # doing the request ends up setting the "unique_id" value in the cookie
            cookie = jar.filter_cookies(client_info.CLIENT_URL)
            self.device_id = cookie["unique_id"].value
        if not self._hasattrs("access_token", "user_id"):
            if jar is None:
                raise RuntimeError("Authentication cookie jar is unavailable")
            # looks like we're missing something
            login_form: LoginForm = self._twitch.gui.login
            logger.info("Checking login")
            login_form.update(_("gui", "login", "logging_in"), None)
            for client_mismatch_attempt in range(2):
                for invalid_token_attempt in range(2):
                    cookie = jar.filter_cookies(client_info.CLIENT_URL)
                    if "auth-token" not in cookie:
                        refresh_token = self._oauth_tokens.load(client_info.CLIENT_ID)
                        if refresh_token is not None:
                            logger.info("Refreshing Twitch OAuth session")
                            refreshed_token = await self._refresh_access_token(
                                client_info, refresh_token
                            )
                        else:
                            refreshed_token = None
                        if refreshed_token is None:
                            if refresh_token is not None:
                                logger.info("Stored Twitch refresh token is invalid")
                                self._clear_refresh_token()
                            self.access_token = await self._oauth_login()
                        else:
                            self.access_token = refreshed_token
                        cookie["auth-token"] = self.access_token
                    elif not hasattr(self, "access_token"):
                        logger.info("Restoring session from cookie")
                        self.access_token = cookie["auth-token"].value
                    # validate the auth token, by obtaining user_id
                    async with self._twitch.request(
                        "GET",
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {self.access_token}"}
                    ) as response:
                        if response.status == 401:
                            # the access token we have is invalid - clear the cookie and reauth
                            logger.info("Restored session is invalid")
                            assert client_info.CLIENT_URL.host is not None
                            jar.clear_domain(client_info.CLIENT_URL.host)
                            continue
                        elif response.status == 200:
                            try:
                                validate_response = await response.json(loads=SAFE_LOADS)
                            except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError) as exc:
                                raise RuntimeError("Login validation returned invalid JSON") from exc
                            if not isinstance(validate_response, dict):
                                raise RuntimeError("Login validation returned malformed data")
                            break
                else:
                    raise RuntimeError("Login verification failure (step #2)")
                # ensure the cookie's client ID matches the currently selected client
                if validate_response.get("client_id") == client_info.CLIENT_ID:
                    break
                # otherwise, we need to delete the entire cookie file and clear the jar
                logger.info("Cookie client ID mismatch")
                jar.clear()
                remove_stale_new(COOKIES_PATH)
                self._clear_refresh_token()
            else:
                raise RuntimeError("Login verification failure (step #1)")
            try:
                self.user_id = int(validate_response["user_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Login verification returned an invalid user ID") from exc
            cookie["persistent"] = str(self.user_id)
            logger.info(f"Login successful, user ID: {self.user_id}")
            login_form.update(_("gui", "login", "logged_in"), self.user_id)
            # update our cookie and save it atomically
            jar.update_cookies(cookie, client_info.CLIENT_URL)
            self._twitch._save_cookie_jar(jar, COOKIES_PATH)
        self._twitch.gui.help._invalidate_button.config(state="normal")
        self._last_validated = datetime.now(timezone.utc)
        self._logged_in.set()


class Twitch:
    def __init__(
        self,
        settings: Settings,
        # Optional presentation backend for tests or alternate frontends.
        # The production default is the Qt presentation layer.
        gui_factory: Callable[["Twitch"], Any] | None = None,
    ):
        self.settings: Settings = settings
        # State management
        self._state: State = State.IDLE
        self._state_change = asyncio.Event()
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._mnt_triggers: deque[datetime] = deque()
        # NOTE: GQL is pretty volatile and breaks everything if one runs into their rate limit.
        # Do not modify the default, safe values.
        self._qgl_limiter = RateLimiter(capacity=5, window=1)
        # Client type, session and auth
        self._client_type: ClientInfo = ClientType.ANDROID_APP
        self._rate_limit_remaining: int | None = None
        self._rate_limit_reset: datetime | None = None
        self._session: aiohttp.ClientSession | None = None
        self._auth_state: _AuthState = _AuthState(self)
        # GUI; import the default presentation only when it is actually used.
        # Keeping this dependency lazy prevents the backend and Qt packages from
        # forming an import cycle and lets backend tools use custom GUI seams.
        if gui_factory is None:
            from gui_qt import QtGUIManager

            gui_factory = QtGUIManager
        self.gui: Any = gui_factory(self)
        # Storing and watching channels
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_channels: OrderedDict[int, Channel] = OrderedDict()
        self._watch_drop_ids: dict[int, str] = {}
        self._watch_tasks: dict[int, asyncio.Task[None]] = {}
        self._watch_restart_events: dict[int, asyncio.Event] = {}
        self._watch_claim_cooldowns: dict[str, float] = {}
        self._watch_completed_drop_ids: set[str] = set()
        self._watch_channel_cooldowns: dict[int, float] = {}
        self._watch_resync_cooldowns: dict[str, float] = {}
        self._watch_generation = 0
        self._dual_watch_enabled = True
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        if (session := self._session) is not None:
            if session.closed:
                raise RuntimeError("Session is closed")
            return session
        # load in cookies
        cookie_jar = aiohttp.CookieJar()
        try:
            if COOKIES_PATH.exists():
                with suppress(OSError):
                    COOKIES_PATH.chmod(0o600)
                cookie_jar.load(COOKIES_PATH)
        except Exception:
            # if loading in the cookies file ends up in an error, just ignore it
            # clear the jar, just in case
            cookie_jar.clear()
        # create timeouts
        # connection quality mulitiplier determines the magnitude of timeouts
        connection_quality = self.settings.connection_quality
        if connection_quality < 1:
            connection_quality = self.settings.connection_quality = 1
        elif connection_quality > 6:
            connection_quality = self.settings.connection_quality = 6
        timeout = aiohttp.ClientTimeout(
            sock_connect=5*connection_quality,
            total=10*connection_quality,
        )
        # create session, limited to 50 connections at maximum
        connector = aiohttp.TCPConnector(limit=50)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            cookie_jar=cookie_jar,
            headers={"User-Agent": self._client_type.USER_AGENT},
        )
        return self._session

    @staticmethod
    def _save_cookie_jar(cookie_jar: aiohttp.CookieJar, path: Path) -> None:
        """Persist cookies atomically without destroying the last good file."""
        try:
            atomic_write(path, cookie_jar.save)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Unable to persist cookies: %s", type(exc).__name__)

    async def shutdown(self) -> None:
        start_time = time()
        background_tasks: list[asyncio.Task[Any]] = list(self._watch_tasks.values())
        self.stop_watching()
        if self._mnt_task is not None:
            background_tasks.append(self._mnt_task)
            self._mnt_task = None
        pending_channel_tasks = [
            channel._pending_stream_up
            for channel in self.channels.values()
            if channel._pending_stream_up is not None
        ]
        for channel in self.channels.values():
            channel.remove()
        await cancel_tasks((*background_tasks, *pending_channel_tasks))
        # stop websocket, close session and save cookies
        await self.websocket.stop(clear_topics=True)
        if self._session is not None:
            cookie_jar = cast(aiohttp.CookieJar, self._session.cookie_jar)
            # clear empty cookie entries off the cookies file before saving
            # NOTE: Unfortunately, aiohttp provides no easy way of clearing empty cookies,
            # so we need to access the private '_cookies' attribute for this.
            for cookie_key, cookie in list(cookie_jar._cookies.items()):
                if not cookie:
                    del cookie_jar._cookies[cookie_key]
            self._save_cookie_jar(cookie_jar, COOKIES_PATH)
            await self._session.close()
            self._session = None
        self._drops.clear()
        self.channels.clear()
        self.inventory.clear()
        self._campaigns.clear()
        self._auth_state.clear()
        self.wanted_games.clear()
        self._mnt_triggers.clear()
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(start_time + 0.5 - time())

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        if self._state is not State.EXIT:
            # prevent state changing once we switch to exit state
            self._state = state
        self._state_change.set()

    def state_change(self, state: State) -> abc.Callable[[], None]:
        # this is identical to change_state, but defers the call
        # perfect for GUI usage
        return partial(self.change_state, state)

    def close(self):
        """
        Called when the application is requested to close by the user,
        usually by the console or application window being closed.
        """
        self.change_state(State.EXIT)

    def prevent_close(self):
        """
        Called when the application window has to be prevented from closing, even after the user
        closes it with X. Usually used solely to display tracebacks from the closing sequence.
        """
        self.gui.prevent_close()

    def print(self, message: str):
        """
        Can be used to print messages within the GUI.
        """
        self.gui.print(message)

    def save(self, *, force: bool = False) -> None:
        """
        Saves the application state.
        """
        self.gui.save(force=force)
        self.settings.save(force=force)

    def get_priority(self, channel: Channel) -> int:
        """
        Return a priority number for a given channel.

        0 has the highest priority.
        Higher numbers -> lower priority.
        MAX_INT (a really big number) signifies the lowest possible priority.
        """
        if (
            (game := channel.game) is None  # None when OFFLINE or no game set
            or game not in self.wanted_games  # we don't care about the played game
        ):
            return MAX_INT
        return self.wanted_games.index(game)

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        if (viewers := channel.viewers) is not None:
            return viewers
        return -1

    async def run(self):
        if self.settings.dump:
            with _open_dump("w"):
                # replace the existing file with an empty one
                pass
        while True:
            try:
                await self._run()
                break
            except ReloadRequest:
                await self.shutdown()
            except ExitRequest:
                break
            except aiohttp.ContentTypeError as exc:
                raise RequestException(_("login", "unexpected_content")) from exc

    async def _run(self):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        self.gui.start()
        # Re-enable the optional second slot after a full application reload;
        # a live reconciliation failure disables it for the current run.
        self._dual_watch_enabled = True
        auth_state = await self.get_auth()
        await self.websocket.start()
        # Watch tasks are created per channel when the first targets are selected.
        # Add default topics
        self.websocket.add_topics([
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic(
                "User", "Notifications", auth_state.user_id, self.process_notifications
            ),
        ])
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        while True:
            if self._state is State.IDLE:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.tray.change_icon("idle")
                self.gui.status.update(_("gui", "status", "idle"))
                self.stop_watching()
                # clear the flag and wait until it's set again
                self._state_change.clear()
            elif self._state is State.INVENTORY_FETCH:
                self.gui.tray.change_icon("maint")
                # Inventory replacement invalidates every old drop object and
                # assignment, so no watch task may run against the old indexes.
                self.stop_watching()
                # ensure the websocket is running
                await self.websocket.start()
                await self.fetch_inventory()
                self.gui.set_games(set(campaign.game for campaign in self.inventory))
                # Save state on every inventory fetch
                self.save()
                self.change_state(State.GAMES_UPDATE)
            elif self._state is State.GAMES_UPDATE:
                # claim drops from expired and active campaigns
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            if drop.can_claim and await drop.claim():
                                self._mark_watch_completed_drop(drop.id)
                # figure out which games we want
                self.wanted_games.clear()
                exclude = self.settings.exclude
                priority = self.settings.priority
                priority_mode = self.settings.priority_mode
                priority_only = priority_mode is PriorityMode.PRIORITY_ONLY
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                # sorted_campaigns: list[DropsCampaign] = list(self.inventory)
                sorted_campaigns: list[DropsCampaign] = self.inventory
                if not priority_only:
                    if priority_mode is PriorityMode.ENDING_SOONEST:
                        sorted_campaigns.sort(key=lambda c: c.ends_at)
                    elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
                        sorted_campaigns.sort(key=lambda c: c.availability)
                sorted_campaigns.sort(
                    key=lambda c: (
                        priority.index(c.game.name) if c.game.name in priority else MAX_INT
                    )
                )
                for campaign in sorted_campaigns:
                    game: Game = campaign.game
                    if (
                        game not in self.wanted_games  # isn't already there
                        # and isn't excluded by list or priority mode
                        and game.name not in exclude
                        and (not priority_only or game.name in priority)
                        # and can be progressed within the next hour
                        and campaign.can_earn_within(next_hour)
                    ):
                        # non-excluded games with no priority are placed last, below priority ones
                        self.wanted_games.append(game)
                full_cleanup = True
                self.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                self.gui.status.update(_("gui", "status", "cleanup"))
                if not self.wanted_games or full_cleanup:
                    # no games selected or we're doing full cleanup: remove everything
                    to_remove_channels: list[Channel] = list(channels.values())
                else:
                    # remove all channels that:
                    to_remove_channels = [
                        channel
                        for channel in channels.values()
                        if (
                            not channel.acl_based  # aren't ACL-based
                            and (
                                channel.offline  # and are offline
                                # or online but aren't streaming the game we want anymore
                                or (channel.game is None or channel.game not in self.wanted_games)
                            )
                        )
                    ]
                full_cleanup = False
                if to_remove_channels:
                    to_remove_topics: list[str] = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    for channel in to_remove_channels:
                        del channels[channel.id]
                        channel.remove()
                    del to_remove_channels, to_remove_topics
                if self.wanted_games:
                    self.change_state(State.CHANNELS_FETCH)
                else:
                    # with no games available, we switch to IDLE after cleanup
                    self.print(_("status", "no_campaign"))
                    self.change_state(State.IDLE)
            elif self._state is State.CHANNELS_FETCH:
                # Channel objects are replaced below; cancel watch tasks before
                # clearing the channel map so an old task cannot race a relink.
                self.stop_watching()
                self.gui.status.update(_("gui", "status", "gathering"))
                # start with all current channels, clear the memory and GUI
                new_channels: set[Channel] = set(channels.values())
                channels.clear()
                self.gui.channels.clear()
                # gather and add ACL channels from campaigns
                # NOTE: we consider only campaigns that can be progressed
                # NOTE: we use another set so that we can set them online separately
                no_acl: set[Game] = set()
                acl_channels: set[Channel] = set()
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                for campaign in self.inventory:
                    if (
                        campaign.game in self.wanted_games
                        and campaign.can_earn_within(next_hour)
                    ):
                        if campaign.allowed_channels:
                            acl_channels.update(campaign.allowed_channels)
                        else:
                            no_acl.add(campaign.game)
                # remove all ACL channels that already exist from the other set
                acl_channels.difference_update(new_channels)
                # use the other set to set them online if possible
                await self.bulk_check_online(acl_channels)
                # finally, add them as new channels
                new_channels.update(acl_channels)
                for game in no_acl:
                    # for every campaign without an ACL, for it's game,
                    # add a list of live channels with drops enabled
                    new_channels.update(await self.get_live_streams(game, drops_enabled=True))
                # sort them descending by viewers, by priority and by game priority
                # NOTE: Viewers sort also ensures ONLINE channels are sorted to the top
                # NOTE: We can drop using the set now, because there's no more channels being added
                ordered_channels: list[Channel] = sorted(
                    new_channels, key=self._viewers_key, reverse=True
                )
                ordered_channels.sort(key=lambda ch: ch.acl_based, reverse=True)
                ordered_channels.sort(key=self.get_priority)
                # ensure that we won't end up with more channels than we can handle
                # NOTE: we trim from the end because that's where the non-priority,
                # offline (or online but low viewers) channels end up
                to_remove_channels = ordered_channels[MAX_CHANNELS:]
                ordered_channels = ordered_channels[:MAX_CHANNELS]
                if to_remove_channels:
                    # tracked channels and gui were cleared earlier, so no need to do it here
                    # just make sure to unsubscribe from their topics
                    to_remove_topics = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    del to_remove_channels, to_remove_topics
                # set our new channel list
                for channel in ordered_channels:
                    channels[channel.id] = channel
                    channel.display(add=True)
                # subscribe to these channel's state updates
                to_add_topics: list[WebsocketTopic] = []
                for channel_id in channels:
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamState", channel_id, self.process_stream_state
                        )
                    )
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamUpdate", channel_id, self.process_stream_update
                        )
                    )
                self.websocket.add_topics(to_add_topics)
                # relink watching channel after cleanup,
                # or stop watching it if it no longer qualifies
                # NOTE: this replaces 'self.watching_channel's internal value with the new object
                watching_channel = self.watching_channel.get_with_default(None)
                if watching_channel is not None:
                    new_watching: Channel | None = channels.get(watching_channel.id)
                    if new_watching is not None and self.can_watch(new_watching):
                        self.watch(new_watching, update_status=False)
                    else:
                        # we've removed a channel we were watching
                        self.stop_watching()
                    del new_watching
                # pre-display the active drop with a substracted minute
                for channel in channels.values():
                    # check if there's any channels we can watch first
                    if self.can_watch(channel):
                        if (
                            (active_campaign := self.get_active_campaign(channel)) is not None
                            and (active_drop := active_campaign.first_drop) is not None
                        ):
                            active_drop.display(countdown=False, subone=True)
                        break
                self.change_state(State.CHANNEL_SWITCH)
                del (
                    no_acl,
                    acl_channels,
                    new_channels,
                    to_add_topics,
                    ordered_channels,
                    watching_channel,
                )
            elif self._state is State.CHANNEL_SWITCH:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.status.update(_("gui", "status", "switching"))
                # Change into the selected channel, stay in the watching channel,
                # or select a new channel that meets the required conditions
                new_watching = None
                selected_channel = self.gui.channels.get_selection()
                if selected_channel is not None and self.can_watch(selected_channel):
                    # selected channel is checked first, and set as long as we can watch it
                    new_watching = selected_channel
                else:
                    # other channels additionally need to have a good reason
                    # for a switch (including the watching one)
                    # NOTE: we need to sort the channels every time because one channel
                    # can end up streaming any game - channels aren't game-tied
                    for channel in sorted(channels.values(), key=self.get_priority):
                        if self.should_switch(channel):
                            new_watching = channel
                            break
                watching_channel = self.watching_channel.get_with_default(None)
                if new_watching is not None:
                    # if we have a better switch target - do so
                    self.watch(new_watching)
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                elif watching_channel is not None and self.can_watch(watching_channel):
                    # otherwise, continue watching what we had before and refill
                    # the second distinct target if one is available.
                    self.watch(watching_channel, update_status=False)
                    self.gui.status.update(
                        _("status", "watching").format(channel=watching_channel.name)
                    )
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                else:
                    # not watching anything and there isn't anything to watch either
                    self.print(_("status", "no_channel"))
                    self.change_state(State.IDLE)
                del new_watching, selected_channel, watching_channel
            elif self._state is State.RESTART:
                raise ReloadRequest()
            elif self._state is State.EXIT:
                self.gui.tray.change_icon("pickaxe")
                self.gui.status.update(_("gui", "status", "exiting"))
                # we've been requested to exit the application
                break
            await self._state_change.wait()

    async def _watch_sleep(self, event: asyncio.Event, delay: float) -> bool:
        # Each watched channel owns an event so a restart wakes every watch loop.
        interrupted = False
        try:
            await asyncio.wait_for(event.wait(), timeout=max(delay, 0))
        except asyncio.TimeoutError:
            pass
        else:
            interrupted = True
        event.clear()
        return interrupted

    def _display_primary_drop(self, drop: TimedDrop) -> None:
        primary = self.watching_channel.get_with_default(None)
        if primary is not None and self._watch_drop_ids.get(primary.id) == drop.id:
            drop.display()

    def _mark_watch_completed_drop(self, drop_id: str) -> None:
        completed_drop_ids = getattr(self, "_watch_completed_drop_ids", None)
        if completed_drop_ids is None:
            completed_drop_ids = set()
            self._watch_completed_drop_ids = completed_drop_ids
        completed_drop_ids.add(drop_id)

    def _request_watch_resync(self, key: str, seconds: float = 300) -> bool:
        resync_cooldowns = getattr(self, "_watch_resync_cooldowns", None)
        if resync_cooldowns is None:
            resync_cooldowns = {}
            self._watch_resync_cooldowns = resync_cooldowns
        now = time()
        if resync_cooldowns.get(key, 0) > now:
            return False
        resync_cooldowns[key] = now + seconds
        self.change_state(State.INVENTORY_FETCH)
        return True

    def _disable_dual_watch_if_secondary(self, channel: Channel) -> None:
        primary = self.watching_channel.get_with_default(None)
        if (
            primary is not None
            and primary.id != channel.id
            and len(self._watching_channels) > 1
            and self._dual_watch_enabled
        ):
            self._dual_watch_enabled = False
            logger.warning(
                "Disabling the second watch target after unscoped progress for %s",
                channel.name,
            )

    async def _reconcile_watch_progress(self, channel: Channel) -> None:
        """Refresh assigned progress from Twitch's authoritative viewer session."""
        if self._watch_drop_ids.get(channel.id) is None:
            return
        try:
            context = await self.gql_request(
                GQL_QUERIES["CurrentDrop"].with_variables(
                    {"channelID": str(channel.id)}
                )
            )
            drop_data: JsonType | None = (
                context["data"]["currentUser"]["dropCurrentSession"]
            )
        except (GQLException, RequestException, KeyError, TypeError):
            logger.warning("Unable to reconcile drop progress for %s", channel.name)
            return
        if not isinstance(drop_data, dict):
            logger.log(CALL, "Twitch reported no current drop for %s", channel.name)
            return
        drop_id = drop_data.get("dropID")
        if not isinstance(drop_id, str):
            logger.warning("Twitch returned an invalid current drop for %s", channel.name)
            return

        reported_channel_id: int | None = None
        reported_channel = drop_data.get("channel")
        if isinstance(reported_channel, dict):
            try:
                raw_channel_id = reported_channel.get("id")
                reported_channel_id = (
                    int(raw_channel_id) if raw_channel_id is not None else None
                )
            except (TypeError, ValueError):
                logger.warning("Twitch returned an invalid channel for %s", channel.name)
                return

        # DropCurrentSessionContext is account-scoped in practice: Twitch can return
        # a stale session for a different channel even when channelID was supplied.
        # The Drop ID is the safest discriminator, followed by the reported channel.
        # A mismatch is therefore advisory and must not restart the productive target.
        if drop_id in getattr(self, "_watch_completed_drop_ids", set()):
            stale_channel: Channel | None = channel
            if reported_channel_id is not None:
                stale_channel = self._watching_channels.get(reported_channel_id)
                if stale_channel is None:
                    logger.log(
                        CALL,
                        "Ignoring stale completed drop %s from channel %s",
                        drop_id,
                        reported_channel_id,
                    )
                    return
            logger.info(
                "Twitch still reports previously completed drop %s for %s; skipping claim",
                drop_id,
                stale_channel.name,
            )
            self._disable_dual_watch_if_secondary(stale_channel)
            self._block_watch_channel(stale_channel.id)
            self.change_state(State.CHANNEL_SWITCH)
            return

        target = channel
        if reported_channel_id is not None and reported_channel_id != channel.id:
            assigned_owner = next(
                (
                    candidate
                    for candidate, assigned in self._watch_drop_ids.items()
                    if assigned == drop_id and candidate in self._watching_channels
                ),
                None,
            )
            if assigned_owner is not None:
                target = self._watching_channels[assigned_owner]
                logger.log(
                    CALL,
                    "Routing current drop %s from %s to assigned channel %s",
                    drop_id,
                    channel.name,
                    target.name,
                )
            elif reported_channel_id in self._watching_channels:
                target = self._watching_channels[reported_channel_id]
                logger.log(
                    CALL,
                    "Routing current drop response from %s to reported channel %s",
                    channel.name,
                    target.name,
                )
            else:
                logger.warning(
                    "Ignoring stale current-drop session for %s: %s",
                    channel.name,
                    drop_id,
                )
                return

        assigned_drop_id = self._watch_drop_ids.get(target.id)
        if assigned_drop_id is None:
            return
        try:
            current_minutes = int(drop_data["currentMinutesWatched"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Twitch returned invalid progress for %s", target.name)
            return
        gql_drop = self._drops.get(drop_id)
        if gql_drop is None:
            if self._request_watch_resync(f"unknown-current-drop:{drop_id}"):
                logger.warning(
                    "Twitch reported an unknown current drop for %s: %s",
                    target.name,
                    drop_id,
                )
            self._disable_dual_watch_if_secondary(target)
            self._block_watch_channel(target.id)
            return
        if drop_id != assigned_drop_id:
            assigned_elsewhere = any(
                other_id != target.id and assigned == drop_id
                for other_id, assigned in self._watch_drop_ids.items()
            )
            if assigned_elsewhere:
                logger.log(
                    CALL,
                    "Ignoring duplicate current drop %s already assigned elsewhere",
                    drop_id,
                )
                return
            if not gql_drop.can_earn(target):
                if self._request_watch_resync(f"ineligible-current-drop:{drop_id}"):
                    logger.warning(
                        "Twitch current drop %s is not locally eligible on %s",
                        drop_id,
                        target.name,
                    )
                self._disable_dual_watch_if_secondary(target)
                self._block_watch_channel(target.id)
                return
            self._watch_drop_ids[target.id] = drop_id
            restart_event = self._watch_restart_events.get(target.id)
            if restart_event is not None:
                restart_event.set()
            logger.info(
                "Reconciled watch assignment for %s: %s -> %s",
                target.name,
                assigned_drop_id,
                drop_id,
            )
        if gql_drop.is_claimed:
            logger.info("Twitch reported an already-claimed drop for %s", target.name)
            self._mark_watch_completed_drop(drop_id)
            self._disable_dual_watch_if_secondary(target)
            self._block_watch_channel(target.id)
            self._request_watch_resync(f"claimed-current-drop:{drop_id}")
            return
        if not gql_drop.can_earn(target):
            if self._request_watch_resync(f"lost-eligibility:{drop_id}"):
                logger.warning(
                    "Current drop %s is no longer locally eligible on %s",
                    drop_id,
                    target.name,
                )
            self._disable_dual_watch_if_secondary(target)
            self._block_watch_channel(target.id)
            return
        if current_minutes >= gql_drop.required_minutes > 0:
            try:
                await gql_drop.generate_claim()
                claimed = await gql_drop.claim()
            except (GQLException, RequestException):
                claimed = False
            self._block_watch_channel(target.id)
            if claimed:
                self._mark_watch_completed_drop(drop_id)
                self._watch_claim_cooldowns.pop(drop_id, None)
                logger.info("Claimed completed current drop %s", drop_id)
            else:
                # Do not let the normal inventory claim pass immediately retry
                # the same synthetic claim ID; retry it only after a cooldown.
                gql_drop.claim_id = None
                self._watch_claim_cooldowns[drop_id] = time() + 300
                logger.warning("Could not claim completed current drop %s", drop_id)
            self._request_watch_resync(f"completed-current-drop:{drop_id}")
            return
        previous_minutes = gql_drop.current_minutes
        gql_drop.update_minutes(current_minutes)
        self._display_primary_drop(gql_drop)
        if gql_drop.current_minutes > previous_minutes:
            logger.log(
                CALL,
                "Drop progress from GQL: %s (%s, %s/%s) on %s",
                gql_drop.name,
                gql_drop.campaign.game,
                gql_drop.current_minutes,
                gql_drop.required_minutes,
                target.name,
            )

    async def _watch_channel_loop(
        self, channel: Channel, restart_event: asyncio.Event, generation: int
    ) -> None:
        interval = WATCH_INTERVAL.total_seconds()
        try:
            while (
                generation == getattr(self, "_watch_generation", 0)
                and self._watching_channels.get(channel.id) is channel
                and channel.id in self._watch_drop_ids
            ):
                if not channel.online or not self.can_watch(channel):
                    self.change_state(State.CHANNEL_SWITCH)
                    return
                succeeded = await channel.send_watch()
                last_sent = time()
                if not succeeded:
                    logger.log(CALL, "Watch request failed for channel: %s", channel.name)
                if await self._watch_sleep(restart_event, 20):
                    continue
                primary = self.watching_channel.get_with_default(None)
                if channel is not primary or self.gui.progress.minute_almost_done():
                    await self._reconcile_watch_progress(channel)
                await self._watch_sleep(
                    restart_event, interval - min(time() - last_sent, interval)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Watch loop failed for channel %s", channel.name)
            self.change_state(State.CHANNEL_SWITCH)

    def _watch_task_done(self, channel_id: int, task: asyncio.Task[None]) -> None:
        if self._watch_tasks.get(channel_id) is task:
            del self._watch_tasks[channel_id]
        if not task.cancelled() and task.exception() is not None:
            logger.error("Watch task failed for channel %s", channel_id)

    def _release_watch_channel(self, channel_id: int, blocked_until: float) -> None:
        channel_cooldowns = getattr(self, "_watch_channel_cooldowns", {})
        if channel_cooldowns.get(channel_id) != blocked_until:
            return
        remaining = blocked_until - time()
        if remaining > 0:
            try:
                asyncio.get_running_loop().call_later(
                    remaining, self._release_watch_channel, channel_id, blocked_until
                )
            except RuntimeError:
                return
            return
        del channel_cooldowns[channel_id]
        self.change_state(State.CHANNEL_SWITCH)

    def _block_watch_channel(self, channel_id: int, seconds: float = 300) -> None:
        channel_cooldowns = getattr(self, "_watch_channel_cooldowns", None)
        if channel_cooldowns is None:
            channel_cooldowns = {}
            self._watch_channel_cooldowns = channel_cooldowns
        blocked_until = max(
            channel_cooldowns.get(channel_id, 0), time() + seconds
        )
        channel_cooldowns[channel_id] = blocked_until
        try:
            asyncio.get_running_loop().call_later(
                max(0, blocked_until - time()),
                self._release_watch_channel,
                channel_id,
                blocked_until,
            )
        except RuntimeError:
            pass

    def _eligible_drops_for_channel(self, channel: Channel) -> list[TimedDrop]:
        candidates: list[TimedDrop] = []
        seen: set[str] = set()
        now = time()
        claim_cooldowns = getattr(self, "_watch_claim_cooldowns", {})
        completed_drop_ids = getattr(self, "_watch_completed_drop_ids", set())
        for campaign in self.inventory:
            if not campaign.can_earn(channel):
                continue
            for drop in campaign.drops:
                if drop.id in completed_drop_ids:
                    continue
                blocked_until = claim_cooldowns.get(drop.id)
                if blocked_until is not None:
                    if blocked_until > now:
                        continue
                    claim_cooldowns.pop(drop.id, None)
                if drop.id not in seen and drop.can_earn(channel):
                    candidates.append(drop)
                    seen.add(drop.id)
        candidates.sort(key=lambda drop: (drop.remaining_minutes, drop.ends_at))
        return candidates

    def _drop_for_channel(self, channel: Channel) -> TimedDrop | None:
        return next(iter(self._eligible_drops_for_channel(channel)), None)

    def _select_watch_assignments(
        self, preferred: Channel | None = None
    ) -> list[tuple[Channel, TimedDrop]]:
        """Select up to two assignments with a unique game and drop per target."""
        ordered = sorted(self.channels.values(), key=self._viewers_key, reverse=True)
        ordered.sort(key=lambda candidate: candidate.acl_based, reverse=True)
        ordered.sort(key=self.get_priority)
        if preferred is not None and preferred in ordered:
            ordered.remove(preferred)
            ordered.insert(0, preferred)
        now = time()
        channel_cooldowns = getattr(self, "_watch_channel_cooldowns", {})
        for candidate in ordered:
            blocked_until = channel_cooldowns.get(candidate.id)
            if blocked_until is not None and blocked_until <= now:
                del channel_cooldowns[candidate.id]
        options = [
            (candidate, self._eligible_drops_for_channel(candidate))
            for candidate in ordered
            if candidate.game is not None
            and self.can_watch(candidate)
            and channel_cooldowns.get(candidate.id, 0) <= now
        ]
        for first_index, (first_channel, first_drops) in enumerate(options):
            for first_drop in first_drops:
                first_assignment = (first_channel, first_drop)
                if MAX_WATCH_CHANNELS == 1 or not getattr(self, "_dual_watch_enabled", True):
                    return [first_assignment]
                for second_channel, second_drops in options[first_index + 1:]:
                    if second_channel.game == first_channel.game:
                        continue
                    for second_drop in second_drops:
                        if second_drop.id != first_drop.id:
                            return [first_assignment, (second_channel, second_drop)]
        if options:
            return [(options[0][0], options[0][1][0])]
        return []

    def _select_watch_channels(self, preferred: Channel | None = None) -> list[Channel]:
        return [channel for channel, _drop in self._select_watch_assignments(preferred)]

    def _apply_watch_assignments(
        self,
        assignments: list[tuple[Channel, TimedDrop]],
        *,
        update_status: bool = True,
    ) -> None:
        max_targets = (
            MAX_WATCH_CHANNELS if getattr(self, "_dual_watch_enabled", True) else 1
        )
        assignments = assignments[:max_targets]
        channels = [channel for channel, _drop in assignments]
        targets = OrderedDict((channel.id, channel) for channel in channels)
        target_drop_ids = {channel.id: drop.id for channel, drop in assignments}
        self._watch_generation = getattr(self, "_watch_generation", 0) + 1
        generation = self._watch_generation
        for event in self._watch_restart_events.values():
            event.set()
        for task in self._watch_tasks.values():
            task.cancel()
        self._watch_tasks.clear()
        self._watch_restart_events.clear()
        self._watching_channels = targets
        self._watch_drop_ids = target_drop_ids
        for channel in channels:
            event = asyncio.Event()
            self._watch_restart_events[channel.id] = event
            task = asyncio.create_task(
                self._watch_channel_loop(channel, event, generation)
            )
            self._watch_tasks[channel.id] = task
            task.add_done_callback(
                lambda completed, channel_id=channel.id: self._watch_task_done(
                    channel_id, completed
                )
            )
        primary = channels[0] if channels else None
        if primary is None:
            self._watch_drop_ids.clear()
            self.watching_channel.clear()
            self.gui.channels.clear_watching()
            return
        self.watching_channel.set(primary)
        set_watching_channels = getattr(self.gui.channels, "set_watching_channels", None)
        if set_watching_channels is not None:
            set_watching_channels(channels)
        else:
            self.gui.channels.set_watching(primary)
        if getattr(self.gui, "display_drop", None) is not None:
            assignments[0][1].display(countdown=False, subone=True)
        if update_status:
            status_text = _("status", "watching").format(channel=primary.name)
            self.print(status_text)
            self.gui.status.update(status_text)
        if len(assignments) > 1:
            logger.info(
                "Watching distinct drop targets: %s (%s) and %s (%s)",
                assignments[0][0].name,
                assignments[0][1].id,
                assignments[1][0].name,
                assignments[1][1].id,
            )

    @task_wrapper(critical=True)
    async def _maintenance_task(self) -> None:
        now = datetime.now(timezone.utc)
        next_period = now + timedelta(hours=1)
        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            next_trigger = next_period
            while self._mnt_triggers and self._mnt_triggers[0] <= next_trigger:
                next_trigger = self._mnt_triggers.popleft()
            trigger_type: str = "Reload" if next_trigger == next_period else "Cleanup"
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                )
            )
            await asyncio.sleep((next_trigger - now).total_seconds())
            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            if next_trigger != next_period:
                logger.log(CALL, "Maintenance task requests channels cleanup")
                self.change_state(State.CHANNELS_CLEANUP)
        # this triggers a restart of this task every (up to) 60 minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self.change_state(State.INVENTORY_FETCH)

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.
        """
        # exit early if stream is offline
        if not channel.online:
            return False
        for campaign in self.inventory:
            if (
                campaign.can_earn(channel)  # let the campaign do the "special games" check
                and (
                    # limit watching to the games the user wants
                    channel.game is not None
                    and channel.drops_enabled
                    and channel.game in self.wanted_games
                    # let the campaign ignore all channel-related checks
                    or campaign.game.is_special()
                )
            ):
                return True
        return False

    def should_switch(self, channel: Channel) -> bool:
        """Return whether a channel should enter the distinct watch set."""
        if not self.can_watch(channel) or channel.id in self._watching_channels:
            return False
        watching_channel = self.watching_channel.get_with_default(None)
        if watching_channel is None or not self.can_watch(watching_channel):
            return True
        selected = self._select_watch_channels(preferred=channel)
        if channel.id not in {candidate.id for candidate in selected}:
            return False
        if len(self._watching_channels) < MAX_WATCH_CHANNELS:
            return True
        current_worst = max(
            self._watching_channels.values(),
            key=lambda candidate: (self.get_priority(candidate), not candidate.acl_based),
        )
        return (
            self.get_priority(channel) < self.get_priority(current_worst)
            or (
                self.get_priority(channel) == self.get_priority(current_worst)
                and channel.acl_based > current_worst.acl_based
            )
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        self.gui.tray.change_icon("active")
        assignments = self._select_watch_assignments(preferred=channel)
        self._apply_watch_assignments(assignments, update_status=update_status)

    def stop_watching(self):
        self.gui.clear_drop()
        self._watch_generation = getattr(self, "_watch_generation", 0) + 1
        for event in self._watch_restart_events.values():
            event.set()
        for task in self._watch_tasks.values():
            task.cancel()
        self._watch_tasks.clear()
        self._watch_restart_events.clear()
        self._watching_channels.clear()
        self._watch_drop_ids.clear()
        self.watching_channel.clear()
        self.gui.channels.clear_watching()

    def restart_watching(self):
        self.gui.progress.stop_timer()
        for event in self._watch_restart_events.values():
            event.set()

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType):
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                try:
                    viewers = int(message["viewers"])
                except (KeyError, TypeError, ValueError):
                    logger.warning("Ignoring invalid viewer count for %s", channel.name)
                    return
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type == "commercial":
            # skip these
            pass
        else:
            logger.warning(f"Unknown stream state: {msg_type}")

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType):
        # message = {
        #     "channel_id": "12345678",
        #     "type": "broadcast_settings_update",
        #     "channel": "channel._login",
        #     "old_status": "Old title",
        #     "status": "New title",
        #     "old_game": "Old game name",
        #     "game": "New game name",
        #     "old_game_id": 123456,
        #     "game_id": 123456
        # }
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return
        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ''
        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")
        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'check_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online()

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ):
        """
        Called by a Channel when it's status is updated (ONLINE, OFFLINE, title/tags change).

        NOTE: 'stream_before' gets dealocated once this function finishes.
        """
        if stream_before is None:
            if stream_after is not None:
                # Channel going ONLINE
                if self.should_switch(channel):
                    # we can watch the channel, and we should
                    self.print(_("status", "goes_online").format(channel=channel.name))
                    self.watch(channel)
                else:
                    logger.info(f"{channel.name} goes ONLINE")
            else:
                # Channel was OFFLINE and stays that way
                logger.log(CALL, f"{channel.name} stays OFFLINE")
        else:
            is_watching = channel.id in self._watching_channels
            if is_watching:
                if not self.can_watch(channel):
                    if stream_after is None:
                        self.print(_("status", "goes_offline").format(channel=channel.name))
                    else:
                        logger.info(
                            f"{channel.name} status has been updated, switching... "
                            f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                            f"{stream_after.drops_enabled and '✔' or '❌'})"
                        )
                    self.change_state(State.CHANNEL_SWITCH)
            elif stream_after is None:
                logger.info(f"{channel.name} goes OFFLINE")
            else:
                logger.info(
                    f"{channel.name} status has been updated "
                    f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                    f"{stream_after.drops_enabled and '✔' or '❌'})"
                )
                if self.should_switch(channel):
                    self.watch(channel)
        channel.display()

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType):
        # Message examples:
        # {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
        # {"type": "drop-claim", data: {"drop_instance_id": ...}}
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        data = message.get("data")
        if not isinstance(data, dict):
            logger.warning("Ignoring a drop event without an object data payload")
            return
        drop_id = data.get("drop_id")
        if not isinstance(drop_id, str):
            logger.warning("Ignoring a drop event without a valid drop ID")
            return
        drop: TimedDrop | None = self._drops.get(drop_id)
        watching_channels = [
            channel
            for channel in self._watching_channels.values()
            if self._watch_drop_ids.get(channel.id) == drop_id
        ]
        if not watching_channels:
            if drop_id in getattr(self, "_watch_completed_drop_ids", set()):
                logger.log(CALL, "Ignoring an event for a previously completed drop: %s", drop_id)
                return
            candidates = (
                [
                    channel
                    for channel in self._watching_channels.values()
                    if drop.can_earn(channel)
                ]
                if drop is not None
                else []
            )
            if drop is not None and len(candidates) == 1:
                channel = candidates[0]
                previous_drop_id = self._watch_drop_ids.get(channel.id)
                self._watch_drop_ids[channel.id] = drop_id
                restart_event = self._watch_restart_events.get(channel.id)
                if restart_event is not None:
                    restart_event.set()
                watching_channels = [channel]
                logger.info(
                    "Adopted unassigned drop event for %s: %s -> %s",
                    channel.name,
                    previous_drop_id,
                    drop_id,
                )
            else:
                if self._request_watch_resync(f"unassigned-drop:{drop_id}"):
                    logger.warning("Ignoring an event for an unassigned drop: %s", drop_id)
                return
        if drop is None:
            logger.error("Received an event for an unknown drop: %s", drop_id)
            self.change_state(State.INVENTORY_FETCH)
            return
        if msg_type == "drop-claim":
            claim_id = data.get("drop_instance_id")
            if not isinstance(claim_id, str):
                logger.warning("Ignoring a drop claim without a valid instance ID")
                return
            drop.update_claim(claim_id)
            campaign = drop.campaign
            claimed = await drop.claim()
            if claimed:
                self._mark_watch_completed_drop(drop.id)
            self._display_primary_drop(drop)

            async def wait_for_next_drop(channel: Channel) -> None:
                # About 4-20s after claiming, Twitch starts the next drop after
                # another watch payload. Check each assigned channel independently.
                for _attempt in range(8):
                    try:
                        context = await self.gql_request(
                            GQL_QUERIES["CurrentDrop"].with_variables(
                                {"channelID": str(channel.id)}
                            )
                        )
                        current_data: JsonType | None = (
                            context["data"]["currentUser"]["dropCurrentSession"]
                        )
                    except (GQLException, RequestException, KeyError, TypeError):
                        return
                    if (
                        not isinstance(current_data, dict)
                        or current_data.get("dropID") != drop.id
                    ):
                        return
                    await asyncio.sleep(2)

            await asyncio.sleep(4)
            await asyncio.gather(*(wait_for_next_drop(channel) for channel in watching_channels))
            if claimed and any(self.can_watch(channel) for channel in self._watching_channels.values()):
                primary = self.watching_channel.get_with_default(None)
                if primary is not None:
                    self.watch(primary, update_status=False)
                    self.restart_watching()
                    return
            elif not claimed and any(campaign.can_earn(channel) for channel in watching_channels):
                self.restart_watching()
                return
            self.change_state(State.INVENTORY_FETCH)
            return
        assert msg_type == "drop-progress"
        current_progress = data.get("current_progress_min")
        required_progress = data.get("required_progress_min")
        try:
            current_progress_int = int(cast(Any, current_progress))
            required_progress_int = int(cast(Any, required_progress))
        except (TypeError, ValueError):
            logger.warning("Ignoring a drop event with invalid progress: %s", drop_id)
            return
        logger.log(
            CALL,
            "Drop update from websocket: %s (%s/%s)",
            drop.name,
            current_progress_int,
            required_progress_int,
        )
        # PubSub does not include a channel ID; the assigned drop ID is the
        # authoritative discriminator when two channels are being farmed.
        drop.update_minutes(current_progress_int)
        self._display_primary_drop(drop)

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType):
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] in (
                "user_drop_reward_reminder_notification",  # drop confirmation
                "quests_viewer_reward_campaign_earned_emote",  # emote confirmation
                # badge confirmation?
            ):
                self.change_state(State.INVENTORY_FETCH)
                try:
                    await self.gql_request(
                        GQL_QUERIES["NotificationsDelete"].with_variables(
                            {"input": {"id": data["id"]}}
                        )
                    )
                except (GQLException, RequestException):
                    # Notifications can disappear or the delete request can fail
                    # after the inventory refresh; the next event can retry it.
                    logger.debug("Unable to delete Twitch notification")

    async def get_auth(self) -> _AuthState:
        await self._auth_state.validate()
        return self._auth_state

    def _record_rate_limit(self, response: aiohttp.ClientResponse) -> None:
        remaining = response.headers.get("Ratelimit-Remaining")
        reset = response.headers.get("Ratelimit-Reset")
        try:
            self._rate_limit_remaining = int(remaining) if remaining is not None else None
        except ValueError:
            self._rate_limit_remaining = None
        try:
            reset_timestamp = int(reset) if reset is not None else None
        except ValueError:
            reset_timestamp = None
        self._rate_limit_reset = (
            datetime.fromtimestamp(reset_timestamp, timezone.utc)
            if reset_timestamp is not None
            else None
        )
        if remaining is not None or reset is not None:
            logger.debug(
                "Twitch rate limit: remaining=%s reset=%s",
                self._rate_limit_remaining,
                self._rate_limit_reset.isoformat() if self._rate_limit_reset else None,
            )

    @staticmethod
    def _response_retry_delay(response: aiohttp.ClientResponse, fallback: float) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                except (TypeError, ValueError, OverflowError):
                    pass
                else:
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return max(1.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        reset = response.headers.get("Ratelimit-Reset")
        if reset is not None:
            try:
                return max(1.0, int(reset) - time())
            except (TypeError, ValueError, OverflowError):
                pass
        return max(1.0, fallback)

    @asynccontextmanager
    async def request(
        self, method: str, url: URL | str, *, invalidate_after: datetime | None = None, **kwargs
    ) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self.get_session()
        method = method.upper()
        if self.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.settings.proxy
        logger.debug(
            "Request: method=%s url=%s kwargs=%s",
            method,
            redact_log_value(url, key="url"),
            redact_log_value(kwargs),
        )
        session_timeout = timedelta(seconds=session.timeout.total or 0)
        backoff = ExponentialBackoff(maximum=3*60)
        for delay in backoff:
            if self.gui.close_requested:
                raise ExitRequest()
            elif (
                invalidate_after is not None
                # account for the expiration landing during the request
                and datetime.now(timezone.utc) >= (invalidate_after - session_timeout)
            ):
                raise RequestInvalid()
            response: aiohttp.ClientResponse | None = None
            sleep_delay = delay
            try:
                response = await self.gui.coro_unless_closed(
                    session.request(method, url, **kwargs)
                )
                if response is None:
                    raise RuntimeError("HTTP request returned no response")
                self._record_rate_limit(response)
                logger.debug(
                    "Response: status=%s url=%s",
                    response.status,
                    redact_log_value(response.url, key="url"),
                )
                if response.status < 500 and response.status not in (408, 425, 429):
                    # Pre-read the response to avoid getting errors outside of the
                    # context manager. aiohttp keeps the bytes available to json().
                    await response.read()
                    yield response
                    return
                await response.read()
                if response.status == 429:
                    sleep_delay = self._response_retry_delay(response, delay)
                    logger.warning(
                        "Twitch rate limit response; retrying in %.1fs (%s)",
                        sleep_delay,
                        redact_log_value(response.url, key="url"),
                    )
                else:
                    self.print(_("error", "site_down").format(seconds=round(delay)))
            except aiohttp.ClientConnectorCertificateError as exc:
                # for a case where SSL verification fails
                raise
            except (
                aiohttp.ClientConnectionError, asyncio.TimeoutError, aiohttp.ClientPayloadError
            ):
                # connection problems, retry
                if backoff.steps > 1:
                    # just so that quick retries that sometimes happen, aren't shown
                    self.print(
                        _("error", "no_connection").format(
                            seconds=round(delay),
                            url=redact_log_value(url, key="url"),
                        )
                    )
            finally:
                if response is not None:
                    response.release()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.gui.wait_until_closed(), timeout=sleep_delay)

    @overload
    async def gql_request(self, ops: GQLOperation) -> JsonType:
        ...

    @overload
    async def gql_request(self, ops: list[GQLOperation]) -> list[JsonType]:
        ...

    async def gql_request(
        self, ops: GQLOperation | list[GQLOperation]
    ) -> JsonType | list[JsonType]:
        gql_logger.debug("GQL Request: %s", redact_log_value(ops))
        backoff = ExponentialBackoff(maximum=60)
        # Persisted queries occasionally return transient service errors. Retry
        # those once immediately, while continuing to back off on outages.
        single_retry = True
        for delay in backoff:
            async with self._qgl_limiter:
                auth_state = await self.get_auth()
                async with self.request(
                    "POST",
                    "https://gql.twitch.tv/gql",
                    json=ops,
                    headers=auth_state.headers(user_agent=self._client_type.USER_AGENT, gql=True),
                ) as response:
                    if response.status == 401:
                        self._auth_state.invalidate()
                        raise LoginException("Twitch rejected the GraphQL access token")
                    try:
                        response_json: Any = await response.json(loads=SAFE_LOADS)
                    except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError) as exc:
                        raise RequestException("Twitch GraphQL returned invalid JSON") from exc
                    if response.status >= 400:
                        raise GQLException(
                            f"GraphQL HTTP {response.status}: "
                            f"{redact_log_value(response_json)}"
                        )
            gql_logger.debug("GQL Response: %s", redact_log_value(response_json))
            orig_response = response_json
            if isinstance(response_json, list):
                if not all(isinstance(item, dict) for item in response_json):
                    raise RequestException("Twitch GraphQL returned an invalid response list")
                response_list: list[JsonType] = response_json
            elif isinstance(response_json, dict):
                response_list = [response_json]
            else:
                raise RequestException("Twitch GraphQL returned an invalid response")

            retry_messages = {
                "service timeout",
                "service unavailable",
                "context deadline exceeded",
            }
            force_retry = False
            permanent_errors: list[Any] = []
            for response_item in response_list:
                errors = response_item.get("errors")
                if errors is None:
                    if "error" in response_item:
                        raise GQLException(
                            f"{response_item['error']}: "
                            f"{response_item.get('message', 'GraphQL request failed')}"
                        )
                    continue
                if not isinstance(errors, list):
                    raise GQLException("GraphQL returned a malformed errors field")
                for error_dict in errors:
                    if not isinstance(error_dict, dict):
                        permanent_errors.append(error_dict)
                        continue
                    message = error_dict.get("message")
                    if message in ("service error", "PersistedQueryNotFound") and single_retry:
                        extensions = response_item.get("extensions")
                        operation = (
                            extensions.get("operationName", "unknown")
                            if isinstance(extensions, dict)
                            else "unknown"
                        )
                        logger.warning(
                            "Retrying GraphQL %s for operation %s",
                            message,
                            operation,
                        )
                        single_retry = False
                        force_retry = True
                        break
                    if message in retry_messages or message == "server error":
                        force_retry = True
                        break
                    permanent_errors.append(error_dict)
                if force_retry:
                    break
            if force_retry:
                await asyncio.sleep(max(delay, 5))
                continue
            if permanent_errors:
                raise GQLException(str(redact_log_value(permanent_errors)))
            return orig_response
        raise RuntimeError("GraphQL retry loop was broken")

    def _merge_lists(self, primary: list[Any], secondary: list[Any]) -> list[Any]:
        """Merge detail lists by ID without losing viewer progress from inventory."""
        if not primary:
            return secondary
        if not secondary:
            return primary
        if not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in primary):
            return primary
        if not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in secondary):
            return primary
        secondary_by_id = {item["id"]: item for item in secondary}
        merged: list[Any] = []
        for item in primary:
            detail = secondary_by_id.pop(item["id"], None)
            merged.append(self._merge_data(item, detail) if detail is not None else item)
        merged.extend(secondary_by_id.values())
        return merged

    def _merge_data(self, primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        merged: JsonType = {}
        for key in set(chain(primary_data.keys(), secondary_data.keys())):
            in_primary = key in primary_data
            if in_primary and key in secondary_data:
                vp = primary_data[key]
                vs = secondary_data[key]
                if vp is None:
                    merged[key] = vs
                elif vs is None:
                    merged[key] = vp
                elif type(vp) is not type(vs):
                    raise MinerException("Inconsistent merge data")
                elif isinstance(vp, dict):
                    merged[key] = self._merge_data(vp, vs)
                elif isinstance(vp, list):
                    merged[key] = self._merge_lists(vp, vs)
                else:
                    # Inventory is primary so its viewer-specific values win.
                    merged[key] = vp
            elif in_primary:
                merged[key] = primary_data[key]
            else:
                merged[key] = secondary_data[key]
        return merged

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self.get_auth()
        response_list: list[JsonType] = await self.gql_request(
            [
                GQL_QUERIES["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )
        fetched_data: dict[str, JsonType] = {}
        for response_json in response_list:
            try:
                campaign_data = response_json["data"]["user"]["dropCampaign"]
                campaign_id = campaign_data["id"]
            except (KeyError, TypeError):
                logger.warning("Campaign detail response did not contain a campaign")
                continue
            if (
                isinstance(campaign_data, dict)
                and isinstance(campaign_id, str)
                and campaign_id in campaign_ids
            ):
                fetched_data[campaign_id] = campaign_data
        return self._merge_data(campaign_ids, fetched_data)

    async def fetch_inventory(self) -> None:
        status_update = self.gui.status.update
        status_update(_("gui", "status", "fetching_inventory"))
        # fetch in-progress campaigns (inventory)
        response = await self.gql_request(GQL_QUERIES["Inventory"])
        try:
            inventory = response["data"]["currentUser"]["inventory"]
        except (KeyError, TypeError) as exc:
            raise RequestException("Twitch inventory response was malformed") from exc
        if not isinstance(inventory, dict):
            raise RequestException("Twitch inventory response was malformed")
        raw_ongoing = inventory.get("dropCampaignsInProgress") or []
        ongoing_campaigns = raw_ongoing if isinstance(raw_ongoing, list) else []
        # This contains claimed benefit edge IDs, not drop IDs.
        claimed_benefits: dict[str, datetime] = {}
        raw_game_events = inventory.get("gameEventDrops") or []
        if isinstance(raw_game_events, list):
            for benefit_data in raw_game_events:
                if not isinstance(benefit_data, dict):
                    continue
                benefit_id = benefit_data.get("id")
                awarded_at = benefit_data.get("lastAwardedAt")
                if isinstance(benefit_id, str) and isinstance(awarded_at, str):
                    try:
                        claimed_benefits[benefit_id] = timestamp(awarded_at)
                    except ValueError:
                        logger.warning("Ignoring malformed claimed benefit timestamp")
        inventory_data: dict[str, JsonType] = {
            campaign_data["id"]: campaign_data
            for campaign_data in ongoing_campaigns
            if isinstance(campaign_data, dict)
            and isinstance(campaign_data.get("id"), str)
        }
        # fetch general available campaigns data (campaigns)
        response = await self.gql_request(GQL_QUERIES["Campaigns"])
        try:
            raw_available = response["data"]["currentUser"]["dropCampaigns"]
        except (KeyError, TypeError) as exc:
            raise RequestException("Twitch campaign response was malformed") from exc
        available_list = raw_available if isinstance(raw_available, list) else []
        applicable_statuses = {"ACTIVE", "UPCOMING"}
        available_campaigns: dict[str, JsonType] = {}
        for campaign_data in available_list:
            if not isinstance(campaign_data, dict):
                continue
            campaign_id = campaign_data.get("id")
            status = campaign_data.get("status")
            if isinstance(campaign_id, str) and status in applicable_statuses:
                available_campaigns[campaign_id] = campaign_data
        # fetch detailed data for each campaign, in chunks
        status_update(_("gui", "status", "fetching_campaigns"))
        fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self.fetch_campaigns(campaigns_chunk))
            for campaigns_chunk in chunk(available_campaigns.items(), 20)
        ]
        try:
            for coro in asyncio.as_completed(fetch_campaigns_tasks):
                chunk_campaigns_data = await coro
                # merge the inventory and campaigns datas together
                inventory_data = self._merge_data(inventory_data, chunk_campaigns_data)
        finally:
            await cancel_tasks(fetch_campaigns_tasks)
        # filter out invalid campaigns
        for campaign_id in list(inventory_data.keys()):
            campaign_data = inventory_data[campaign_id]
            if not isinstance(campaign_data, dict) or campaign_data.get("game") is None:
                del inventory_data[campaign_id]

        if self.settings.dump:
            # dump the campaigns data to the dump file
            with _open_dump("a") as file:
                # we need to pre-process the inventory dump a little
                dump_data: JsonType = deepcopy(inventory_data)
                for campaign_data in dump_data.values():
                    if not isinstance(campaign_data, dict):
                        continue
                    # replace ACL lists with a simple text description
                    allow = campaign_data.get("allow")
                    if (
                        isinstance(allow, dict)
                        and allow.get("isEnabled", True)
                        and isinstance(allow.get("channels"), list)
                        and allow["channels"]
                    ):
                        # simply count the channels included in the ACL
                        allow["channels"] = f"{len(allow['channels'])} channels"
                    # replace drop instance IDs, so they don't include user IDs
                    drops = campaign_data.get("timeBasedDrops")
                    if not isinstance(drops, list):
                        continue
                    for drop_data in drops:
                        if not isinstance(drop_data, dict):
                            continue
                        self_data = drop_data.get("self")
                        if isinstance(self_data, dict) and self_data.get("dropInstanceID"):
                            self_data["dropInstanceID"] = "..."
                json.dump(dump_data, file, indent=4, sort_keys=True)
                file.write("\n\n")  # add 2x new line spacer
                json.dump(inventory["gameEventDrops"], file, indent=4, sort_keys=True, default=str)

        # Use the merged data to create campaign objects. A single malformed
        # campaign should not discard the rest of the viewer inventory.
        campaigns: list[DropsCampaign] = []
        for campaign_data in inventory_data.values():
            try:
                campaigns.append(DropsCampaign(self, campaign_data, claimed_benefits))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping malformed Twitch campaign: %s",
                    type(exc).__name__,
                )
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.eligible, reverse=True)

        self._drops.clear()
        self.gui.inv.clear()
        self.inventory.clear()
        self._campaigns.clear()
        self._mnt_triggers.clear()
        switch_triggers: set[datetime] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        # add the campaigns to the internal inventory
        for campaign in campaigns:
            self._drops.update({drop.id: drop for drop in campaign.drops})
            self._campaigns[campaign.id] = campaign
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
            self.inventory.append(campaign)
        now_timestamp = time()
        self._watch_claim_cooldowns = {
            drop_id: blocked_until
            for drop_id, blocked_until in getattr(self, "_watch_claim_cooldowns", {}).items()
            if blocked_until > now_timestamp
            and drop_id in self._drops
            and not self._drops[drop_id].is_claimed
        }
        # concurrently add the campaigns into the GUI
        # NOTE: this fetches pictures from the CDN, so might be slow without a cache
        status_update(
            _("gui", "status", "adding_campaigns").format(counter=f"(0/{len(campaigns)})")
        )
        add_campaign_tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(self.gui.inv.add_campaign(campaign))
            for campaign in campaigns
        ]
        try:
            for i, coro in enumerate(asyncio.as_completed(add_campaign_tasks), start=1):
                await coro
                status_update(
                    _("gui", "status", "adding_campaigns").format(
                        counter=f"({i}/{len(campaigns)})"
                    )
                )
                # this is needed here explicitly, because cache reads from disk don't raise this
                if self.gui.close_requested:
                    raise ExitRequest()
        finally:
            await cancel_tasks(add_campaign_tasks)
        self._mnt_triggers.extend(sorted(switch_triggers))
        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._mnt_triggers and self._mnt_triggers[0] <= now:
            self._mnt_triggers.popleft()
        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self._mnt_task is not None and not self._mnt_task.done():
            await cancel_tasks([self._mnt_task])
        self._mnt_task = asyncio.create_task(self._maintenance_task())

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        if not self.wanted_games:
            return None
        watching_channel = self.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None
        campaigns: list[DropsCampaign] = []
        for campaign in self.inventory:
            if campaign.can_earn(watching_channel):
                campaigns.append(campaign)
        if campaigns:
            campaigns.sort(key=lambda c: c.remaining_minutes)
            return campaigns[0]
        return None

    async def get_live_streams(
        self, game: Game, *, limit: int = 20, drops_enabled: bool = True
    ) -> list[Channel]:
        filters: list[str] = []
        if drops_enabled:
            filters.append("DROPS_ENABLED")
        try:
            response = await self.gql_request(
                GQL_QUERIES["GameDirectory"].with_variables({
                    "limit": limit,
                    "slug": game.slug,
                    "options": {
                        "includeRestricted": ["SUB_ONLY_LIVE"],
                        "systemFilters": filters,
                    },
                })
            )
        except GQLException as exc:
            raise MinerException(f"Game: {game.slug}") from exc
        data = response.get("data") if isinstance(response, dict) else None
        game_data = data.get("game") if isinstance(data, dict) else None
        streams = game_data.get("streams") if isinstance(game_data, dict) else None
        edges = streams.get("edges") if isinstance(streams, dict) else []
        if not isinstance(edges, list):
            return []
        channels: list[Channel] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict) or node.get("broadcaster") is None:
                continue
            try:
                channels.append(Channel.from_directory(self, node, drops_enabled=drops_enabled))
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed directory stream response")
        return channels

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        """
        Utilize batch GQL requests to check ONLINE status for a lot of channels at once.
        The optional available-drops check is applied to channels when enabled;
        otherwise directory filtering and campaign ACLs remain the source of truth.
        """
        channels = tuple(channels)
        acl_streams_map: dict[int, JsonType] = {}
        stream_gql_ops: list[GQLOperation] = [channel.stream_gql for channel in channels]
        if not stream_gql_ops:
            # shortcut for nothing to process
            # NOTE: Have to do this here, becase "channels" can be any iterable
            return
        stream_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
            asyncio.create_task(self.gql_request(stream_gql_chunk))
            for stream_gql_chunk in chunk(stream_gql_ops, 20)
        ]
        try:
            for coro in asyncio.as_completed(stream_gql_tasks):
                response_list: list[JsonType] = await coro
                for response_json in response_list:
                    try:
                        channel_data = response_json["data"]["user"]
                        channel_id = int(channel_data["id"]) if channel_data is not None else None
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Ignoring malformed stream lookup response")
                        continue
                    if isinstance(channel_data, dict) and channel_id is not None:
                        acl_streams_map[channel_id] = channel_data
        finally:
            await cancel_tasks(stream_gql_tasks)
        # for all channels with an active stream, check the available drops as well
        acl_available_drops_map: dict[int, list[JsonType]] = {}
        if self.settings.available_drops_check:
            available_gql_ops: list[GQLOperation] = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
                for channel_id, channel_data in acl_streams_map.items()
                if isinstance(channel_data.get("stream"), dict)  # only ONLINE channels
            ]
            available_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
                asyncio.create_task(self.gql_request(available_gql_chunk))
                for available_gql_chunk in chunk(available_gql_ops, 20)
            ]
            try:
                for coro in asyncio.as_completed(available_gql_tasks):
                    response_list = await coro
                    for response_json in response_list:
                        try:
                            available_info = response_json["data"]["channel"]
                            channel_id = int(available_info["id"])
                        except (KeyError, TypeError, ValueError):
                            logger.warning("Ignoring malformed available-drops response")
                            continue
                        if isinstance(available_info, dict):
                            campaigns = available_info.get("viewerDropCampaigns") or []
                            acl_available_drops_map[channel_id] = (
                                campaigns if isinstance(campaigns, list) else []
                            )
            finally:
                await cancel_tasks(available_gql_tasks)
        for channel in channels:
            channel_id = channel.id
            if channel_id not in acl_streams_map:
                continue
            channel_data = acl_streams_map[channel_id]
            if not isinstance(channel_data.get("stream"), dict):
                continue
            available_drops: list[JsonType] = acl_available_drops_map.get(channel_id, [])
            try:
                channel.external_update(channel_data, available_drops)
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed stream data for channel %s", channel_id)
