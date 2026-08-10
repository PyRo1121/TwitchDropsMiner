from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import tempfile
import unittest
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from PIL import Image
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton
except ModuleNotFoundError as exc:  # pragma: no cover - depends on test environment
    raise unittest.SkipTest(f"Qt test dependencies unavailable: {exc}") from exc

from constants import PROJECT_URL, ClientType, PriorityMode, State
from exceptions import ExitRequest, WebsocketClosed
from gui_qt.autostart import AutostartManager
from gui_qt.image_cache import QtImageCache
from gui_qt.manager import QtGUIManager
from gui_qt.pages import CampaignCard, ChannelRow, InventoryPage
from gui_qt.theme import LIGHT
from gui_qt.widgets import Badge, ProgressRing, SegmentedProgress
from session_history import HistoryEvent
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
    experimental_dual_watch: bool = False
    logging_level: int = logging.ERROR

    def alter(self) -> None:
        pass


class FakeTwitch:
    def __init__(self, settings: FakeSettings) -> None:
        self.settings = settings
        self.inventory: list[Any] = []
        self.channels: dict[int, Any] = {}
        self.state_changes: list[Any] = []

    def close(self) -> None:
        pass

    def state_change(self, state):
        return lambda: self.change_state(state)

    def change_state(self, state: Any) -> None:
        self.state_changes.append(state)


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

    def test_failed_remote_revocation_still_performs_local_logout(self) -> None:
        manager = self.make_manager()
        auth_state = SimpleNamespace(
            _access_token="access-token",
            invalidate=Mock(),
        )

        class FailingTransport:
            def request(self, *_args: object, **_kwargs: object) -> object:
                raise OSError("offline")

        twitch = cast(Any, manager._twitch)
        twitch._auth_state = auth_state
        twitch._client_type = ClientType.WEB
        twitch.transport = FailingTransport()

        asyncio.run(manager.help._invalidate_token())

        auth_state.invalidate.assert_called_once_with(
            delete_cookies=True,
            delete_refresh_token=True,
        )
        self.assertEqual(twitch.state_changes, [State.RESTART])

    def test_modern_shell_navigation_and_command_palette(self) -> None:
        manager = self.make_manager()
        self.assertEqual(manager._nav_buttons["overview"].objectName(), "nav")
        self.assertNotIn("inventory", manager._nav_buttons)
        self.assertFalse(manager.websocket_label.isHidden())
        self.assertFalse(manager._nav_buttons["overview"].icon().isNull())
        manager._command.setText("settings")
        manager._submit_command()
        self.assertIs(manager.stack.currentWidget(), manager.pages["settings"])
        self.assertEqual(manager._page_context.text(), "Preferences")

    def test_repeated_navigation_cleans_replaced_page_animation(self) -> None:
        manager = self.make_manager()
        manager._navigate("channels")
        channels_page = manager.pages["channels"]
        first_animation = manager._page_animation
        self.assertIsNotNone(channels_page.graphicsEffect())

        manager._navigate("drops")

        self.assertIsNot(manager._page_animation, first_animation)
        self.assertIsNone(channels_page.graphicsEffect())

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

    def test_custom_progress_widgets_expose_accessible_values(self) -> None:
        ring = ProgressRing()
        ring.set_progress(0.5, "50%", "Campaign")
        segmented = SegmentedProgress()
        segmented.setAccessibleName("Drop")
        segmented.set_value(0.25)

        self.assertEqual(ring.accessibleName(), "Campaign")
        self.assertEqual(ring.accessibleDescription(), "50%")
        self.assertEqual(segmented.accessibleName(), "Drop")
        self.assertEqual(segmented.accessibleDescription(), "25%")

    def test_channel_rows_are_keyboard_selectable_and_accessible(self) -> None:
        channel = SimpleNamespace(
            id=42,
            name="example",
            pending_online=False,
            online=True,
            drops_enabled=True,
            game=SimpleNamespace(name="Example Game"),
            viewers=123,
            url="https://www.twitch.tv/example",
        )
        row = ChannelRow(cast(Any, channel))
        selected: list[int] = []
        row.clicked.connect(selected.append)

        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertIn("example", row.accessibleName())
        self.assertIn("Example Game", row.accessibleDescription())
        QTest.keyClick(row, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(selected, [42])

    def test_campaign_schedule_and_allowlist_are_rendered(self) -> None:
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        campaign = SimpleNamespace(
            game=SimpleNamespace(name="Example Game"),
            name="Example Campaign",
            drops=[],
            image_url="",
            linked=True,
            link_url="https://www.twitch.tv/drops/campaigns",
            active=False,
            upcoming=True,
            expired=False,
            allowed_channels=[SimpleNamespace(name="allowed-channel")],
            progress=0.25,
            claimed_drops=0,
            total_drops=1,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(days=1),
        )

        card = CampaignCard(cast(Any, campaign))

        expected_time = starts_at.astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(
            card.timeline.text(),
            _("gui", "inventory", "starts").format(time=expected_time),
        )
        self.assertIn("allowed-channel", card.allowed.text())
        self.assertFalse(card.allowed.isHidden())
        card.deleteLater()

    def test_inventory_presentation_stages_before_atomic_publish(self) -> None:
        async def exercise() -> None:
            settings = FakeSettings(priority=["Example Game"])
            page = InventoryPage(settings)
            cache = SimpleNamespace(get=AsyncMock(return_value=QPixmap()))

            def campaign(campaign_id: str) -> SimpleNamespace:
                starts_at = datetime.now(timezone.utc)
                return SimpleNamespace(
                    id=campaign_id,
                    game=SimpleNamespace(name="Example Game"),
                    name=campaign_id,
                    drops=[],
                    image_url="",
                    linked=True,
                    link_url="https://www.twitch.tv/drops/campaigns",
                    active=True,
                    upcoming=False,
                    expired=False,
                    allowed_channels=[],
                    progress=0.0,
                    claimed_drops=0,
                    total_drops=1,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(days=1),
                    required_minutes=60,
                    eligible=True,
                    finished=False,
                )

            first = await page.stage_campaigns(
                [cast(Any, campaign("first"))],
                cast(Any, cache),
            )
            first.commit()
            first.finalize()

            second = await page.stage_campaigns(
                [cast(Any, campaign("second"))],
                cast(Any, cache),
            )
            self.assertEqual(set(page._campaigns), {"first"})
            second.commit()
            self.assertEqual(set(page._campaigns), {"second"})
            second.rollback()
            self.assertEqual(set(page._campaigns), {"first"})

            page._clear_campaigns()
            page.stop()
            page.deleteLater()

        asyncio.run(exercise())

    def test_inventory_timer_refreshes_temporal_campaign_state(self) -> None:
        settings = FakeSettings(priority=["Example Game"])
        page = InventoryPage(settings)
        campaign = SimpleNamespace(
            game=SimpleNamespace(name="Example Game"),
            name="Example Campaign",
            drops=[],
            image_url="",
            linked=True,
            link_url="https://www.twitch.tv/drops/campaigns",
            active=False,
            upcoming=True,
            expired=False,
            allowed_channels=[],
            progress=0.0,
            claimed_drops=0,
            total_drops=1,
            starts_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ends_at=datetime.now(timezone.utc) + timedelta(days=1),
            required_minutes=60,
            eligible=True,
            finished=False,
        )
        card = CampaignCard(cast(Any, campaign))
        page._campaigns["campaign"] = card
        self.assertEqual(
            card.status.text(),
            _("gui", "inventory", "status", "upcoming"),
        )

        campaign.upcoming = False
        campaign.active = True
        page._refresh_timer.timeout.emit()

        self.assertEqual(
            card.status.text(),
            _("gui", "inventory", "status", "active"),
        )
        page.stop()
        page.deleteLater()
        card.deleteLater()

    def test_settings_watch_changes_refresh_and_reconcile(self) -> None:
        manager = self.make_manager()
        twitch = cast(FakeTwitch, manager._twitch)
        manager.settings.priority_entry.setEditText("Example Game")

        with patch.object(manager.inventory, "refresh") as refresh:
            manager.settings._priority_add()

        refresh.assert_called_once_with()
        self.assertEqual(manager._twitch.settings.priority, ["Example Game"])
        self.assertEqual(twitch.state_changes[-1], State.GAMES_UPDATE)

    def test_proxy_editor_preserves_previous_value_on_invalid_input(self) -> None:
        settings = FakeSettings(proxy="http://old.example:8080")
        manager = self.make_manager(settings)
        self.assertEqual(
            manager.settings.proxy.echoMode(),
            QLineEdit.EchoMode.Password,
        )

        manager.settings.proxy.setText("ftp://invalid.example:8080")
        manager.settings._proxy_changed()
        self.assertEqual(settings.proxy, "http://old.example:8080")
        self.assertTrue(manager.settings.proxy.styleSheet())

        manager.settings.proxy.setText("https://new.example:8443")
        manager.settings._proxy_changed()
        self.assertEqual(str(settings.proxy), "https://new.example:8443")
        self.assertFalse(manager.settings.proxy.styleSheet())

    def test_theme_reapplies_status_and_badge_semantic_colors(self) -> None:
        manager = self.make_manager()
        manager._status_changed("Watching")

        manager.apply_theme(False)

        self.assertIn(LIGHT.green, manager._health._dot.styleSheet())
        self.assertEqual(manager._signal_pulse._color, LIGHT.green)
        badge = Badge("LIVE", "success", manager)
        manager._refresh_semantic_colors()
        self.assertIn(LIGHT.green, badge.styleSheet())

    def test_metric_and_focus_styles_target_actionable_widgets(self) -> None:
        manager = self.make_manager()
        qss = manager._theme.qss

        self.assertEqual(manager.campaign_metric.objectName(), "metricTile")
        self.assertIn("QFrame#metricRail > QWidget#metricTile", qss)
        self.assertNotIn("QFrame#metricRail QWidget", qss)
        for selector in (
            "QPushButton:focus",
            "QCheckBox:focus",
            "QListWidget:focus",
        ):
            self.assertIn(selector, qss)

    def test_history_event_notification_does_not_rebuild_history_twice(self) -> None:
        manager = self.make_manager()
        event = HistoryEvent(
            at="2026-01-01T00:00:00Z",
            kind="watch.started",
        )
        with patch.object(manager, "history_changed") as history_changed, patch.object(
            manager._notifications,
            "handle",
            return_value=None,
        ):
            manager.on_history_event(event)

        history_changed.assert_not_called()

    def test_help_repository_action_uses_fork_url(self) -> None:
        manager = self.make_manager()
        repository = next(
            button
            for button in manager.help_page.findChildren(QPushButton)
            if button.text() == _("gui", "text", "open_repository")
        )
        with patch("gui_qt.subs.webopen") as open_url:
            repository.click()
        open_url.assert_called_once_with(PROJECT_URL)

    def test_token_invalidation_is_single_flight(self) -> None:
        async def exercise() -> None:
            manager = self.make_manager()
            started = asyncio.Event()
            release = asyncio.Event()

            async def invalidate() -> None:
                started.set()
                await release.wait()

            manager.help._invalidate_token = invalidate  # type: ignore[method-assign]
            manager.help.invalidate_token()
            task = manager.help._invalidate_task
            manager.help.invalidate_token()
            await started.wait()

            self.assertIs(manager.help._invalidate_task, task)
            self.assertEqual(len(manager._tasks._tasks), 1)
            release.set()
            assert task is not None
            await task

        asyncio.run(exercise())

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

    def test_cancelled_close_race_drains_the_wrapped_child(self) -> None:
        manager = self.make_manager()

        async def exercise() -> None:
            started = asyncio.Event()
            finalized = asyncio.Event()

            async def work() -> None:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.set()

            waiter = asyncio.create_task(manager.coro_unless_closed(work()))
            await started.wait()
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.assertTrue(finalized.is_set())

        asyncio.run(exercise())

    def test_completed_work_error_wins_simultaneous_close_race(self) -> None:
        manager = self.make_manager()

        async def exercise() -> None:
            async def fail() -> None:
                raise RuntimeError("work failed")

            manager._close_requested.set()
            with self.assertRaisesRegex(RuntimeError, "work failed"):
                await manager.coro_unless_closed(fail())

        asyncio.run(exercise())

    def test_qt_task_registry_cancels_and_drains(self) -> None:
        manager = self.make_manager()

        async def exercise() -> None:
            started = asyncio.Event()
            finalized = asyncio.Event()

            async def work() -> None:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.set()

            task = manager._tasks.create(work())
            await started.wait()
            await manager._tasks.cancel_and_wait()
            self.assertTrue(task.done())
            self.assertTrue(finalized.is_set())
            self.assertEqual(manager._tasks._tasks, set())

        asyncio.run(exercise())

    def test_websocket_ignores_malformed_text(self) -> None:
        socket = Websocket.__new__(Websocket)
        socket._idx = 0
        socket._ws = AwaitableValue()
        socket._ws.set(cast(Any, FakeWebSocket()))

        async def receive_messages() -> None:
            with self.assertRaises(WebsocketClosed):
                await socket._gather_recv([], timeout=1)

        asyncio.run(receive_messages())

    def test_image_hash_is_collision_resistant_content_identity(self) -> None:
        self.assertIsNone(QtImageCache._safe_hash("../outside-cache"))
        self.assertIsNone(QtImageCache._safe_hash("not-a-sha256"))
        self.assertIsNotNone(QtImageCache._safe_hash("a" * 64 + ".png"))
        red = Image.new("RGB", (8, 8), (255, 0, 0))
        blue = Image.new("RGB", (8, 8), (0, 0, 255))
        self.assertNotEqual(QtImageCache._hash(red), QtImageCache._hash(blue))

    def test_disk_decode_rejects_oversized_files_before_image_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.png"
            path.write_bytes(b"x" * (QtImageCache.MAX_IMAGE_BYTES + 1))
            self.assertIsNone(QtImageCache._decode_file(path))

    def test_streaming_image_read_stops_at_the_size_limit(self) -> None:
        class Content:
            async def iter_chunked(self, _size: int):
                yield b"123"
                yield b"456"

        response = SimpleNamespace(content_length=None, content=Content())

        async def exercise() -> None:
            with patch.object(QtImageCache, "MAX_IMAGE_BYTES", 5):
                self.assertIsNone(await QtImageCache._read_response(response))

        asyncio.run(exercise())

    def test_image_memory_caches_use_lru_bounds(self) -> None:
        cache = QtImageCache.__new__(QtImageCache)
        cache._images = OrderedDict()
        cache._pixmaps = OrderedDict()

        with patch.object(QtImageCache, "MAX_MEMORY_IMAGES", 2), patch.object(
            QtImageCache,
            "MAX_MEMORY_PIXMAPS",
            2,
        ):
            for index in range(3):
                cache._remember_image(
                    f"image-{index}",
                    Image.new("RGB", (1, 1), (index, index, index)),
                )
                cache._remember_pixmap(
                    (f"image-{index}", (1, 1)),
                    cast(Any, object()),
                )

        self.assertEqual(list(cache._images), ["image-1", "image-2"])
        self.assertEqual(
            list(cache._pixmaps),
            [("image-1", (1, 1)), ("image-2", (1, 1))],
        )

        cache._images.clear()
        with patch.object(QtImageCache, "MAX_DECODED_IMAGE_BYTES", 5):
            cache._remember_image("first", Image.new("RGB", (1, 1)))
            cache._remember_image("second", Image.new("RGB", (1, 1)))
        self.assertEqual(list(cache._images), ["second"])

    def test_image_cache_rejects_untrusted_urls_and_oversized_dimensions(self) -> None:
        self.assertTrue(
            QtImageCache._trusted_remote_url(
                "https://static-cdn.jtvnw.net/reward.png"
            )
        )
        for url in (
            "http://static-cdn.jtvnw.net/reward.png",
            "https://127.0.0.1/reward.png",
            "https://user@static-cdn.jtvnw.net/reward.png",
            "https://static-cdn.jtvnw.net:8443/reward.png",
        ):
            with self.subTest(url=url):
                self.assertFalse(QtImageCache._trusted_remote_url(url))

        image = Image.new("RGB", (11, 10))
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        with patch.object(QtImageCache, "MAX_IMAGE_PIXELS", 100):
            self.assertIsNone(QtImageCache._decode(payload.getvalue()))

    def test_image_cache_deduplicates_requests_and_does_not_cache_failures(self) -> None:
        class Response:
            status = 200
            headers: dict[str, str] = {}

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
                self.transport = self

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
                        cache.get("https://static-cdn.jtvnw.net/image.png", (64, 64)),
                        cache.get("https://static-cdn.jtvnw.net/image.png", (32, 32)),
                    )
                    self.assertFalse(first.isNull())
                    self.assertFalse(second.isNull())
                    self.assertEqual(valid_twitch.calls, 1)
                    cache.save()
                    self.assertTrue(mapping_path.exists())

                    write_failed_cache = QtImageCache(valid_twitch)
                    with patch.object(
                        write_failed_cache,
                        "_cache_file",
                        side_effect=OSError("read-only cache"),
                    ):
                        uncached = await write_failed_cache.get(
                            "https://static-cdn.jtvnw.net/uncached.png",
                            (32, 32),
                        )
                    self.assertFalse(uncached.isNull())
                    self.assertFalse(write_failed_cache._disk_enabled)
                    write_failed_cache.save(force=True)

                    failed_cache = QtImageCache(invalid_twitch)
                    failed = await failed_cache.get("https://static-cdn.jtvnw.net/bad.png", (32, 32))
                    retried = await failed_cache.get("https://static-cdn.jtvnw.net/bad.png", (32, 32))
                    self.assertFalse(failed.isNull())
                    self.assertFalse(retried.isNull())
                    self.assertEqual(invalid_twitch.calls, 2)
                    self.assertNotIn(
                        "https://static-cdn.jtvnw.net/bad.png",
                        failed_cache._hashes,
                    )

        asyncio.run(exercise())

    def test_image_cache_falls_back_when_cache_directory_is_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache"
            cache_path.write_text("not a directory", encoding="utf8")
            mapping_path = cache_path / "mapping.json"
            with patch("gui_qt.image_cache.CACHE_PATH", cache_path), patch(
                "gui_qt.image_cache.CACHE_DB", mapping_path
            ):
                cache = QtImageCache(cast(Any, object()))
                self.assertFalse(cache._disk_enabled)
                cache.save(force=True)

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
