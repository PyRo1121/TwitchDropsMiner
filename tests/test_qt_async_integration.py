from __future__ import annotations

import asyncio
import unittest

import qasync
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


class QtAsyncIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_qasync_loop_drives_qtimer_and_coroutine(self) -> None:
        async def exercise() -> None:
            fired = asyncio.Event()
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(fired.set)
            timer.start(0)
            await asyncio.wait_for(fired.wait(), timeout=1)
            timer.deleteLater()

        loop = qasync.QEventLoop(self.app)
        asyncio.set_event_loop(loop)
        try:
            with loop:
                loop.run_until_complete(exercise())
        finally:
            asyncio.set_event_loop(None)


if __name__ == "__main__":
    unittest.main()
