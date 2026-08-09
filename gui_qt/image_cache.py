"""Async image cache shared by the Qt inventory and campaign cards.

The cache stores mutable data in the platform-standard per-user data directory.
Disk persistence is optional: QPixmap objects remain available from memory when
cache initialization or writes fail.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import warnings
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image
from PySide6.QtGui import QPixmap
from yarl import URL

from constants import CACHE_DB, CACHE_PATH
from utils import json_load, json_save
from .tasks import QtTaskRegistry

logger = logging.getLogger("TwitchDrops.ui")


class QtImageCache:
    LIFETIME = timedelta(days=7)
    NETWORK_TIMEOUT = 15.0
    MAX_IMAGE_BYTES = 8 * 1024 * 1024
    MAX_IMAGE_DIMENSION = 4096
    MAX_IMAGE_PIXELS = 4_000_000
    MAX_DECODED_IMAGE_BYTES = 64 * 1024 * 1024
    MAX_MEMORY_IMAGES = 128
    MAX_MEMORY_PIXMAPS = 256
    MAX_DISK_ENTRIES = 500
    _HASH_RE = re.compile(r"^[0-9a-f]{64}\.png$")
    _TRUSTED_IMAGE_HOST_SUFFIXES = (
        "jtvnw.net",
        "twitchcdn.net",
        "steamstatic.com",
        "akamaihd.net",
    )

    @classmethod
    def _safe_hash(cls, value: object) -> str | None:
        if isinstance(value, str) and cls._HASH_RE.fullmatch(value):
            return value
        return None

    def __init__(self, twitch: Any, *, tasks: QtTaskRegistry | None = None) -> None:
        self._twitch = twitch
        self._tasks = tasks or QtTaskRegistry()
        self._hashes: dict[str, dict[str, Any]] = {}
        self._images: OrderedDict[str, Image.Image] = OrderedDict()
        self._pixmaps: OrderedDict[
            tuple[str, tuple[int, int]], QPixmap
        ] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[tuple[Image.Image, str]]] = {}
        self._altered = False
        self._disk_enabled = True

        try:
            CACHE_PATH.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable_disk_cache("initialization", exc)
        if self._disk_enabled:
            try:
                loaded = json_load(CACHE_DB, {}, merge=False)
                self._hashes = loaded if isinstance(loaded, dict) else {}
            except OSError as exc:
                self._disable_disk_cache("mapping load", exc)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                logger.warning(
                    "Ignoring invalid image cache mapping: %s",
                    type(exc).__name__,
                )
        if self._disk_enabled:
            try:
                self._cleanup()
            except OSError as exc:
                self._disable_disk_cache("cleanup", exc)

    def _disable_disk_cache(self, operation: str, exc: BaseException) -> None:
        if self._disk_enabled:
            logger.warning(
                "Image cache persistence disabled during %s: %s",
                operation,
                type(exc).__name__,
            )
        self._disk_enabled = False
        self._hashes.clear()
        self._altered = False

    def _cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        valid: list[tuple[datetime, str, str]] = []
        for url, entry in list(self._hashes.items()):
            try:
                expires = entry["expires"]
                image_hash = self._safe_hash(entry["hash"])
                if (
                    image_hash is None
                    or not isinstance(expires, datetime)
                    or now >= expires
                ):
                    del self._hashes[url]
                    self._altered = True
                else:
                    valid.append((expires, url, image_hash))
            except (KeyError, TypeError):
                del self._hashes[url]
                self._altered = True

        valid.sort(reverse=True)
        for _expires, url, _image_hash in valid[self.MAX_DISK_ENTRIES :]:
            del self._hashes[url]
            self._altered = True
        live = {
            image_hash
            for _expires, _url, image_hash in valid[: self.MAX_DISK_ENTRIES]
        }
        for path in CACHE_PATH.glob("*.png"):
            if path.name not in live:
                path.unlink(missing_ok=True)

    def _expires(self) -> datetime:
        return datetime.now(timezone.utc) + self.LIFETIME

    @staticmethod
    def _decoded_bytes(image: Image.Image) -> int:
        return image.width * image.height * max(1, len(image.getbands()))

    def _remember_image(self, key: str, image: Image.Image) -> None:
        self._images[key] = image
        self._images.move_to_end(key)
        decoded_bytes = sum(self._decoded_bytes(item) for item in self._images.values())
        while self._images and (
            len(self._images) > self.MAX_MEMORY_IMAGES
            or decoded_bytes > self.MAX_DECODED_IMAGE_BYTES
        ):
            _, removed = self._images.popitem(last=False)
            decoded_bytes -= self._decoded_bytes(removed)

    def _remember_pixmap(
        self,
        key: tuple[str, tuple[int, int]],
        pixmap: QPixmap,
    ) -> None:
        self._pixmaps[key] = pixmap
        self._pixmaps.move_to_end(key)
        while len(self._pixmaps) > self.MAX_MEMORY_PIXMAPS:
            self._pixmaps.popitem(last=False)

    @staticmethod
    def _hash(image: Image.Image) -> str:
        digest = hashlib.sha256()
        digest.update(image.mode.encode("ascii"))
        digest.update(b"\0")
        digest.update(f"{image.width}x{image.height}".encode("ascii"))
        digest.update(b"\0")
        digest.update(image.tobytes())
        return f"{digest.hexdigest()}.png"

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
    def _open_image(cls, source: Any) -> Image.Image:
        with Image.open(source) as opened:
            width, height = opened.size
            if (
                width <= 0
                or height <= 0
                or width > cls.MAX_IMAGE_DIMENSION
                or height > cls.MAX_IMAGE_DIMENSION
                or width * height > cls.MAX_IMAGE_PIXELS
            ):
                raise ValueError("Image dimensions exceed cache limits")
            opened.load()
            return opened.copy()

    @classmethod
    def _trusted_remote_url(cls, value: str) -> bool:
        try:
            url = URL(value)
        except (TypeError, ValueError):
            return False
        host = url.host
        if (
            url.scheme != "https"
            or not host
            or url.user is not None
            or url.password is not None
            or url.port != 443
        ):
            return False
        normalized = host.rstrip(".").lower()
        return any(
            normalized == suffix or normalized.endswith(f".{suffix}")
            for suffix in cls._TRUSTED_IMAGE_HOST_SUFFIXES
        )

    @classmethod
    def _decode_file(cls, path: Path) -> Image.Image | None:
        try:
            with path.open("rb") as file:
                payload = file.read(cls.MAX_IMAGE_BYTES + 1)
        except OSError:
            return None
        return cls._decode(payload)

    @classmethod
    def _decode(cls, payload: bytes) -> Image.Image | None:
        if len(payload) > cls.MAX_IMAGE_BYTES:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                return cls._open_image(io.BytesIO(payload))
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            Image.UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            return None

    @classmethod
    async def _read_response(cls, response: Any) -> bytes | None:
        content_length = getattr(response, "content_length", None)
        if isinstance(content_length, int) and content_length > cls.MAX_IMAGE_BYTES:
            return None
        content = getattr(response, "content", None)
        if content is None:
            payload = await response.read()
            return payload if len(payload) <= cls.MAX_IMAGE_BYTES else None

        chunks: list[bytes] = []
        total = 0
        async for chunk in content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > cls.MAX_IMAGE_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def _download(self, url: str) -> Image.Image | None:
        async def request() -> Image.Image | None:
            current_url = url
            for _redirect in range(4):
                if not self._trusted_remote_url(current_url):
                    logger.warning("Blocked untrusted remote image URL")
                    return None
                async with self._twitch.transport.request(
                    "GET",
                    current_url,
                    preload=False,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400:
                        location = response.headers.get("Location")
                        if not isinstance(location, str) or not location:
                            return None
                        try:
                            current_url = str(URL(current_url).join(URL(location)))
                        except ValueError:
                            return None
                        continue
                    if not 200 <= response.status < 300:
                        return None
                    payload = await self._read_response(response)
                return self._decode(payload) if payload is not None else None
            logger.warning("Remote image exceeded redirect limit")
            return None

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
                        image = self._decode_file(path)
                        if image is None:
                            raise ValueError("Invalid cached image")
                        self._remember_image(image_hash, image)
                    else:
                        self._images.move_to_end(image_hash)
                    entry["expires"] = self._expires()
                    self._altered = True
                    return image, image_hash
                except (
                    FileNotFoundError,
                    OSError,
                    Image.DecompressionBombError,
                    Image.DecompressionBombWarning,
                    Image.UnidentifiedImageError,
                    ValueError,
                ):
                    del self._hashes[url]
                    self._altered = True

        image = await self._download(url)
        if image is None:
            # Do not cache a transient network failure for a week. A failed
            # lookup gets a stable in-memory key only for pixmap reuse.
            placeholder = self._placeholder()
            self._remember_image("placeholder.png", placeholder)
            return placeholder, "placeholder.png"

        image_hash = self._hash(image)
        self._remember_image(image_hash, image)
        if self._disk_enabled:
            try:
                image.save(self._cache_file(image_hash), format="PNG")
            except (OSError, ValueError) as exc:
                self._disable_disk_cache("image write", exc)
            else:
                self._hashes[url] = {
                    "hash": image_hash,
                    "expires": self._expires(),
                }
                self._altered = True
        return image, image_hash

    def _clear_inflight(self, url: str, task: asyncio.Task[tuple[Image.Image, str]]) -> None:
        if self._inflight.get(url) is task:
            del self._inflight[url]

    async def get(self, url: str, size: tuple[int, int]) -> QPixmap:
        key = str(url)
        task = self._inflight.get(key)
        if task is None:
            task = self._tasks.create(self._load_image(key))
            self._inflight[key] = task
            task.add_done_callback(lambda completed: self._clear_inflight(key, completed))

        image, image_hash = await asyncio.shield(task)
        cache_key = (image_hash, size)
        cached = self._pixmaps.get(cache_key)
        if cached is not None:
            self._pixmaps.move_to_end(cache_key)
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
        self._remember_pixmap(cache_key, pixmap)
        return pixmap

    def save(self, *, force: bool = False) -> None:
        if not self._disk_enabled or not (self._altered or force):
            return
        try:
            json_save(CACHE_DB, self._hashes, sort=True)
        except (OSError, TypeError, ValueError) as exc:
            self._disable_disk_cache("mapping save", exc)
        else:
            self._altered = False
