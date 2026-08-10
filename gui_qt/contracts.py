"""Typed contracts shared by Qt adapters and pages."""
from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, TypeVar

from PySide6.QtGui import QPixmap

_T = TypeVar("_T")


class LoginManager(Protocol):
    """Backend-facing operations used by the login form."""

    @property
    def accepting_actions(self) -> bool: ...

    def grab_attention(self, *, sound: bool = True) -> None: ...

    def print(self, message: str) -> None: ...

    async def coro_unless_closed(self, awaitable: Awaitable[_T]) -> _T: ...


class ImageCache(Protocol):
    """Async image loading contract used by inventory cards."""

    async def get(self, url: str, size: tuple[int, int]) -> QPixmap: ...
