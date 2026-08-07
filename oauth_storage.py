"""Small, private refresh-token store for the documented OAuth flow.

The Twitch cookie jar remains responsible for the existing browser/session
cookies. Refresh tokens are OAuth credentials rather than web cookies, so they
are kept in a separate atomically-written file with restrictive permissions.
"""
from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from utils import atomic_write, remove_stale_new


class OAuthTokenStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, client_id: str) -> str | None:
        if self._path.is_symlink():
            return None
        with suppress(OSError):
            self._path.chmod(0o600)
        try:
            payload: Any = json.loads(self._path.read_text(encoding="utf8"))
        except (OSError, UnicodeError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("client_id") != client_id:
            return None
        token = payload.get("refresh_token")
        return token if isinstance(token, str) and token else None

    def save(self, client_id: str, refresh_token: str) -> None:
        if not client_id or not refresh_token:
            raise ValueError("OAuth token records require non-empty values")

        def writer(temporary_path: Path) -> None:
            with temporary_path.open("w", encoding="utf8") as file:
                json.dump(
                    {"client_id": client_id, "refresh_token": refresh_token},
                    file,
                    sort_keys=True,
                )
                file.write("\n")

        atomic_write(self._path, writer)

    def clear(self) -> None:
        remove_stale_new(self._path)
