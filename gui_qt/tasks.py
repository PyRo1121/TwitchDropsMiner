"""Ownership for asyncio tasks and live Qt presentation resources."""
from __future__ import annotations

import asyncio
import logging
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
    """Queue log delivery onto the QApplication thread."""

    message = Signal(str)

    def __init__(self, console: QtConsole) -> None:
        super().__init__()
        self._console = console
        self.message.connect(
            self._deliver,
            Qt.ConnectionType.QueuedConnection,
        )

    @Slot(str)
    def _deliver(self, message: str) -> None:
        self._console.print(message)


class QtLogHandler(logging.Handler):
    """Thread-safe logging bridge for widget-backed activity consoles."""

    def __init__(self, console: QtConsole) -> None:
        super().__init__()
        self._bridge = _QtLogBridge(console)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - logging's defensive contract
            self.handleError(record)
            return
        self._bridge.message.emit(message)


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
        self._stopped = False
        self._sync_closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def log_handler(self) -> QtLogHandler:
        return self._log_handler

    def start(self) -> None:
        if self._started:
            return
        if self._stopped or self._sync_closed:
            raise RuntimeError("Qt presentation runtime is closed")
        try:
            self._apply_application_theme()
            self._install_log_handler()
            self._dashboard.start()
            self._inventory_page.start()
            self._tray.start()
            if self._tray_requested and self._tray.available:
                self._window.hide()
            else:
                # A tray-only launch must expose a window if the platform has
                # no system tray (for example, a minimal desktop or CI).
                self._window.show()
        except Exception:
            failures: list[BaseException] = []
            self._quiesce(failures)
            raise
        self._started = True

    async def stop(self) -> None:
        if self._stopped:
            return
        self._started = False
        failures: list[BaseException] = []
        self._quiesce(failures)
        try:
            await self._tasks.cancel_and_wait()
        except BaseException as exc:
            app_logger.exception("Unable to drain Qt tasks")
            failures.append(exc)
        self._save_into(failures, force=False)
        self._stopped = True
        if failures:
            raise failures[0]

    def close_sync(self) -> None:
        """Best-effort fallback when the window is finally destroyed.

        Normal shutdown calls :meth:`stop` first so task finalizers are awaited.
        This fallback still stops every timer/animation and cancels task owners
        before the Qt event loop can disappear.
        """
        failures: list[BaseException] = []
        self._started = False
        self._sync_closed = True
        self._quiesce(failures)
        try:
            self._tasks.cancel_all()
        except Exception as exc:
            app_logger.exception(
                "Unable to cancel Qt tasks during window cleanup"
            )
            failures.append(exc)
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
        app_logger.addHandler(self._log_handler)
        self._handler_installed = True

    def _remove_log_handler(self) -> None:
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
