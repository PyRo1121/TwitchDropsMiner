from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from math import inf
from pathlib import Path
from typing import Any
from unittest.mock import patch

from yarl import URL

from constants import (
    CACHE_DB,
    CACHE_PATH,
    COOKIES_PATH,
    DATA_DIR,
    DUMP_PATH,
    HISTORY_PATH,
    LANG_PATH,
    LOCK_PATH,
    LOG_PATH,
    OAUTH_TOKEN_PATH,
    SETTINGS_PATH,
    WORKING_DIR,
    WebsocketTopic,
    _user_data_dir,
)
from utils import (
    atomic_write,
    format_duration,
    json_load,
    json_save,
    lock_file,
    merge_primary_json,
    safe_int,
    timestamp,
    webopen,
)


class JsonLoadTests(unittest.TestCase):
    def test_websocket_topic_equality_matches_hash_contract(self) -> None:
        def process(_target: int, _message: dict[str, Any]) -> None:
            return None

        first = WebsocketTopic("User", "Drops", 42, process)
        second = WebsocketTopic("User", "Drops", 42, process)

        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertNotEqual(first, str(first))
        self.assertEqual(len({first, second}), 1)

    def test_webopen_rejects_non_http_urls(self) -> None:
        with patch("utils.webbrowser.open_new_tab") as browser:
            for value in ("file:///tmp/login", "javascript:alert(1)"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    webopen(value)
        browser.assert_not_called()

    def test_packaged_browser_failure_restores_library_environment(self) -> None:
        original = {
            "LD_LIBRARY_PATH": "/bundled/lib",
            "LD_LIBRARY_PATH_ORIG": "/system/lib",
        }
        with patch.dict(os.environ, original, clear=False), patch(
            "utils.IS_PACKAGED",
            True,
        ), patch("utils.sys.platform", "linux"), patch(
            "utils.webbrowser.open_new_tab",
            side_effect=RuntimeError("browser failed"),
        ):
            with self.assertRaises(RuntimeError):
                webopen("https://www.twitch.tv/activate")
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/bundled/lib")
            self.assertEqual(
                os.environ["LD_LIBRARY_PATH_ORIG"],
                "/system/lib",
            )

    def test_source_paths_are_stable_under_test_runners(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(WORKING_DIR, project_root)
        self.assertEqual(LANG_PATH, project_root / "lang")

    def test_mutable_paths_use_the_per_user_data_directory(self) -> None:
        self.assertEqual(
            _user_data_dir(
                "linux",
                {"XDG_DATA_HOME": "/var/lib/user-data"},
                Path("/home/user"),
            ),
            Path("/var/lib/user-data/TwitchDropsMiner"),
        )
        self.assertEqual(
            _user_data_dir("darwin", {}, Path("/Users/user")),
            Path("/Users/user/Library/Application Support/TwitchDropsMiner"),
        )
        self.assertEqual(
            _user_data_dir(
                "win32",
                {"LOCALAPPDATA": "C:/Users/user/AppData/Local"},
                Path("C:/Users/user"),
            ),
            Path("C:/Users/user/AppData/Local/TwitchDropsMiner"),
        )
        for path in (
            LOG_PATH,
            DUMP_PATH,
            LOCK_PATH,
            COOKIES_PATH,
            OAUTH_TOKEN_PATH,
            SETTINGS_PATH,
            HISTORY_PATH,
        ):
            self.assertEqual(path.parent, DATA_DIR)
        self.assertEqual(CACHE_PATH.parent, DATA_DIR)
        self.assertEqual(CACHE_DB.parent, CACHE_PATH)

    def test_default_nested_values_are_not_shared(self) -> None:
        defaults = {"priority": [], "exclude": set()}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            first = json_load(path, defaults)
            first["priority"].append("Game")
            first["exclude"].add("Other Game")
            second = json_load(path, defaults)

        self.assertEqual(second, defaults)
        self.assertEqual(defaults, {"priority": [], "exclude": set()})

    def test_defaults_prune_unknown_keys_and_replace_wrong_types(self) -> None:
        defaults = {
            "nested": {"value": 1, "missing": True},
            "items": ["default"],
            "enabled": False,
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "nested": {"value": 2, "obsolete": "remove"},
                        "items": {"wrong": "type"},
                        "unknown": "remove",
                    }
                ),
                encoding="utf8",
            )

            loaded = json_load(path, defaults)

        self.assertEqual(
            loaded,
            {
                "nested": {"value": 2, "missing": True},
                "items": ["default"],
                "enabled": False,
            },
        )

    def test_legacy_predictable_temporary_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "proxy": {
                            "__type": "URL",
                            "data": "http://proxy.example:8080",
                        }
                    }
                ),
                encoding="utf8",
            )
            path.with_name("settings.json.new").write_text(
                json.dumps({"proxy": {"__type": "URL"}}),
                encoding="utf8",
            )

            legacy_temporary = path.with_name("settings.json.new")
            loaded = json_load(path, {"proxy": URL()})

            self.assertEqual(
                loaded["proxy"],
                URL("http://proxy.example:8080"),
            )
            self.assertTrue(legacy_temporary.exists())

    def test_unknown_typed_values_are_removed_from_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.json"
            path.write_text(
                json.dumps(
                    {
                        "items": [
                            {"__type": "FutureType", "data": "ignored"},
                            "kept",
                        ]
                    }
                ),
                encoding="utf8",
            )

            loaded = json_load(path, {"items": []})

        self.assertEqual(loaded, {"items": ["kept"]})

    def test_lock_file_is_private_and_does_not_truncate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.file"
            path.write_text("existing", encoding="utf8")

            acquired, file = lock_file(path)
            try:
                self.assertTrue(acquired)
                self.assertEqual(path.read_text(encoding="utf8"), "existing")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                file.close()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow open")
    def test_lock_file_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("protected", encoding="utf8")
            lock_path = root / "lock.file"
            lock_path.symlink_to(target)

            with self.assertRaises(OSError):
                lock_file(lock_path)

            self.assertEqual(target.read_text(encoding="utf8"), "protected")

    def test_atomic_write_is_private_before_writer_runs(self) -> None:
        observed_modes: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.json"

            def writer(file: io.TextIOWrapper) -> None:
                observed_modes.append(os.fstat(file.fileno()).st_mode & 0o777)
                file.write("secret")

            atomic_write(path, writer)

            self.assertEqual(observed_modes, [0o600])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_atomic_write_failure_preserves_last_good_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("old", encoding="utf8")

            def writer(file: io.TextIOWrapper) -> None:
                file.write("partial")
                raise OSError("disk full")

            with self.assertRaisesRegex(OSError, "disk full"):
                atomic_write(path, writer)

            self.assertEqual(path.read_text(encoding="utf8"), "old")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_json_save_replaces_without_leaving_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.with_name("settings.json.new").write_text(
                "obsolete",
                encoding="utf8",
            )
            json_save(path, {"value": 1})
            json_save(path, {"value": 2})

            self.assertEqual(json_load(path, {}, merge=False), {"value": 2})
            self.assertFalse(path.with_name("settings.json.new").exists())
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_merge_primary_json_preserves_progress_and_reconciles_lists(self) -> None:
        merged = merge_primary_json(
            {
                "id": "campaign",
                "timeBasedDrops": [
                    {"id": "drop", "self": {"currentMinutesWatched": 4}}
                ],
            },
            {
                "id": "campaign",
                "timeBasedDrops": [
                    {
                        "id": "drop",
                        "requiredMinutesWatched": 10,
                        "benefitEdges": [{"id": "benefit"}],
                    },
                    {"id": "second-drop"},
                ],
            },
        )

        self.assertEqual(merged["timeBasedDrops"][0]["self"]["currentMinutesWatched"], 4)
        self.assertEqual(merged["timeBasedDrops"][0]["requiredMinutesWatched"], 10)
        self.assertEqual(merged["timeBasedDrops"][1]["id"], "second-drop")

    def test_merge_primary_json_rejects_conflicting_types(self) -> None:
        with self.assertRaises(TypeError):
            merge_primary_json({"value": 1}, {"value": "one"})

    def test_format_duration_preserves_hour_alignment_modes(self) -> None:
        self.assertEqual(format_duration(3661.4), "01:01:01")
        self.assertEqual(format_duration(299, pad_hours=False), " 0:04:59")
        self.assertEqual(format_duration(-1), "00:00:00")

    def test_safe_int_rejects_lossy_or_boolean_values(self) -> None:
        self.assertEqual(safe_int(12), 12)
        self.assertEqual(safe_int("12"), 12)
        for value in (True, 1.5, "1.5", inf, None):
            with self.subTest(value=value):
                self.assertIsNone(safe_int(value))

    def test_rfc3339_timestamps_normalize_to_utc(self) -> None:
        self.assertEqual(
            timestamp("2026-08-07T01:02:03.123456789Z").isoformat(),
            "2026-08-07T01:02:03.123456+00:00",
        )
        self.assertEqual(
            timestamp("2026-08-07T03:02:03+02:00").isoformat(),
            "2026-08-07T01:02:03+00:00",
        )

    def test_non_object_json_root_is_rejected_when_merging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("[]", encoding="utf8")

            with self.assertRaises(ValueError):
                json_load(path, {})


if __name__ == "__main__":
    unittest.main()
