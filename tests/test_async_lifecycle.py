from __future__ import annotations

import asyncio
import unittest

from utils import cancel_tasks


class AsyncLifecycleTests(unittest.TestCase):
    def test_cancel_tasks_waits_for_cancellation(self) -> None:
        async def exercise() -> None:
            task = asyncio.create_task(asyncio.sleep(60))
            await asyncio.sleep(0)
            await cancel_tasks([task])
            self.assertTrue(task.cancelled())

        asyncio.run(exercise())

    def test_cancel_tasks_consumes_task_errors(self) -> None:
        async def fail() -> None:
            raise RuntimeError("expected")

        async def exercise() -> None:
            task = asyncio.create_task(fail())
            await asyncio.sleep(0)
            await cancel_tasks([task])
            self.assertIsInstance(task.exception(), RuntimeError)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
