"""Ownership for asyncio tasks and live Qt presentation resources."""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QMainWindow

from constants import OUTPUT_FORMATTER
from utils import cancel_tasks

from .subs import QtCampaignProgress, QtConsole
from .tray import QtTray

if TYPE_CHECKING:
    from game_metadata import SteamMetadataProvider

    from .controllers import QtDashboardController
    from .game_context import QtGameContextController
    from .image_cache import QtImageCache
    from .pages import InventoryPage
    from .shell import QtWindowShell

_T = TypeVar("_T")
logger = logging.getLogger("TwitchDrops.ui")
app_logger = logging.getLogger("TwitchDrops")


class QtTaskRegistry:
    """Track UI tasks and cancel them together during shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def accepting(self) -> bool:
        return not self._closed

    @property
    def drained(self) -> bool:
        return self._closed and not self._tasks

    def create(self, coroutine: Coroutine[Any, Any, _T]) -> asyncio.Task[_T]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("Qt task registry is closed")
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "Qt task failed: %s",
                type(exception).__name__,
                exc_info=exception,
            )

    def cancel_all(self) -> None:
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()

    async def cancel_and_wait(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        try:
            await cancel_tasks(tasks)
        finally:
            self._tasks.difference_update(tasks)


class _QtLogBridge(QObject):
    """Queue current-generation log delivery onto the QApplication thread."""

    message = Signal(int, str)

    def __init__(self, console: QtConsole) -> None:
        super().__init__()
        self._console = console
        self._active = False
        self._generation = 0
        self._lifecycle_lock = threading.Lock()
        self.message.connect(
            self._deliver,
            Qt.ConnectionType.QueuedConnection,
        )

    @property
    def active(self) -> bool:
        with self._lifecycle_lock:
            return self._active

    def activate(self) -> None:
        with self._lifecycle_lock:
            self._generation += 1
            self._active = True

    def deactivate(self) -> None:
        # Incrementing invalidates records queued by every earlier activation,
        # even if a recoverable start later activates the bridge again.
        with self._lifecycle_lock:
            self._active = False
            self._generation += 1

    def capture_generation(self) -> int | None:
        """Snapshot this emit's immutable activation before formatting."""
        with self._lifecycle_lock:
            return self._generation if self._active else None

    def enqueue(self, generation: int, message: str) -> None:
        self.message.emit(generation, message)

    @Slot(int, str)
    def _deliver(self, generation: int, message: str) -> None:
        with self._lifecycle_lock:
            current = self._active and generation == self._generation
        if current:
            self._console.print(message)


class QtLogHandler(logging.Handler):
    """Thread-safe logging bridge for widget-backed activity consoles."""

    def __init__(self, console: QtConsole) -> None:
        super().__init__()
        self._bridge = _QtLogBridge(console)

    @property
    def active(self) -> bool:
        return self._bridge.active

    def activate(self) -> None:
        self._bridge.activate()

    def deactivate(self) -> None:
        self._bridge.deactivate()

    def emit(self, record: logging.LogRecord) -> None:
        generation = self._bridge.capture_generation()
        if generation is None:
            return
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - logging's defensive contract
            self.handleError(record)
            return
        self._bridge.enqueue(generation, message)


