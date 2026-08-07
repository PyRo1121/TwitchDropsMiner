from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from PIL import Image
from typing import Any, cast
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel
except ModuleNotFoundError as exc:  # pragma: no cover - depends on test environment
    raise unittest.SkipTest(f"Qt test dependencies unavailable: {exc}") from exc

from constants import PriorityMode
from exceptions import ExitRequest, WebsocketClosed
from gui_qt.autostart import AutostartManager
from gui_qt.image_cache import QtImageCache
from gui_qt.manager import QtGUIManager
from translate import _
from utils import AwaitableValue
from websocket import Websocket


@dataclass
class FakeSettings:
    dark_mode: bool = True
    tray: bool = False
    autostart_tray: bool = False
    tray_notifications: bool = True
    proxy: str = ""
    priority: list[str] = field(default_factory=list)
    exclude: set[str] = field(default_factory=set)
    priority_mode: PriorityMode = PriorityMode.PRIORITY_ONLY
    language: str = "English"
    enable_badges_emotes: bool = False
    available_drops_check: bool = False
    logging_level: int = logging.ERROR


class FakeTwitch:
    def __init__(self, settings: FakeSettings) -> None:
        self.settings = settings

    def close(self) -> None:
        pass

    def state_change(self, state):
        return lambda: None


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages = [
            aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, "not-json", ""),
            aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, None, ""),
        ]

    async def receive(self, timeout: float):
        return self.messages.pop(0)


