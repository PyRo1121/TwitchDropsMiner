from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_migration  # pyright: ignore[reportMissingImports]
from data_migration import (  # pyright: ignore[reportMissingImports]
    DataMigrationError,
    migrate_legacy_data,
)


class DataMigrationTests(unittest.TestCase):
    def _directories(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        legacy = root / "legacy"
        data = root / "data"
        legacy.mkdir()
        return temporary, legacy, data

    def test_migrates_durable_state_and_bounded_cache_once(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        settings_text = json.dumps({"language": "English", "dark_mode": True})
        cookies_text = json.dumps({"twitch.tv|": {"unique_id": {"value": "device"}}})
        (legacy / "settings.json").write_text(settings_text, encoding="utf8")
        (legacy / "cookies.jar").write_text(cookies_text, encoding="utf8")
        (legacy / "cache").mkdir()
        (legacy / "cache" / "mapping.json").write_text("{}", encoding="utf8")

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual((data / "settings.json").read_text(), settings_text)
        self.assertEqual((data / "cookies.jar").read_text(), cookies_text)
        self.assertEqual((data / "cache" / "mapping.json").read_text(), "{}")
        self.assertTrue((legacy / "settings.json").exists())
        self.assertEqual(
            set(result.copied),
            {"settings.json", "cookies.jar", "cache/mapping.json"},
        )
        self.assertEqual(
            data_migration._metadata_version(data / "storage.json"),
            data_migration.STORAGE_VERSION,
        )
        if os.name != "nt":
            self.assertEqual((data / "settings.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)

        rerun = migrate_legacy_data(legacy_dir=legacy, data_dir=data)
        self.assertEqual(rerun.copied, ())

    def test_existing_destination_wins(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        (legacy / "settings.json").write_text('{"source": true}', encoding="utf8")
        (data / "settings.json").write_text('{"destination": true}', encoding="utf8")

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual((data / "settings.json").read_text(), '{"destination": true}')
        self.assertIn("settings.json:destination-exists", result.skipped)

    def test_corrupt_legacy_json_is_quarantined_without_installing_it(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        corrupt = b'{"language":'
        (legacy / "settings.json").write_bytes(corrupt)

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertFalse((data / "settings.json").exists())
        self.assertEqual(
            (data / "migration-quarantine" / "settings.json.legacy-corrupt").read_bytes(),
            corrupt,
        )
        self.assertEqual(result.quarantined, ("settings.json",))
        self.assertEqual((legacy / "settings.json").read_bytes(), corrupt)

    def test_unknown_forward_storage_version_is_rejected(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        marker_text = json.dumps({"version": data_migration.STORAGE_VERSION + 1})
        (data / "storage.json").write_text(marker_text, encoding="utf8")
        (legacy / "settings.json").write_text("{}", encoding="utf8")

        with self.assertRaisesRegex(DataMigrationError, "newer than supported"):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertFalse((data / "settings.json").exists())
        self.assertEqual((data / "storage.json").read_text(), marker_text)

    def test_interrupted_marker_write_can_be_rerun_safely(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        (legacy / "settings.json").write_text('{"language": "English"}', encoding="utf8")
        original_write = data_migration._atomic_write_bytes

        def fail_marker(path: Path, contents: bytes) -> None:
            if path.name == "storage.json":
                raise DataMigrationError("injected marker failure")
            original_write(path, contents)

        with (
            patch.object(data_migration, "_atomic_write_bytes", side_effect=fail_marker),
            self.assertRaisesRegex(DataMigrationError, "injected marker failure"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertTrue((data / "settings.json").exists())
        self.assertFalse((data / "storage.json").exists())

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)
        self.assertIn("settings.json:destination-exists", result.skipped)
        self.assertTrue((data / "storage.json").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_legacy_symlink_is_not_followed(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        secret = Path(temporary.name) / "secret.json"
        secret.write_text('{"secret": true}', encoding="utf8")
        try:
            (legacy / "oauth.json").symlink_to(secret)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertFalse((data / "oauth.json").exists())
        self.assertTrue(any(item.startswith("oauth.json:") for item in result.skipped))


if __name__ == "__main__":
    unittest.main()
