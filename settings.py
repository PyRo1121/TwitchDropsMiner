from __future__ import annotations

import logging
from typing import Any, Mapping, TypedDict, Protocol

from yarl import URL

from utils import json_load, json_save, quarantine_file
from constants import SETTINGS_PATH, DEFAULT_LANG, PriorityMode

logger = logging.getLogger("TwitchDrops")


class CliArgs(Protocol):
    """Structural shape of the parsed CLI args that Settings reads.

    Satisfied by the ``qt_main.ParsedArgs`` launcher.
    """

    log: bool
    tray: bool
    dump: bool
    allow_insecure_oauth_file: bool

    @property
    def debug_ws(self) -> int: ...

    @property
    def debug_gql(self) -> int: ...

    @property
    def logging_level(self) -> int: ...


class SettingsFile(TypedDict):
    proxy: URL
    language: str
    dark_mode: bool
    exclude: set[str]
    priority: list[str]
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    history_retention_days: int
    enable_badges_emotes: bool
    available_drops_check: bool
    experimental_dual_watch: bool
    priority_mode: PriorityMode


default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "dark_mode": True,
    "autostart_tray": False,
    "connection_quality": 1,
    "language": DEFAULT_LANG,
    "tray_notifications": True,
    "history_retention_days": 90,
    "enable_badges_emotes": False,
    "available_drops_check": False,
    "experimental_dual_watch": False,
    "priority_mode": PriorityMode.PRIORITY_ONLY,
}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        return default
    return min(maximum, max(minimum, value))


def _game_names(value: Any, *, ordered: bool) -> list[str] | set[str]:
    expected_type = list if ordered else set
    if type(value) is not expected_type:
        return [] if ordered else set()
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not (name := item.strip()) or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names if ordered else set(names)


def parse_proxy(value: str | URL) -> URL:
    try:
        proxy = value if isinstance(value, URL) else URL(value)
        if not proxy:
            return URL()
        port = proxy.port
        if (
            proxy.scheme not in {"http", "https"}
            or proxy.host is None
            or port is None
            or not 1 <= port <= 65535
            or proxy.path not in {"", "/"}
            or bool(proxy.query_string)
            or bool(proxy.fragment)
        ):
            raise ValueError("Proxy URL must be an HTTP(S) origin with an explicit port")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid proxy URL") from exc
    return proxy


def _proxy(value: Any) -> URL:
    if not isinstance(value, URL):
        return URL()
    try:
        return parse_proxy(value)
    except ValueError:
        return URL()


def _validated_settings(raw: Mapping[str, Any]) -> SettingsFile:
    language = raw.get("language")
    if not isinstance(language, str) or not language.strip():
        language = DEFAULT_LANG

    priority_mode = raw.get("priority_mode")
    if not isinstance(priority_mode, PriorityMode):
        priority_mode = PriorityMode.PRIORITY_ONLY

    def boolean(name: str) -> bool:
        value = raw.get(name)
        default = default_settings[name]  # type: ignore[literal-required]
        return value if type(value) is bool else bool(default)

    return {
        "proxy": _proxy(raw.get("proxy")),
        "language": language,
        "dark_mode": boolean("dark_mode"),
        "exclude": set(_game_names(raw.get("exclude"), ordered=False)),
        "priority": list(_game_names(raw.get("priority"), ordered=True)),
        "autostart_tray": boolean("autostart_tray"),
        "connection_quality": _bounded_int(
            raw.get("connection_quality"),
            default_settings["connection_quality"],
            1,
            6,
        ),
        "tray_notifications": boolean("tray_notifications"),
        "history_retention_days": _bounded_int(
            raw.get("history_retention_days"),
            default_settings["history_retention_days"],
            1,
            3650,
        ),
        "enable_badges_emotes": boolean("enable_badges_emotes"),
        "available_drops_check": boolean("available_drops_check"),
        "experimental_dual_watch": boolean("experimental_dual_watch"),
        "priority_mode": priority_mode,
    }


class Settings:
    # from args
    log: bool
    tray: bool
    dump: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: int
    # from settings file
    proxy: URL
    language: str
    dark_mode: bool
    exclude: set[str]
    priority: list[str]
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    history_retention_days: int
    enable_badges_emotes: bool
    available_drops_check: bool
    experimental_dual_watch: bool
    priority_mode: PriorityMode

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: CliArgs):
        load_failed = False
        try:
            loaded: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        except ValueError as exc:
            try:
                quarantined = quarantine_file(SETTINGS_PATH, reason="invalid")
            except OSError as quarantine_exc:
                raise ValueError("Unable to preserve invalid settings") from quarantine_exc
            logger.warning(
                "Invalid settings were quarantined at %s (%s)",
                quarantined,
                type(exc).__name__,
            )
            loaded = default_settings
            load_failed = True
        validated = _validated_settings(loaded)
        self._settings: SettingsFile = validated
        self._args: CliArgs = args
        self._altered: bool = load_failed or validated != loaded

    # default logic of reading settings is to check args first, then the settings file
    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            # passthrough
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            # passthrough
            return super().__setattr__(name, value)
        elif name in self._settings:
            candidate = dict(self._settings)
            candidate[name] = value
            validated = _validated_settings(candidate)
            normalized = validated[name]  # type: ignore[literal-required]
            if type(normalized) is not type(value) or normalized != value:
                raise ValueError(f"Invalid value for setting: {name}")
            self._settings[name] = normalized  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True

    def save(self, *, force: bool = False) -> None:
        if not (self._altered or force):
            return
        try:
            json_save(SETTINGS_PATH, self._settings, sort=True)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "Unable to persist settings: %s",
                type(exc).__name__,
            )
        else:
            self._altered = False
