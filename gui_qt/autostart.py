"""Cross-platform login-at-startup integration for the Qt settings page."""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from constants import IS_PACKAGED, LOGGING_LEVELS, SELF_PATH
from utils import atomic_write


class AutostartError(RuntimeError):
    pass


class AutostartManager:
    NAME = "TwitchDropsMiner"
    WINDOWS_KEY = "HKCU/Software/Microsoft/Windows/CurrentVersion/Run"
    MAC_LABEL = "com.devilxd.twitchdropsminer"

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def _command(self, tray: bool) -> list[str]:
        if IS_PACKAGED:
            command = [str(SELF_PATH.resolve())]
        else:
            command = [sys.executable, str(SELF_PATH.resolve())]
        level = self._settings.logging_level
        for index, value in LOGGING_LEVELS.items():
            if value == level and index:
                command.append("-" + "v" * index)
                break
        if tray:
            command.append("--tray")
        return command

    def _command_line(self, tray: bool) -> str:
        return subprocess.list2cmdline(self._command(tray)) if sys.platform == "win32" else shlex.join(self._command(tray))

    @staticmethod
    def _desktop_quote(value: str) -> str:
        # Desktop Entry Exec values treat percent sequences as field codes and
        # reserve shell-like punctuation even though no shell is involved.
        escaped = value.replace("%", "%%")
        reserved = set(" \t\n\"'\\><~|&;$*?#()`")
        if not escaped or any(char in reserved for char in escaped):
            escaped = (
                escaped.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("`", "\\`")
                .replace("$", "\\$")
            )
            return f'"{escaped}"'
        return escaped

    @staticmethod
    def _desktop_string(value: str) -> str:
        """Serialize a plain Desktop Entry string, not an Exec argument."""
        if any(character in value for character in ("\0", "\n", "\r")):
            raise ValueError("Desktop Entry strings cannot contain control characters")
        return value.replace("\\", "\\\\")

    @classmethod
    def _desktop_command_line(cls, command: list[str]) -> str:
        return " ".join(cls._desktop_quote(value) for value in command)

    def linux_path(self) -> Path:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
        return root / "autostart" / f"{self.NAME}.desktop"

    def mac_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.MAC_LABEL}.plist"

    def is_enabled(self) -> bool:
        try:
            if sys.platform == "win32":
                from registry import RegistryKey, ValueNotFound

                with RegistryKey(self.WINDOWS_KEY, read_only=True) as key:
                    try:
                        _, value = key.get(self.NAME)
                    except ValueNotFound:
                        return False
                return str(SELF_PATH.resolve()) in str(value)
            if sys.platform.startswith("linux"):
                path = self.linux_path()
                if not path.exists():
                    return False
                fields = dict(
                    line.split("=", 1)
                    for line in path.read_text(encoding="utf8").splitlines()
                    if "=" in line
                )
                executable = self._desktop_string(self._command(False)[0])
                return fields.get("TryExec") == executable
            if sys.platform == "darwin":
                path = self.mac_path()
                if not path.exists():
                    return False
                with path.open("rb") as file:
                    payload = plistlib.load(file)
                arguments = payload.get("ProgramArguments")
                return (
                    isinstance(arguments, list)
                    and bool(arguments)
                    and arguments[0] == self._command(False)[0]
                )
        except (OSError, ValueError, TypeError):
            return False
        return False

    def set_enabled(self, enabled: bool, *, tray: bool) -> None:
        try:
            if sys.platform == "win32":
                from registry import RegistryKey, ValueType

                with RegistryKey(self.WINDOWS_KEY) as key:
                    if enabled:
                        key.set(self.NAME, ValueType.REG_SZ, self._command_line(tray))
                    else:
                        key.delete(self.NAME, silent=True)
                return
            if sys.platform.startswith("linux"):
                path = self.linux_path()
                if enabled:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    command = self._command(tray)
                    desktop = "\n".join(
                        [
                            "[Desktop Entry]",
                            "Type=Application",
                            f"Name={self.NAME}",
                            "Comment=Mine timed Drops on Twitch",
                            f"TryExec={self._desktop_string(command[0])}",
                            f"Exec={self._desktop_command_line(command)}",
                            "Terminal=false",
                            "X-GNOME-Autostart-enabled=true",
                            "",
                        ]
                    )
                    def write_desktop(file: Any) -> None:
                        file.write(desktop)

                    atomic_write(path, write_desktop)
                else:
                    path.unlink(missing_ok=True)
                return
            if sys.platform == "darwin":
                path = self.mac_path()
                if enabled:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    plist = {
                        "Label": self.MAC_LABEL,
                        "ProgramArguments": self._command(tray),
                        "RunAtLoad": True,
                        "ProcessType": "Interactive",
                    }
                    with path.open("wb") as file:
                        plistlib.dump(plist, file)
                else:
                    path.unlink(missing_ok=True)
                return
        except (OSError, ValueError, TypeError) as exc:
            raise AutostartError(str(exc)) from exc
        raise AutostartError(f"Autostart is unsupported on {sys.platform}")
