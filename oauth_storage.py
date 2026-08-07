"""Small, private refresh-token store for the documented OAuth flow.

The Twitch cookie jar remains responsible for the existing browser/session
cookies. Refresh tokens are OAuth credentials rather than web cookies, so they
are kept in a separate atomically-written file with restrictive permissions.
"""
from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any


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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_name(f"{self._path.name}.new")
        try:
            if temporary_path.is_symlink():
                raise OSError("OAuth token temporary path is a symlink")
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_TRUNC
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(temporary_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf8") as file:
                json.dump(
                    {"client_id": client_id, "refresh_token": refresh_token},
                    file,
                    sort_keys=True,
                )
                file.write("\n")
            temporary_path.chmod(0o600)
            temporary_path.replace(self._path)
            self._path.chmod(0o600)
        finally:
            with suppress(OSError):
                temporary_path.unlink()

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)
        self._path.with_name(f"{self._path.name}.new").unlink(missing_ok=True)
