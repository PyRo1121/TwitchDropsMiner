from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace
from typing import Any, Callable, cast

from constants import ClientType, GQLPersistedQuery, GQL_RETRY_ATTEMPTS
from exceptions import GQLException, LoginException, RequestException
from auth import AuthState
from http_transport import HttpTransport
from twitch import Twitch


class _Limiter:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Response:
    def __init__(
        self,
        status: int,
        payload: object,
        *,
        on_enter: Callable[[], None] | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self._on_enter = on_enter

    async def __aenter__(self) -> _Response:
        if self._on_enter is not None:
            self._on_enter()
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, **_kwargs: object) -> object:
        return self._payload


class _Button:
    def __init__(self) -> None:
        self.disabled = 0

    def config(self, *, state: str) -> None:
        if state == "disabled":
            self.disabled += 1


class GraphQLAuthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _miner(
        self, responses: list[_Response]
    ) -> tuple[HttpTransport, AuthState, _Button, list[str]]:
        miner = Twitch.__new__(Twitch)
        button = _Button()
        miner.gui = cast(
            Any,
            SimpleNamespace(
                set_authenticated=lambda authenticated: button.config(
                    state="normal" if authenticated else "disabled"
                ),
            ),
        )
        miner._session = None
        miner._client_type = ClientType.WEB
        transport = HttpTransport(miner)
        transport._gql_limiter = cast(Any, _Limiter())
        miner.transport = transport

        auth = AuthState(cast(Any, miner))
        auth.device_id = "device"
        auth.session_id = "session"
        auth.access_token = "old-token"
        auth.user_id = 42
        auth._last_validated = datetime.now(timezone.utc)
        miner._auth_state = auth

        sent_tokens: list[str] = []

        async def get_auth() -> AuthState:
            if not hasattr(auth, "access_token"):
                auth.access_token = "new-token"
                auth.user_id = 42
                auth._last_validated = datetime.now(timezone.utc)
            return auth

        def request(*_args: object, **kwargs: object) -> _Response:
            headers = cast(dict[str, str], kwargs["headers"])
            sent_tokens.append(headers["Authorization"])
            return responses.pop(0)

        miner.get_auth = get_auth  # type: ignore[method-assign]
        transport.request = request  # type: ignore[method-assign]
        return transport, auth, button, sent_tokens

    async def test_current_token_401_reauthenticates_and_retries_once(self) -> None:
        transport, auth, button, sent_tokens = self._miner(
            [
                _Response(401, {}),
                _Response(200, {"data": {"ok": True}}),
            ]
        )

        response = await transport.gql_request(
            GQLPersistedQuery("Example", "0" * 64)
        )

        self.assertEqual(response, {"data": {"ok": True}})
        self.assertEqual(sent_tokens, ["OAuth old-token", "OAuth new-token"])
        self.assertEqual(auth.access_token, "new-token")
        self.assertEqual(button.disabled, 1)

    async def test_stale_401_cannot_invalidate_newer_credentials(self) -> None:
        responses: list[_Response] = []
        transport, auth, button, sent_tokens = self._miner(responses)
        responses.extend(
            [
                _Response(
                    401,
                    {},
                    on_enter=lambda: setattr(auth, "access_token", "new-token"),
                ),
                _Response(200, {"data": {"ok": True}}),
            ]
        )

        response = await transport.gql_request(
            GQLPersistedQuery("Example", "0" * 64)
        )

        self.assertEqual(response, {"data": {"ok": True}})
        self.assertEqual(sent_tokens, ["OAuth old-token", "OAuth new-token"])
        self.assertEqual(auth.access_token, "new-token")
        self.assertEqual(button.disabled, 0)

    async def test_top_level_graphql_errors_do_not_echo_remote_secrets(self) -> None:
        secret = "oauth:super-secret-token"
        transport, _auth, _button, _sent_tokens = self._miner(
            [_Response(200, {"error": secret, "message": secret})]
        )

        with self.assertRaises(GQLException) as caught:
            await transport.gql_request(
                GQLPersistedQuery("Example", "0" * 64)
            )

        self.assertNotIn(secret, str(caught.exception))

    async def test_single_operation_rejects_batched_response(self) -> None:
        transport, _auth, _button, _sent_tokens = self._miner(
            [_Response(200, [{"data": {}}])]
        )

        with self.assertRaisesRegex(RequestException, "single operation"):
            await transport.gql_request(GQLPersistedQuery("Example", "0" * 64))

    async def test_batch_response_cardinality_must_match_request(self) -> None:
        transport, _auth, _button, _sent_tokens = self._miner(
            [_Response(200, [{"data": {}}])]
        )
        operations = [
            GQLPersistedQuery("First", "0" * 64),
            GQLPersistedQuery("Second", "1" * 64),
        ]

        with self.assertRaisesRegex(RequestException, "count"):
            await transport.gql_request(operations)

    async def test_transient_graphql_errors_stop_at_retry_limit(self) -> None:
        transport, _auth, _button, sent_tokens = self._miner(
            [
                _Response(200, {"errors": [{"message": "server error"}]})
                for _ in range(GQL_RETRY_ATTEMPTS)
            ]
        )
        transport.wait_for_delay = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(GQLException, "retry limit"):
            await transport.gql_request(
                GQLPersistedQuery("Example", "0" * 64)
            )

        self.assertEqual(len(sent_tokens), GQL_RETRY_ATTEMPTS)

    async def test_second_401_fails_after_the_controlled_retry(self) -> None:
        transport, auth, button, sent_tokens = self._miner(
            [
                _Response(401, {}),
                _Response(401, {}),
            ]
        )

        with self.assertRaisesRegex(LoginException, "rejected"):
            await transport.gql_request(
                GQLPersistedQuery("Example", "0" * 64)
            )

        self.assertEqual(sent_tokens, ["OAuth old-token", "OAuth new-token"])
        self.assertFalse(hasattr(auth, "access_token"))
        self.assertEqual(button.disabled, 2)


if __name__ == "__main__":
    unittest.main()