class QtUiTests(unittest.TestCase):
    app: QApplication
    manager: QtGUIManager | None

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        cls.app = cast(QApplication, instance) if instance is not None else QApplication([])

    def setUp(self) -> None:
        _.set_language("English")
        self.manager = None

    def tearDown(self) -> None:
        if self.manager is not None:
            self.manager.close_window()
            self.app.processEvents()
            self.manager = None

    def make_manager(self, settings: FakeSettings | None = None) -> QtGUIManager:
        self.manager = QtGUIManager(cast(Any, FakeTwitch(settings or FakeSettings())))
        return self.manager

    def test_gui_protocol_and_persistent_tray_flag(self) -> None:
        manager = self.make_manager(FakeSettings(autostart_tray=True))
        for name in (
            "status",
            "websockets",
            "login",
            "progress",
            "channels",
            "inv",
            "tray",
            "help",
            "settings",
            "display_drop",
            "clear_drop",
            "wait_until_closed",
        ):
            self.assertTrue(hasattr(manager, name), name)
        self.assertTrue(manager._tray_requested)
        self.assertEqual(manager._nav_buttons["help"].text(), "Help && About")

    def test_modern_shell_navigation_and_command_palette(self) -> None:
        manager = self.make_manager()
        self.assertEqual(manager._nav_buttons["overview"].objectName(), "nav")
        self.assertNotIn("inventory", manager._nav_buttons)
        self.assertFalse(manager.websocket_label.isVisible())
        self.assertFalse(manager._nav_buttons["overview"].icon().isNull())
        manager._command.setText("settings")
        manager._submit_command()
        self.assertIs(manager.stack.currentWidget(), manager.pages["settings"])
        self.assertEqual(manager._page_context.text(), "Preferences")

    def test_generated_brand_symbol_is_loaded(self) -> None:
        manager = self.make_manager()
        mark = manager.findChild(QLabel, "brandMark")
        self.assertIsNotNone(mark)
        assert mark is not None
        pixmap = mark.pixmap()
        self.assertIsNotNone(pixmap)
        assert pixmap is not None
        self.assertFalse(pixmap.isNull())
        self.assertGreaterEqual(mark.width(), 52)

    def test_dashboard_readout_and_external_game_links(self) -> None:
        manager = self.make_manager()
        manager._status_changed("Idle")
        self.assertIn("standing by", manager.diagnostic_label.text())
        manager.hero.set_links(
            steam="https://store.steampowered.com/search/?term=Test",
            steamdb="https://steamdb.info/search/?a=app&q=Test",
            twitch="https://www.twitch.tv/directory/category/test",
        )
        self.assertTrue(manager.hero.steam_button.isEnabled())
        self.assertTrue(manager.hero.steamdb_button.isEnabled())
        self.assertTrue(manager.hero.twitch_button.isEnabled())

    def test_arabic_layout_and_translated_labels(self) -> None:
        _.set_language("العربية")
        settings = FakeSettings(language="العربية")
        manager = self.make_manager(settings)
        self.assertEqual(self.app.layoutDirection(), Qt.LayoutDirection.RightToLeft)
        self.assertEqual(manager._nav_buttons["channels"].text(), _("gui", "channels", "name"))
        self.assertEqual(
            manager.settings.priority_mode.itemText(0),
            _("gui", "settings", "priority_modes", "priority_only"),
        )

    def test_close_window_survives_presentation_cache_errors(self) -> None:
        manager = self.make_manager()
        with patch.object(manager._steam_metadata, "save", side_effect=OSError("read-only")), patch.object(
            manager._image_cache, "save", side_effect=ValueError("bad cache")
        ):
            manager.close_window()
        self.assertFalse(manager._closing)

    def test_close_interrupts_async_waiters(self) -> None:
        manager = self.make_manager()

        async def wait_for_close() -> None:
            waiter = asyncio.create_task(manager.coro_unless_closed(asyncio.sleep(60)))
            await asyncio.sleep(0)
            manager.close()
            await waiter

        with self.assertRaises(ExitRequest):
            asyncio.run(wait_for_close())

    def test_websocket_ignores_malformed_text(self) -> None:
        socket = Websocket.__new__(Websocket)
        socket._idx = 0
        socket._ws = AwaitableValue()
        socket._ws.set(cast(Any, FakeWebSocket()))

        async def receive_messages() -> None:
            with self.assertRaises(WebsocketClosed):
                await socket._gather_recv([], timeout=0)

        asyncio.run(receive_messages())

    def test_image_hash_rejects_paths(self) -> None:
        self.assertIsNone(QtImageCache._safe_hash("../outside-cache"))
        self.assertIsNone(QtImageCache._safe_hash("not-a-sha256"))
        self.assertIsNotNone(QtImageCache._safe_hash("a" * 64 + ".png"))

    def test_image_cache_deduplicates_requests_and_does_not_cache_failures(self) -> None:
        class Response:
            status = 200

            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def read(self) -> bytes:
                return self.payload

        class Twitch:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload
                self.calls = 0

            def request(self, *_args: object, **_kwargs: object) -> Response:
                self.calls += 1
                return Response(self.payload)

        image = Image.new("RGB", (32, 32), (100, 140, 200))
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        valid_twitch = Twitch(stream.getvalue())
        invalid_twitch = Twitch(b"not an image")

        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                cache_path = Path(directory) / "cache"
                mapping_path = cache_path / "mapping.json"
                with patch("gui_qt.image_cache.CACHE_PATH", cache_path), patch(
                    "gui_qt.image_cache.CACHE_DB", mapping_path
                ):
                    cache = QtImageCache(valid_twitch)
                    first, second = await asyncio.gather(
                        cache.get("https://cdn.example/image.png", (64, 64)),
                        cache.get("https://cdn.example/image.png", (32, 32)),
                    )
                    self.assertFalse(first.isNull())
                    self.assertFalse(second.isNull())
                    self.assertEqual(valid_twitch.calls, 1)
                    cache.save()
                    self.assertTrue(mapping_path.exists())

                    failed_cache = QtImageCache(invalid_twitch)
                    failed = await failed_cache.get("https://cdn.example/bad.png", (32, 32))
                    retried = await failed_cache.get("https://cdn.example/bad.png", (32, 32))
                    self.assertFalse(failed.isNull())
                    self.assertFalse(retried.isNull())
                    self.assertEqual(invalid_twitch.calls, 2)
                    self.assertNotIn("https://cdn.example/bad.png", failed_cache._hashes)

        asyncio.run(exercise())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux autostart semantics")
    def test_linux_autostart_round_trip(self) -> None:
        settings = FakeSettings()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory}
        ):
            autostart = AutostartManager(settings)
            path = autostart.linux_path()
            autostart.set_enabled(True, tray=True)
            self.assertTrue(path.exists())
            self.assertTrue(autostart.is_enabled())
            self.assertIn("--tray", path.read_text(encoding="utf8"))
            autostart.set_enabled(False, tray=True)
            self.assertFalse(path.exists())
            self.assertFalse(autostart.is_enabled())


if __name__ == "__main__":
    unittest.main()
