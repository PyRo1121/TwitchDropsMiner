from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast

from inventory import TimedDrop


class InventoryProgressTests(unittest.TestCase):
    def test_stale_authoritative_progress_cannot_rewind_a_drop(self) -> None:
        updates: list[int] = []
        drop = cast(Any, TimedDrop.__new__(TimedDrop))
        drop.real_current_minutes = 5
        drop.required_minutes = 10
        drop.campaign = SimpleNamespace(_bump_all_minutes=updates.append)

        drop.update_minutes(3)
        self.assertEqual(updates, [])
        drop.update_minutes(8)
        self.assertEqual(updates, [3])


if __name__ == "__main__":
    unittest.main()
