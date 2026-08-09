"""Ownership for asyncio tasks created by the Qt presentation layer."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

from utils import cancel_tasks

_T = TypeVar("_T")
logger = logging.getLogger("TwitchDrops.ui")


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
