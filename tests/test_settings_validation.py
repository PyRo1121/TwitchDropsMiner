from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from constants import PriorityMode
from settings import Settings, parse_proxy
from yarl import URL


class _Args:
    log = False
    tray = False
    dump = False
    allow_insecure_oauth_file = False

    @property
    def debug_ws(self) -> int:
        return 20

    @property
    def debug_gql(self) -> int:
        return 20

    @property
    def logging_level(self) -> int:
        return 20


class SettingsValidationTests(unittest.TestCase):
    def test_proxy_parser_accepts_only_http_origins_with_explicit_ports(self) -> None:
        self.assertEqual(
            str(parse_proxy("https://proxy.example:8443")),
            "https://proxy.example:8443",
        )
        self.assertFalse(parse_proxy(""))
        for value in (
            "ftp://proxy.example:8080",
            "//proxy.example:8080",
            "http://proxy.example:0",
            "http://proxy.example:8080/path",
            "http://proxy.example:8080?query=yes",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_proxy(value)

    def test_semantic_values_are_normalized_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "proxy": {
                            "__type": "URL",
                            "data": "ftp://proxy.example:21",
                        },
                        "priority": [" Game ", "Game", 4, ""],
                        "exclude": {
                            "__type": "set",
                            "data": [" Other Game ", 5],
                        },
                        "connection_quality": 99,
                        "history_retention_days": 0,
                        "language": " ",
                    }
                ),
                encoding="utf8",
            )
            with patch("settings.SETTINGS_PATH", path):
                settings = Settings(_Args())

        self.assertEqual(settings.proxy, URL())
        self.assertEqual(settings.priority, ["Game"])
        self.assertEqual(settings.exclude, {"Other Game"})
        self.assertEqual(settings.connection_quality, 6)
        self.assertEqual(settings.history_retention_days, 1)
        self.assertEqual(settings.language, "English")
        self.assertTrue(settings._altered)

    def test_corrupt_settings_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("not json", encoding="utf8")
            with patch("settings.SETTINGS_PATH", path):
                settings = Settings(_Args())
            quarantined = list(path.parent.glob("settings.json.invalid-*"))
            self.assertFalse(path.exists())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf8"), "not json")

        self.assertEqual(settings.priority, [])
        self.assertEqual(settings.exclude, set())
        self.assertEqual(settings.priority_mode, PriorityMode.PRIORITY_ONLY)
        self.assertFalse(settings.experimental_dual_watch)
        self.assertTrue(settings._altered)

    def test_runtime_updates_reject_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch("settings.SETTINGS_PATH", path):
                settings = Settings(_Args())

                with self.assertRaisesRegex(ValueError, "connection_quality"):
                    settings.connection_quality = 0
                with self.assertRaisesRegex(ValueError, "priority"):
                    settings.priority = ["Game", "Game"]
                with self.assertRaisesRegex(ValueError, "proxy"):
                    settings.proxy = URL("ftp://proxy.example:21")

    def test_failed_save_keeps_altered_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch("settings.SETTINGS_PATH", path), patch(
                "settings.json_save",
                side_effect=OSError("read-only"),
            ):
                settings = Settings(_Args())
                settings.priority = ["Game"]
                settings.save()

        self.assertTrue(settings._altered)

    def test_successful_save_clears_altered_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch("settings.SETTINGS_PATH", path):
                settings = Settings(_Args())
                settings.priority = ["Game"]
                settings.save()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        self.assertFalse(settings._altered)


if __name__ == "__main__":
    unittest.main()