class QtPresentationRuntime:
    """Own all effects that exist only while the Qt presentation is running."""

    def __init__(
        self,
        window: QMainWindow,
        *,
        tasks: QtTaskRegistry,
        dashboard: QtDashboardController,
        progress: QtCampaignProgress,
        inventory_page: InventoryPage,
        game_context: QtGameContextController,
        shell: QtWindowShell,
        tray: QtTray,
        metadata: SteamMetadataProvider,
        image_cache: QtImageCache,
        console: QtConsole,
        tray_requested: bool,
        apply_application_theme: Callable[[], None],
    ) -> None:
        self._window = window
        self._tasks = tasks
        self._dashboard = dashboard
        self._progress = progress
        self._inventory_page = inventory_page
        self._game_context = game_context
        self._shell = shell
        self._tray = tray
        self._metadata = metadata
        self._image_cache = image_cache
        self._tray_requested = tray_requested
        self._apply_application_theme = apply_application_theme
        self._log_handler = QtLogHandler(console)
        self._log_handler.setFormatter(OUTPUT_FORMATTER)
        self._handler_installed = False
        self._started = False
        self._stopping = False
        self._stopped = False
        self._sync_closed = False
        self._stop_complete = asyncio.Event()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def log_handler(self) -> QtLogHandler:
        return self._log_handler

    @property
    def accepting_actions(self) -> bool:
        return (
            not self._stopping
            and not self._stopped
            and not self._sync_closed
            and self._tasks.accepting
        )

    def _set_interactive(self, enabled: bool) -> None:
        central = self._window.centralWidget()
        if central is not None:
            central.setEnabled(enabled)
        self._shell.set_interactive(enabled)

    def _begin_shutdown(self) -> None:
        # This method must remain synchronous: no user action may race the
        # registry closing or any cancellation/persistence await below.
        self._stopping = True
        self._started = False
        self._set_interactive(False)
        self._window.hide()

    def start(self) -> None:
        if self._started:
            return
        if self._stopping or self._stopped or self._sync_closed:
            raise RuntimeError("Qt presentation runtime is closed")
        central = self._window.centralWidget()
        was_enabled = central is None or central.isEnabled()
        was_visible = self._window.isVisible()
        try:
            self._apply_application_theme()
            self._install_log_handler()
            self._dashboard.start()
            self._inventory_page.start()
            self._tray.start()
            self._set_interactive(True)
            if self._tray_requested and self._tray.available:
                self._window.hide()
            else:
                # A tray-only launch must expose a window if the platform has
                # no system tray (for example, a minimal desktop or CI).
                self._window.show()
        except Exception:
            failures: list[BaseException] = []
            self._quiesce(failures)
            self._set_interactive(was_enabled)
            if was_visible:
                self._window.show()
            else:
                self._window.hide()
            raise
        self._started = True

    async def stop(self) -> None:
        if self._stopped:
            return
        if self._stopping:
            await self._stop_complete.wait()
            return

        self._begin_shutdown()
        failures: list[BaseException] = []
        self._quiesce(failures)
        try:
            await self._tasks.cancel_and_wait()
        except BaseException as exc:
            app_logger.exception("Unable to drain Qt tasks")
            failures.append(exc)
        self._save_into(failures, force=False)
        self._stopped = True
        self._stopping = False
        self._stop_complete.set()
        if failures:
            raise failures[0]

    def close_sync(self) -> None:
        """Force-save and release widgets after asynchronous task drainage."""
        if self._sync_closed:
            return
        if not self._stopped or not self._tasks.drained:
            raise RuntimeError(
                "Qt presentation must be stopped and drained before window close"
            )
        failures: list[BaseException] = []
        self._sync_closed = True
        self._quiesce(failures)
        self._save_into(failures, force=True)
        for failure in failures:
            app_logger.warning(
                "Qt window cleanup stage failed: %s",
                type(failure).__name__,
            )

    def save(self, *, force: bool = False) -> None:
        failures: list[BaseException] = []
        self._save_into(failures, force=force)
        if failures:
            raise failures[0]

    def _install_log_handler(self) -> None:
        if self._handler_installed:
            return
        self._log_handler.activate()
        app_logger.addHandler(self._log_handler)
        self._handler_installed = True

    def _remove_log_handler(self) -> None:
        # Deactivate first so records already queued in Qt are stale before the
        # Python logger can lose its final reference to this handler.
        self._log_handler.deactivate()
        if not self._handler_installed:
            return
        app_logger.removeHandler(self._log_handler)
        self._handler_installed = False

    def _quiesce(self, failures: list[BaseException]) -> None:
        stages: tuple[tuple[str, Callable[[], None]], ...] = (
            ("dashboard timer", self._dashboard.stop),
            ("campaign progress timer", self._progress.stop_timer),
            ("inventory timer", self._inventory_page.stop),
            ("game context", self._game_context.stop),
            ("page animation", self._shell.stop),
            ("logging bridge", self._remove_log_handler),
            ("system tray", self._tray.stop),
        )
        for label, stage in stages:
            try:
                stage()
            except Exception as exc:
                app_logger.exception("Unable to stop Qt %s", label)
                failures.append(exc)

    def _save_into(
        self,
        failures: list[BaseException],
        *,
        force: bool,
    ) -> None:
        stages: tuple[tuple[str, Callable[..., None]], ...] = (
            ("Steam metadata", self._metadata.save),
            ("image cache", self._image_cache.save),
        )
        for label, stage in stages:
            try:
                stage(force=force)
            except Exception as exc:
                app_logger.exception("Unable to save Qt %s", label)
                failures.append(exc)
