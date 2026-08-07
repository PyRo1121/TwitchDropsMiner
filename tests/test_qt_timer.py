from __future__ import annotations

import unittest
from typing import Any, cast

from PySide6.QtWidgets import QApplication, QWidget

from gui_qt.subs import QtCampaignProgress


class _HeroStub(QWidget):
    def __init__(self, updates: list[dict[str, str]]) -> None:
        super().__init__()
        self._updates = updates

    def set_remaining(self, **values: str) -> None:
        self._updates.append(values)


class QtCampaignProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_native_timer_stops_after_countdown_reaches_zero(self) -> None:
        updates: list[dict[str, str]] = []
        hero = cast(Any, _HeroStub(updates))
        progress = QtCampaignProgress(hero)
        progress._seconds = 2
        progress._timer.start()

        progress._tick()
        self.assertEqual(progress._seconds, 1)
        self.assertTrue(progress._timer.isActive())

        progress._tick()
        self.assertEqual(progress._seconds, 0)
        self.assertFalse(progress._timer.isActive())
        self.assertEqual(len(updates), 2)


if __name__ == "__main__":
    unittest.main()
