from __future__ import annotations

import asyncio
import json
import logging
from collections import abc
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from time import time
from typing import TYPE_CHECKING, Any, overload

import aiohttp
from yarl import URL

from exceptions import (
    ExitRequest,
    GQLException,
    LoginException,
    RequestException,
    RequestInvalid,
)
from constants import GQL_RETRY_ATTEMPTS, HTTP_RETRY_MAX_DELAY
from translate import _
from utils import ExponentialBackoff, RateLimiter, cancel_tasks, redact_log_value, safe_int

if TYPE_CHECKING:
    from constants import GQLOperation, JsonType
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")


async def read_json(
    response: aiohttp.ClientResponse,
    exception_type: type[Exception],
    message: str,
) -> Any:
    """Decode one complete JSON document and translate transport errors."""
    try:
        return await response.json(loads=json.loads)
    except (aiohttp.ContentTypeError, TypeError, UnicodeError, ValueError) as exc:
        raise exception_type(message) from exc


def retry_after_delay(response: aiohttp.ClientResponse, fallback: float) -> float:
    """Parse server hints into a finite, bounded retry delay in seconds."""
    def bounded(value: float) -> float:
        if not isfinite(value):
            value = fallback if isfinite(fallback) else 1.0
        return max(1.0, min(value, HTTP_RETRY_MAX_DELAY))

    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return bounded(float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, OverflowError):
                retry_at = None
            if retry_at is not None:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return bounded(
                    (retry_at - datetime.now(timezone.utc)).total_seconds()
                )
    reset = response.headers.get("Ratelimit-Reset")
    if reset is not None:
        try:
            reset_delay = int(reset) - time()
        except (TypeError, ValueError, OverflowError):
            reset_delay = None
        if reset_delay is not None:
            return bounded(reset_delay)
    return bounded(fallback)


