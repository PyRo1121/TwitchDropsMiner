from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from constants import LANG_PATH, WORKING_DIR
from utils import json_load, json_save, timestamp


class JsonLoadTests(unittest.TestCase):
    def test_source_paths_are_stable_under_test_runners(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(WORKING_DIR, project_root)
        self.assertEqual(LANG_PATH, project_root / "lang")

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

    def test_json_save_replaces_without_leaving_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            json_save(path, {"value": 1})
            json_save(path, {"value": 2})

            self.assertEqual(json_load(path, {}, merge=False), {"value": 2})
            self.assertFalse(path.with_name("settings.json.new").exists())

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
