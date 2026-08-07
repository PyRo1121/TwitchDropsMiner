from __future__ import annotations

import unittest

from gui_qt.autostart import AutostartManager


class DesktopEntryCommandTests(unittest.TestCase):
    def test_quotes_paths_using_desktop_entry_syntax(self) -> None:
        command = ["/home/user/Twitch Drops Miner/python", "--tray"]

        self.assertEqual(
            '"/home/user/Twitch Drops Miner/python" --tray',
            AutostartManager._desktop_command_line(command),
        )


if __name__ == "__main__":
    unittest.main()