class HttpTransport:
    """HTTP and GraphQL transport policy for one Twitch coordinator."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._gql_limiter = RateLimiter(capacity=5, window=1)
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: datetime | None = None

    async def wait_for_delay(
        self,
        delay: float,
        *,
        deadline: datetime | None = None,
    ) -> None:
        gui = self._twitch.gui
        if gui.close_requested:
            raise ExitRequest()
        wait_delay = max(0.0, delay)
        if deadline is not None:
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise RequestInvalid()
            wait_delay = min(wait_delay, remaining)

        close_task = asyncio.create_task(gui.wait_until_closed())
        try:
            done, _ = await asyncio.wait((close_task,), timeout=wait_delay)
        finally:
            await cancel_tasks((close_task,))
        if done or gui.close_requested:
            raise ExitRequest()
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            raise RequestInvalid()

    def _record_rate_limit(self, response: aiohttp.ClientResponse) -> None:
        remaining = response.headers.get("Ratelimit-Remaining")
        reset = response.headers.get("Ratelimit-Reset")
        self.rate_limit_remaining = safe_int(remaining)
        reset_timestamp = safe_int(reset)
        try:
            self.rate_limit_reset = (
                datetime.fromtimestamp(reset_timestamp, timezone.utc)
                if reset_timestamp is not None
                else None
            )
        except (OSError, OverflowError, ValueError):
            self.rate_limit_reset = None
        if remaining is not None or reset is not None:
            logger.debug(
                "Twitch rate limit: remaining=%s reset=%s",
                self.rate_limit_remaining,
                self.rate_limit_reset.isoformat()
                if self.rate_limit_reset
                else None,
            )

    @asynccontextmanager
    async def request(
        self,
        method: str,
        url: URL | str,
        *,
        invalidate_after: datetime | None = None,
        preload: bool = True,
        **kwargs: Any,
    ) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self._twitch.get_session()
        method = method.upper()
        if self._twitch.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self._twitch.settings.proxy
        logger.debug(
            "Request: method=%s url=%s kwargs=%s",
            method,
            redact_log_value(url, key="url"),
            redact_log_value(kwargs),
        )
        session_timeout = timedelta(seconds=session.timeout.total or 0)
        request_deadline = (
            invalidate_after - session_timeout
            if invalidate_after is not None
            else None
        )
        backoff = ExponentialBackoff(maximum=3 * 60)
        for delay in backoff:
            if self._twitch.gui.close_requested:
                raise ExitRequest()
            if (
                request_deadline is not None
                and datetime.now(timezone.utc) >= request_deadline
            ):
                raise RequestInvalid()
            response: aiohttp.ClientResponse | None = None
            sleep_delay = delay
            try:
                response = await self._twitch.gui.coro_unless_closed(
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
                if response.status < 500 and response.status not in (
                    408,
                    425,
                    429,
                ):
                    if preload:
                        await response.read()
                    yield response
                    return
                await response.read()
                if response.status == 429:
                    sleep_delay = retry_after_delay(response, delay)
                    logger.warning(
                        "Twitch rate limit response; retrying in %.1fs (%s)",
                        sleep_delay,
                        redact_log_value(response.url, key="url"),
                    )
                else:
                    self._twitch.print(
                        _("error", "site_down").format(
                            seconds=round(delay)
                        )
                    )
            except aiohttp.ClientConnectorCertificateError as exc:
                raise exc
            except (
                aiohttp.ClientConnectionError,
                asyncio.TimeoutError,
                aiohttp.ClientPayloadError,
            ):
                if backoff.steps > 1:
                    self._twitch.print(
                        _("error", "no_connection").format(
                            seconds=round(delay),
                            url=redact_log_value(url, key="url"),
                        )
                    )
            finally:
                if response is not None:
                    response.release()
            await self.wait_for_delay(
                sleep_delay,
                deadline=request_deadline,
            )

    @overload
    async def gql_request(self, ops: GQLOperation) -> JsonType:
        ...

    @overload
    async def gql_request(
        self,
        ops: list[GQLOperation],
    ) -> list[JsonType]:
        ...

    async def gql_request(
        self,
        ops: GQLOperation | list[GQLOperation],
    ) -> JsonType | list[JsonType]:
        gql_logger.debug("GQL Request: %s", redact_log_value(ops))
        backoff = ExponentialBackoff(maximum=60)
        single_retry = True
        auth_retry_available = True
        for _attempt in range(GQL_RETRY_ATTEMPTS):
            delay = next(backoff)
            async with self._gql_limiter:
                auth_state = await self._twitch.get_auth()
                auth_generation = auth_state.generation
                auth_headers = auth_state.headers(
                    user_agent=self._twitch._client_type.USER_AGENT,
                    gql=True,
                )
                async with self.request(
                    "POST",
                    "https://gql.twitch.tv/gql",
                    json=ops,
                    headers=auth_headers,
                ) as response:
                    if response.status == 401:
                        rejected_current = auth_state.invalidate_if_current(
                            auth_generation
                        )
                        if not auth_retry_available:
                            raise LoginException(
                                "Twitch rejected the GraphQL access token"
                            )
                        auth_retry_available = False
                        if rejected_current:
                            logger.warning(
                                "GraphQL access token rejected; reauthenticating"
                            )
                        else:
                            logger.info(
                                "Retrying GraphQL request with newer credentials"
                            )
                        continue
                    response_json: Any = await read_json(
                        response,
                        RequestException,
                        "Twitch GraphQL returned invalid JSON",
                    )
                    if response.status >= 400:
                        raise GQLException(
                            f"GraphQL HTTP {response.status}: "
                            f"{redact_log_value(response_json)}"
                        )
            gql_logger.debug(
                "GQL Response: %s",
                redact_log_value(response_json),
            )
            orig_response = response_json
            if isinstance(ops, list):
                if not ops:
                    raise RequestException("GraphQL operation batch cannot be empty")
                if not isinstance(response_json, list) or len(response_json) != len(ops):
                    raise RequestException(
                        "Twitch GraphQL response count did not match the request batch"
                    )
            elif not isinstance(response_json, dict):
                raise RequestException(
                    "Twitch GraphQL returned a batch for a single operation"
                )

            if isinstance(response_json, list):
                if not all(
                    isinstance(item, dict) for item in response_json
                ):
                    raise RequestException(
                        "Twitch GraphQL returned an invalid response list"
                    )
                response_list: list[JsonType] = response_json
            elif isinstance(response_json, dict):
                response_list = [response_json]
            else:
                raise RequestException(
                    "Twitch GraphQL returned an invalid response"
                )

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
                            "GraphQL request failed with a top-level error"
                        )
                    continue
                if not isinstance(errors, list):
                    raise GQLException(
                        "GraphQL returned a malformed errors field"
                    )
                for error_dict in errors:
                    if not isinstance(error_dict, dict):
                        permanent_errors.append(error_dict)
                        continue
                    message = error_dict.get("message")
                    if (
                        message in (
                            "service error",
                            "PersistedQueryNotFound",
                        )
                        and single_retry
                    ):
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
                await self.wait_for_delay(max(delay, 5))
                continue
            if permanent_errors:
                raise GQLException(
                    str(redact_log_value(permanent_errors))
                )
            return orig_response
        raise GQLException("GraphQL retry limit exceeded")
