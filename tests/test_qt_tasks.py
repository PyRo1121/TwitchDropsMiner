from __future__ import annotations

import asyncio
import unittest

from gui_qt.tasks import QtTaskRegistry


class QtTaskRegistryTests(unittest.TestCase):
    def test_cancel_all_cancels_owned_tasks(self) -> None:
        async def exercise() -> None:
            registry = QtTaskRegistry()
            task = registry.create(asyncio.sleep(60))
            await asyncio.sleep(0)

            registry.cancel_all()
            await asyncio.gather(task, return_exceptions=True)

            self.assertTrue(task.cancelled())

        asyncio.run(exercise())

    def test_closed_registry_rejects_new_tasks(self) -> None:
        async def exercise() -> None:
            registry = QtTaskRegistry()
            await registry.cancel_and_wait()

            with self.assertRaisesRegex(RuntimeError, "closed"):
                registry.create(asyncio.sleep(0))

        asyncio.run(exercise())

    def test_cancel_finalizer_cannot_escape_registry_ownership(self) -> None:
        async def exercise() -> None:
            registry = QtTaskRegistry()
            started = asyncio.Event()
            finalized = asyncio.Event()

            async def child() -> None:
                await asyncio.sleep(0)

            async def parent() -> None:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    with self.assertRaisesRegex(RuntimeError, "closed"):
                        registry.create(child())
                    finalized.set()

            task = registry.create(parent())
            await started.wait()
            await registry.cancel_and_wait()

            self.assertTrue(task.cancelled())
            self.assertTrue(finalized.is_set())
            self.assertEqual(registry._tasks, set())

        asyncio.run(exercise())

    def test_task_failures_are_logged_and_removed(self) -> None:
        async def fail() -> None:
            raise RuntimeError("boom")

        async def exercise() -> None:
            registry = QtTaskRegistry()
            with self.assertLogs("TwitchDrops.ui", level="ERROR") as logs:
                task = registry.create(fail())
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0)

            self.assertTrue(any("Qt task failed: RuntimeError" in entry for entry in logs.output))

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
