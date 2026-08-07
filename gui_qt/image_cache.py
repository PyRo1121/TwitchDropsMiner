"""Async image cache shared by the Qt inventory and campaign cards.

The cache deliberately keeps the existing ``cache/mapping.json`` format so a
user's cached Twitch CDN images survive the UI migration. QPixmap objects are
created after the async network/file section returns to the qasync GUI thread.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import aiohttp
from PIL import Image
from PySide6.QtGui import QPixmap

from constants import CACHE_DB, CACHE_PATH
from utils import json_load, json_save

logger = logging.getLogger("TwitchDrops.ui")


class QtImageCache:
    LIFETIME = timedelta(days=7)
    NETWORK_TIMEOUT = 15.0
    MAX_IMAGE_BYTES = 8 * 1024 * 1024
    _HASH_RE = re.compile(r"^[0-9a-f]+\.png$")

    @classmethod
    def _safe_hash(cls, value: object) -> str | None:
        if isinstance(value, str) and cls._HASH_RE.fullmatch(value):
            return value
        return None

    def __init__(self, twitch: Any) -> None:
        self._twitch = twitch
        CACHE_PATH.mkdir(parents=True, exist_ok=True)
        try:
            self._hashes: dict[str, dict[str, Any]] = json_load(
                CACHE_DB, {}, merge=False
            )
        except Exception:
            self._hashes = {}
        self._images: dict[str, Image.Image] = {}
        self._pixmaps: dict[tuple[str, tuple[int, int]], QPixmap] = {}
        self._inflight: dict[str, asyncio.Task[tuple[Image.Image, str]]] = {}
        self._altered = False
        self._cleanup()

    def _cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        live: set[str] = set()
        for url, entry in list(self._hashes.items()):
            try:
                expires = entry["expires"]
                image_hash = self._safe_hash(entry["hash"])
                if image_hash is None or not isinstance(expires, datetime) or now >= expires:
                    del self._hashes[url]
                    self._altered = True
                else:
                    live.add(image_hash)
            except (KeyError, TypeError):
                del self._hashes[url]
                self._altered = True
        for path in CACHE_PATH.glob("*.png"):
            if path.name not in live:
                path.unlink(missing_ok=True)

    def _expires(self) -> datetime:
        return datetime.now(timezone.utc) + self.LIFETIME

    @staticmethod
    def _hash(image: Image.Image) -> str:
        grayscale = image.resize((10, 10), Image.Resampling.LANCZOS).convert("L")
        pixels = [
            cast(int, grayscale.getpixel((x, y)))
            for y in range(10)
            for x in range(10)
        ]
        average = sum(pixels) / len(pixels)
        bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
        return f"{int(bits, 2):x}.png"

    @staticmethod
    def _placeholder() -> Image.Image:
        return Image.new("RGB", (120, 160), (25, 35, 51))

    @staticmethod
    def _cache_file(image_hash: str) -> Path:
        root = CACHE_PATH.resolve()
        candidate = (root / image_hash).resolve()
        if candidate.parent != root:
            raise ValueError("invalid image cache path")
        return candidate

    @classmethod
    def _decode(cls, payload: bytes) -> Image.Image | None:
        if len(payload) > cls.MAX_IMAGE_BYTES:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as opened:
                    opened.load()
                    return opened.copy()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            Image.UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            return None

    async def _download(self, url: str) -> Image.Image | None:
        async def request() -> Image.Image | None:
            async with self._twitch.request("GET", url) as response:
                if not 200 <= response.status < 300:
                    return None
                payload = await response.read()
            return self._decode(payload)

        try:
            return await asyncio.wait_for(request(), timeout=self.NETWORK_TIMEOUT)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, RuntimeError) as exc:
            logger.debug("Image unavailable: %s", type(exc).__name__)
            return None

    async def _load_image(self, url: str) -> tuple[Image.Image, str]:
        entry = self._hashes.get(url)
        if entry is not None:
            image_hash = self._safe_hash(entry.get("hash"))
            if image_hash is not None:
                try:
                    path = self._cache_file(image_hash)
                    image = self._images.get(image_hash)
                    if image is None:
                        with Image.open(path) as opened:
                            opened.load()
                            image = opened.copy()
                        self._images[image_hash] = image
                    entry["expires"] = self._expires()
                    self._altered = True
                    return image, image_hash
                except (FileNotFoundError, OSError, Image.UnidentifiedImageError, ValueError):
                    del self._hashes[url]
                    self._altered = True

        image = await self._download(url)
        if image is None:
            # Do not cache a transient network failure for a week. A failed
            # lookup gets a stable in-memory key only for pixmap reuse.
            placeholder = self._placeholder()
            self._images["placeholder.png"] = placeholder
            return placeholder, "placeholder.png"

        image_hash = self._hash(image)
        self._images[image_hash] = image
        image.save(self._cache_file(image_hash), format="PNG")
        self._hashes[url] = {"hash": image_hash, "expires": self._expires()}
        self._altered = True
        return image, image_hash

    def _clear_inflight(self, url: str, task: asyncio.Task[tuple[Image.Image, str]]) -> None:
        if self._inflight.get(url) is task:
            del self._inflight[url]

    async def get(self, url: str, size: tuple[int, int]) -> QPixmap:
        key = str(url)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._load_image(key))
            self._inflight[key] = task
            task.add_done_callback(lambda completed: self._clear_inflight(key, completed))

        image, image_hash = await task
        cache_key = (image_hash, size)
        cached = self._pixmaps.get(cache_key)
        if cached is not None:
            return cached
        rendered = image.copy()
        if rendered.size != size:
            rendered.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = self._placeholder().resize(size, Image.Resampling.NEAREST)
            x = (size[0] - rendered.width) // 2
            y = (size[1] - rendered.height) // 2
            canvas.paste(rendered, (x, y))
            rendered = canvas
        stream = io.BytesIO()
        rendered.save(stream, format="PNG")
        pixmap = QPixmap()
        pixmap.loadFromData(stream.getvalue())
        self._pixmaps[cache_key] = pixmap
        return pixmap

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(CACHE_DB, self._hashes, sort=True)
            self._altered = False
