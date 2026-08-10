"""Async game artwork and Steam enrichment for the Qt overview."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from game_metadata import SteamMetadata, SteamMetadataProvider
from translate import _

from .contracts import ImageCache
from .pages import HeroCard
from .tasks import QtTaskRegistry

if TYPE_CHECKING:
    from inventory import TimedDrop

logger = logging.getLogger("TwitchDrops")


class QtGameContextController:
    """Own generation, cancellation, and rendering for optional game context."""

    def __init__(
        self,
        hero: HeroCard,
        image_cache: ImageCache,
        metadata: SteamMetadataProvider,
        tasks: QtTaskRegistry,
    ) -> None:
        self._hero = hero
        self._image_cache = image_cache
        self._metadata = metadata
        self._tasks = tasks
        self._task: asyncio.Task[Any] | None = None
        self._generation = 0
        self._context_key: tuple[str, str] | None = None

    @property
    def task(self) -> asyncio.Task[Any] | None:
        return self._task

    def display(self, drop: TimedDrop) -> None:
        if not self._tasks.accepting:
            return
        campaign = drop.campaign
        game = campaign.game
        game_name = str(game.name)
        image_url = str(getattr(campaign, "image_url", ""))
        context_key = (game_name, image_url)
        if context_key == self._context_key:
            return
        self._context_key = context_key
        self._generation += 1
        self._cancel_task()
        baseline = SteamMetadata(game_name)
        slug = str(getattr(game, "slug", ""))
        twitch_url = (
            f"https://www.twitch.tv/directory/category/{slug}" if slug else ""
        )
        self._hero.set_links(
            steam=baseline.store_url,
            steamdb=baseline.steamdb_url,
            twitch=twitch_url,
        )
        self._hero.set_intel(_("gui", "text", "game_intel_waiting"))
        generation = self._generation
        self._task = self._tasks.create(
            self._load(game_name, image_url, twitch_url, generation)
        )

    def clear(self) -> None:
        self._context_key = None
        self._generation += 1
        self._cancel_task()

    def stop(self) -> None:
        self.clear()

    def _cancel_task(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _load(
        self,
        game_name: str,
        image_url: str,
        twitch_url: str,
        generation: int,
    ) -> None:
        try:
            image_task = (
                self._image_cache.get(image_url, (128, 172))
                if image_url
                else asyncio.sleep(0, result=None)
            )
            image, metadata = await asyncio.gather(
                image_task,
                self._metadata.get(game_name),
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            logger.debug("Game context unavailable: %s", type(exc).__name__)
            return
        if generation != self._generation:
            return
        if image is not None:
            self._hero.set_art(image)
        self._hero.set_links(
            steam=metadata.store_url,
            steamdb=metadata.steamdb_url,
            twitch=twitch_url,
        )
        self._hero.set_intel(self.metadata_text(metadata))

    @staticmethod
    def metadata_text(metadata: SteamMetadata) -> str:
        if metadata.error:
            return _("gui", "text", "game_intel_unavailable")
        if not metadata.available:
            return _("gui", "text", "game_intel_no_match")
        parts: list[str] = []
        if metadata.players is not None:
            parts.append(
                _("gui", "text", "players_playing").format(
                    players=f"{metadata.players:,}"
                )
            )
        if metadata.price is not None:
            parts.append(
                _("gui", "text", "price_us").format(price=metadata.price)
            )
        elif metadata.free_to_play:
            parts.append(_("gui", "text", "free_to_play"))
        if metadata.discount_percent:
            parts.append(f"-{metadata.discount_percent}%")
        if not parts:
            parts.append(_("gui", "text", "steam_listing_found"))
        return _("gui", "text", "steam_intel").format(
            details="  ·  ".join(parts)
        )
