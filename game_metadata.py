"""Optional game metadata used by the Qt dashboard.

Twitch remains the source of truth for farming.  This module only enriches the
presentation layer with best-effort Steam information and never participates
in campaign selection, channel selection, or watch requests.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote_plus

from yarl import URL

from constants import CACHE_PATH
from utils import cancel_tasks, json_load, json_save, normalize_key, safe_int

logger = logging.getLogger("TwitchDrops.ui")


@dataclass(frozen=True)
class SteamMetadata:
    """Presentation-safe Steam metadata for one Twitch game."""

    game_name: str
    app_id: int | None = None
    matched_name: str | None = None
    players: int | None = None
    price: str | None = None
    free_to_play: bool | None = None
    discount_percent: int | None = None
    error: str | None = None
    updated_at: float | None = None

    @property
    def store_url(self) -> str:
        if self.app_id is not None:
            return f"https://store.steampowered.com/app/{self.app_id}/"
        return (
            "https://store.steampowered.com/search/?term="
            f"{quote_plus(self.game_name)}"
        )

    @property
    def steamdb_url(self) -> str:
        if self.app_id is not None:
            return f"https://steamdb.info/app/{self.app_id}/"
        return (
            "https://steamdb.info/search/?a=app&q="
            f"{quote_plus(self.game_name)}"
        )

    @property
    def available(self) -> bool:
        return self.app_id is not None


class SteamMetadataProvider:
    """Fetch and cache optional Steam store/player data.

    The provider deliberately uses the existing Twitch session adapter so the
    user's proxy and close handling still apply.  Calls are deduplicated and
    cached for six hours; a failure never bubbles into the miner loop.
    """

    CACHE_FILE = CACHE_PATH / "steam-metadata.json"
    CACHE_SECONDS = 6 * 60 * 60
    MAX_CACHE_ENTRIES = 500
    REQUEST_TIMEOUT = 30.0

    def __init__(self, twitch: Any, *, country: str = "us") -> None:
        self._twitch = twitch
        self._country = country
        self._cache: dict[str, dict[str, Any]] = {}
        self._persistence_available = True
        try:
            loaded = json_load(self.CACHE_FILE, {}, merge=False)
            self._cache = loaded if isinstance(loaded, dict) else {}
        except OSError as exc:
            self._persistence_available = False
            logger.warning(
                "Steam metadata persistence disabled during load: %s",
                type(exc).__name__,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "Ignoring invalid Steam metadata cache: %s",
                type(exc).__name__,
            )
        self._inflight: dict[str, asyncio.Task[SteamMetadata]] = {}
        self._waiters: dict[str, int] = {}
        self._altered = False
        self._prune_cache()

    def _prune_cache(self) -> None:
        now = time.time()
        valid: list[tuple[float, str]] = []
        for key, raw in list(self._cache.items()):
            updated_at = raw.get("updated_at") if isinstance(raw, dict) else None
            if not isinstance(updated_at, (int, float)) or isinstance(
                updated_at, bool
            ):
                del self._cache[key]
                self._altered = True
                continue
            if (
                not math.isfinite(updated_at)
                or now - updated_at >= self.CACHE_SECONDS
            ):
                del self._cache[key]
                self._altered = True
                continue
            valid.append((updated_at, key))
        valid.sort(reverse=True)
        for _updated_at, key in valid[self.MAX_CACHE_ENTRIES :]:
            del self._cache[key]
            self._altered = True

    @staticmethod
    def normalize_name(name: str) -> str:
        """Normalize names for exact, conservative Steam matching."""
        return normalize_key(name)

    async def get(self, game_name: str) -> SteamMetadata:
        name = str(game_name).strip()
        key = self.normalize_name(name)
        if not key:
            return SteamMetadata(game_name=name, error="empty game name")

        cached = self._cached(key, name)
        if cached is not None:
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._bounded_fetch(name, key))
            self._inflight[key] = task
            task.add_done_callback(
                lambda completed: self._clear_inflight(key, completed)
            )
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            remaining = self._waiters.get(key, 1) - 1
            if remaining > 0:
                self._waiters[key] = remaining
            else:
                self._waiters.pop(key, None)
                if not task.done():
                    task.cancel()

    async def _bounded_fetch(self, name: str, key: str) -> SteamMetadata:
        task = asyncio.create_task(self._fetch(name, key))
        try:
            done, pending = await asyncio.wait((task,), timeout=self.REQUEST_TIMEOUT)
            if pending:
                await cancel_tasks(pending)
                return SteamMetadata(game_name=name, error="Steam request timed out")
            return await task
        finally:
            if not task.done():
                await cancel_tasks((task,))

    def _clear_inflight(
        self,
        key: str,
        task: asyncio.Task[SteamMetadata],
    ) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]

    def _cached(self, key: str, name: str) -> SteamMetadata | None:
        raw = self._cache.get(key)
        if not isinstance(raw, dict):
            return None
        try:
            updated_at = float(raw["updated_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if time.time() - updated_at >= self.CACHE_SECONDS or "free_to_play" not in raw:
            return None
        return self._from_cache(raw, name, updated_at)

    @staticmethod
    def _from_cache(
        raw: dict[str, Any], name: str, updated_at: float
    ) -> SteamMetadata:
        return SteamMetadata(
            game_name=name,
            app_id=safe_int(raw.get("app_id")),
            matched_name=raw.get("matched_name"),
            players=safe_int(raw.get("players")),
            price=raw.get("price"),
            free_to_play=raw.get("free_to_play"),
            discount_percent=safe_int(raw.get("discount_percent")),
            error=raw.get("error"),
            updated_at=updated_at,
        )

    async def _fetch(self, name: str, key: str) -> SteamMetadata:
        try:
            search_url = URL("https://store.steampowered.com/api/storesearch/").with_query(
                term=name,
                l="english",
                cc=self._country,
            )
            payload = await self._json(search_url)
            item = self._exact_item(payload, name)
            if item is None:
                result = SteamMetadata(game_name=name, updated_at=time.time())
            else:
                app_id = self._int(item.get("id"))
                price, discount = self._price(item.get("price"))
                players = await self._players(app_id) if app_id is not None else None
                raw_free_to_play = item.get("is_free")
                free_to_play = (
                    raw_free_to_play
                    if isinstance(raw_free_to_play, bool)
                    else False
                    if price is not None
                    else None
                )
                result = SteamMetadata(
                    game_name=name,
                    app_id=app_id,
                    matched_name=item.get("name"),
                    players=players,
                    price=price,
                    free_to_play=free_to_play,
                    discount_percent=discount,
                    updated_at=time.time(),
                )
        except Exception as exc:  # optional enrichment must never break farming
            logger.debug("Steam metadata unavailable for %s: %s", name, type(exc).__name__)
            return SteamMetadata(
                game_name=name,
                error=type(exc).__name__,
                updated_at=time.time(),
            )
        self._cache[key] = asdict(result)
        self._altered = True
        self._prune_cache()
        return result

    async def _json(self, url: URL) -> dict[str, Any]:
        async with self._twitch.transport.request("GET", url) as response:
            if response.status != 200:
                raise RuntimeError(f"Steam metadata HTTP {response.status}")
            try:
                value = await response.json(content_type=None)
            except (TypeError, ValueError) as exc:
                raise ValueError("Steam metadata response is not JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Steam metadata response must be an object")
        return value

    async def _players(self, app_id: int | None) -> int | None:
        if app_id is None:
            return None
        url = URL(
            "https://api.steampowered.com/ISteamUserStats/"
            "GetNumberOfCurrentPlayers/v1/"
        ).with_query(appid=app_id)
        payload = await self._json(url)
        response = payload.get("response")
        if not isinstance(response, dict):
            return None
        return self._int(response.get("player_count"))

    @classmethod
    def _exact_item(cls, payload: dict[str, Any], name: str) -> dict[str, Any] | None:
        items = payload.get("items")
        if not isinstance(items, list):
            return None
        target = cls.normalize_name(name)
        for item in items:
            if isinstance(item, dict) and cls.normalize_name(str(item.get("name", ""))) == target:
                return item
        return None

    @staticmethod
    def _int(value: Any) -> int | None:
        return safe_int(value)

    @classmethod
    def _price(cls, value: Any) -> tuple[str | None, int | None]:
        if not isinstance(value, dict):
            return None, None
        formatted = value.get("final_formatted") or value.get("initial_formatted")
        if not isinstance(formatted, str):
            cents = cls._int(value.get("final", value.get("initial")))
            currency = str(value.get("currency", ""))
            if cents is not None:
                symbols = {
                    "USD": "$",
                    "CAD": "C$",
                    "AUD": "A$",
                    "EUR": "€",
                    "GBP": "£",
                }
                symbol = symbols.get(currency, f"{currency} ")
                amount = cents if currency in {"JPY", "KRW"} else cents / 100
                formatted = (
                    f"{symbol}{amount:.0f}"
                    if currency in {"JPY", "KRW"}
                    else f"{symbol}{amount:.2f}"
                )
        return formatted if isinstance(formatted, str) else None, cls._int(
            value.get("discount_percent")
        )

    def save(self, *, force: bool = False) -> None:
        if not self._persistence_available or not (self._altered or force):
            return
        try:
            CACHE_PATH.mkdir(parents=True, exist_ok=True)
            json_save(self.CACHE_FILE, self._cache, sort=True)
        except (OSError, TypeError, ValueError) as exc:
            self._persistence_available = False
            logger.warning(
                "Steam metadata persistence disabled during save: %s",
                type(exc).__name__,
            )
        else:
            self._altered = False
