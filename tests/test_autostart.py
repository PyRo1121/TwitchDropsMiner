from __future__ import annotations

import plistlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gui_qt.autostart import AutostartManager


class AutostartTests(unittest.TestCase):
    def manager(self) -> AutostartManager:
        return AutostartManager(SimpleNamespace(logging_level=40))

    def test_desktop_exec_escapes_field_codes_and_reserved_characters(self) -> None:
        command = AutostartManager._desktop_command_line(
            ["/opt/Twitch Miner/%build", "value$with`syntax", "--tray"]
        )

        self.assertIn('"/opt/Twitch Miner/%%build"', command)
        self.assertIn('"value\\$with\\`syntax"', command)
        self.assertNotIn("/%build", command)

    def test_linux_desktop_round_trip_uses_plain_try_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            desktop_path = Path(directory) / "TwitchDropsMiner.desktop"
            manager = self.manager()
            with patch("gui_qt.autostart.sys.platform", "linux"), patch(
                "gui_qt.autostart.IS_PACKAGED", True
            ), patch(
                "gui_qt.autostart.SELF_PATH",
                Path(directory) / "Twitch Miner %dev",
            ), patch.object(
                manager,
                "linux_path",
                return_value=desktop_path,
            ):
                manager.set_enabled(True, tray=True)
                content = desktop_path.read_text(encoding="utf8")
                fields = dict(
                    line.split("=", 1)
                    for line in content.splitlines()
                    if "=" in line
                )
                expected = str((Path(directory) / "Twitch Miner %dev").resolve())
                self.assertEqual(fields["TryExec"], expected)
                self.assertNotIn('TryExec="', content)
                self.assertIn("%%dev", fields["Exec"])
                self.assertIn("--tray", fields["Exec"])
                self.assertTrue(manager.is_enabled())

                manager.set_enabled(False, tray=True)
                self.assertFalse(desktop_path.exists())

    def test_macos_launch_agent_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plist_path = Path(directory) / "miner.plist"
            executable = Path(directory) / "Twitch Drops Miner.app"
            manager = self.manager()
            with patch("gui_qt.autostart.sys.platform", "darwin"), patch(
                "gui_qt.autostart.IS_PACKAGED", True
            ), patch(
                "gui_qt.autostart.SELF_PATH", executable
            ), patch.object(
                manager,
                "mac_path",
                return_value=plist_path,
            ):
                manager.set_enabled(True, tray=True)
                with plist_path.open("rb") as file:
                    payload = plistlib.load(file)
                self.assertEqual(
                    payload["ProgramArguments"],
                    [str(executable.resolve()), "--tray"],
                )
                self.assertTrue(manager.is_enabled())

                manager.set_enabled(False, tray=True)
                self.assertFalse(plist_path.exists())

    def test_windows_registry_round_trip(self) -> None:
        values: dict[str, str] = {}

        class ValueNotFound(Exception):
            pass

        class RegistryKey:
            def __init__(self, _path: str, *, read_only: bool = False) -> None:
                self.read_only = read_only

            def __enter__(self) -> RegistryKey:
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_value: BaseException | None,
                traceback: types.TracebackType | None,
            ) -> None:
                del exc_type, exc_value, traceback

            def get(self, name: str):
                if name not in values:
                    raise ValueNotFound(name)
                return object(), values[name]

            def set(self, name: str, _kind: object, value: str) -> bool:
                values[name] = value
                return True

            def delete(self, name: str, *, silent: bool = False) -> bool:
                del silent
                return values.pop(name, None) is not None

        registry = types.ModuleType("registry")
        setattr(registry, "RegistryKey", RegistryKey)
        setattr(registry, "ValueNotFound", ValueNotFound)
        setattr(registry, "ValueType", SimpleNamespace(REG_SZ=object()))
        executable = Path("C:/Program Files/Twitch Drops Miner/miner.exe")
        manager = self.manager()

        with patch.dict(sys.modules, {"registry": registry}), patch(
            "gui_qt.autostart.sys.platform", "win32"
        ), patch("gui_qt.autostart.IS_PACKAGED", True), patch(
            "gui_qt.autostart.SELF_PATH", executable
        ):
            manager.set_enabled(True, tray=True)
            self.assertIn('"', values[manager.NAME])
            self.assertIn("--tray", values[manager.NAME])
            self.assertTrue(manager.is_enabled())

            manager.set_enabled(False, tray=True)
            self.assertFalse(manager.is_enabled())


if __name__ == "__main__":
    unittest.main()
