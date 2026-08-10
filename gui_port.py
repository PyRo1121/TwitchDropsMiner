from __future__ import annotations

from collections import abc
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

if TYPE_CHECKING:
    from channel import Channel
    from game import Game
    from inventory import DropsCampaign, TimedDrop

_T = TypeVar("_T")


class StatusPort(Protocol):
    def update(self, text: str) -> None: ...


class TrayPort(Protocol):
    def change_icon(self, state: str) -> None: ...

    def notify(
        self,
        message: str,
        title: str,
        duration: int = 5000,
        *,
        severity: Literal["info", "warning", "error"] = "info",
    ) -> bool: ...


class ChannelsPort(Protocol):
    def clear(self) -> None: ...

    def get_selection(self) -> Channel | None: ...

    def set_watching_channels(
        self,
        channels: abc.Iterable[Channel],
    ) -> None: ...

    def clear_watching(self) -> None: ...


class ProgressPort(Protocol):
    def minute_almost_done(self) -> bool: ...

    def stop_timer(self) -> None: ...


class WebsocketsPort(Protocol):
    def update(
        self,
        idx: int,
        status: str | None = None,
        topics: int | None = None,
    ) -> None: ...

    def remove(self, idx: int) -> None: ...


class InventoryPort(Protocol):
    async def replace_campaigns(
        self,
        campaigns: abc.Iterable[DropsCampaign],
    ) -> None: ...

    def update_drop(self, drop: TimedDrop) -> None: ...


class LoginPort(Protocol):
    def update(self, status: str, user_id: int | None) -> None: ...

    async def ask_enter_code(self, page_url: Any, user_code: str) -> None: ...


class GuiPort(Protocol):
    @property
    def status(self) -> StatusPort: ...

    @property
    def tray(self) -> TrayPort: ...

    @property
    def channels(self) -> ChannelsPort: ...

    @property
    def progress(self) -> ProgressPort: ...

    @property
    def websockets(self) -> WebsocketsPort: ...

    @property
    def inv(self) -> InventoryPort: ...

    @property
    def login(self) -> LoginPort: ...

    @property
    def close_requested(self) -> bool: ...

    async def wait_until_closed(self) -> None: ...

    async def coro_unless_closed(self, awaitable: Awaitable[_T]) -> _T: ...

    def set_authenticated(self, authenticated: bool) -> None: ...

    def grab_attention(self, *, sound: bool = True) -> None: ...

    def prevent_close(self) -> None: ...

    def start(self) -> None: ...

    async def stop(self) -> None: ...

    def close(self) -> bool: ...

    def close_window(self) -> None: ...

    def save(self, *, force: bool = False) -> None: ...

    def print(self, message: str) -> None: ...

    def set_games(self, games: set[Game]) -> None: ...

    def display_drop(
        self,
        drop: TimedDrop,
        *,
        countdown: bool = True,
        subone: bool = False,
    ) -> None: ...

    def clear_drop(self) -> None: ...

    def history_changed(self) -> None: ...
