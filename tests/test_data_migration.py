from __future__ import annotations

import asyncio
import json
import os
import pickle
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp  # pyright: ignore[reportMissingImports]
from yarl import URL  # pyright: ignore[reportMissingImports]

import data_migration
from data_migration import DataMigrationError, migrate_legacy_data
from utils import atomic_write_bytes, durable_unlink, lock_file_set


class _TrackedLock:
    def __init__(self, name: str, closed: list[str]) -> None:
        self.name = name
        self._closed = closed

    def close(self) -> None:
        self._closed.append(self.name)


class DataMigrationTests(unittest.TestCase):
    def _directories(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        legacy = root / "legacy"
        data = root / "data"
        legacy.mkdir()
        return temporary, legacy, data

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf8"))
        except (OSError, UnicodeError, ValueError) as exc:
            self.fail(f"Unable to read migration fixture {path}: {exc}")
        if not isinstance(value, dict):
            self.fail(f"Migration fixture is not an object: {path}")
        return value

    @staticmethod
    def _legacy_cookie_pickle() -> bytes:
        loop = asyncio.new_event_loop()
        try:
            jar = aiohttp.CookieJar(loop=loop)
            jar.update_cookies(
                {"auth-token": "legacy-secret"},
                URL("https://www.twitch.tv/"),
            )
            return pickle.dumps(jar._cookies, protocol=pickle.HIGHEST_PROTOCOL)
        finally:
            loop.close()

    def test_migrates_versioned_artifacts_and_removes_obsolete_sources(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        (legacy / "settings.json").write_text(
            json.dumps({"language": "English", "dark_mode": True}),
            encoding="utf8",
        )
        (legacy / "oauth.json").write_text(
            json.dumps({"client_id": "client", "refresh_token": "refresh"}),
            encoding="utf8",
        )
        (legacy / "session_history.json").write_text(
            json.dumps({"version": 1, "sessions": []}),
            encoding="utf8",
        )
        (legacy / "cache").mkdir()
        (legacy / "cache" / "mapping.json").write_text("{}", encoding="utf8")

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual(
            set(result.migrated),
            {
                "settings.json",
                "oauth.json",
                "session_history.json",
                "cache/mapping.json",
            },
        )
        for relative in (
            "settings.json",
            "oauth.json",
            "session_history.json",
            "cache/mapping.json",
        ):
            self.assertTrue((data / relative).is_file())
            self.assertFalse((legacy / relative).exists())
        metadata = self._read_json(data / "storage.json")
        journal = self._read_json(data / "migration-journal.json")
        self.assertEqual(metadata["version"], data_migration.STORAGE_VERSION)
        self.assertEqual(journal["version"], data_migration.JOURNAL_VERSION)
        self.assertTrue(
            all(
                record["state"] == "complete"
                for record in journal["artifacts"].values()
            )
        )
        if os.name != "nt":
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)
            for path in (
                data / "settings.json",
                data / "oauth.json",
                data / "storage.json",
                data / "migration-journal.json",
            ):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        rerun = migrate_legacy_data(legacy_dir=legacy, data_dir=data)
        self.assertEqual(rerun.migrated, ())
        self.assertEqual(rerun.cleaned, ())

    def test_upgrades_version_one_marker_and_converts_legacy_cookie_pickle(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        (data / "storage.json").write_text('{"version": 1}', encoding="utf8")
        (legacy / "cookies.jar").write_bytes(self._legacy_cookie_pickle())

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("cookies.jar", result.migrated)
        self.assertFalse((legacy / "cookies.jar").exists())
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        jar = aiohttp.CookieJar(loop=loop)
        jar.load(data / "cookies.jar")
        cookies = jar.filter_cookies(URL("https://www.twitch.tv/"))
        self.assertEqual(cookies["auth-token"].value, "legacy-secret")
        metadata = self._read_json(data / "storage.json")
        self.assertEqual(metadata["version"], data_migration.STORAGE_VERSION)
        self.assertEqual(
            metadata["artifacts"]["cookies.jar"]["source_format"],
            "cookie-pickle-v1",
        )

    def test_recovers_cookie_from_version_one_misclassified_quarantine(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        quarantine = data / "migration-quarantine"
        quarantine.mkdir(parents=True)
        (data / "storage.json").write_text('{"version":1}', encoding="utf8")
        old_quarantine = quarantine / "cookies.jar.legacy-corrupt"
        old_quarantine.write_bytes(self._legacy_cookie_pickle())

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("cookies.jar", result.recovered)
        self.assertTrue((data / "cookies.jar").exists())
        self.assertFalse(old_quarantine.exists())
        self.assertIn("cookies.jar:v1-quarantine", result.cleaned)

    def test_valid_legacy_new_file_wins_and_is_recovered(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        canonical = legacy / "settings.json"
        recovery = legacy / "settings.json.new"
        canonical.write_text('{"language":"old"}', encoding="utf8")
        recovery.write_text('{"language":"recovered"}', encoding="utf8")

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("settings.json", result.recovered)
        self.assertEqual(
            self._read_json(data / "settings.json"),
            {"language": "recovered"},
        )
        self.assertFalse(canonical.exists())
        self.assertFalse(recovery.exists())

    def test_invalid_new_file_is_preserved_before_canonical_recovery(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        canonical = legacy / "settings.json"
        recovery = legacy / "settings.json.new"
        canonical.write_text('{"language":"canonical"}', encoding="utf8")
        recovery.write_bytes(b'{"language":')

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("settings.json", result.migrated)
        self.assertIn("settings.json", result.quarantined)
        self.assertEqual(
            (
                data
                / "migration-quarantine"
                / "settings.json.new.invalid"
            ).read_bytes(),
            b'{"language":',
        )
        self.assertFalse(canonical.exists())
        self.assertFalse(recovery.exists())

    def test_destination_wins_byte_for_byte_and_conflict_is_preserved(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        destination = data / "settings.json"
        destination_bytes = b'{ "language": "destination" }\n'
        destination.write_bytes(destination_bytes)
        source_bytes = b'{"language":"legacy"}'
        (legacy / "settings.json").write_bytes(source_bytes)

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("settings.json", result.destination_wins)
        self.assertIn("settings.json", result.quarantined)
        self.assertEqual(destination.read_bytes(), destination_bytes)
        self.assertEqual(
            (
                data
                / "migration-quarantine"
                / "settings.json.canonical.destination-conflict"
            ).read_bytes(),
            source_bytes,
        )
        self.assertFalse((legacy / "settings.json").exists())

    def test_destination_wins_preserves_conflicting_credential_before_cleanup(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        destination = data / "oauth.json"
        destination_bytes = b'{"client_id":"client","refresh_token":"current"}'
        destination.write_bytes(destination_bytes)
        source = legacy / "oauth.json"
        source_bytes = b'{"client_id":"client","refresh_token":"legacy"}'
        source.write_bytes(source_bytes)

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("oauth.json", result.destination_wins)
        self.assertIn("oauth.json", result.quarantined)
        self.assertEqual(destination.read_bytes(), destination_bytes)
        preserved = (
            data
            / "migration-quarantine"
            / "oauth.json.canonical.destination-conflict"
        )
        self.assertEqual(preserved.read_bytes(), source_bytes)
        self.assertFalse(source.exists())
        if os.name != "nt":
            self.assertEqual(preserved.stat().st_mode & 0o777, 0o600)

    def test_transient_install_failure_keeps_source_and_does_not_complete(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_text('{"language":"English"}', encoding="utf8")
        real_write = data_migration.atomic_write_bytes

        def fail_target(path: Path, contents: bytes) -> None:
            if path == data / "settings.json":
                raise OSError("injected target failure")
            real_write(path, contents)

        with (
            patch.object(data_migration, "atomic_write_bytes", side_effect=fail_target),
            self.assertRaisesRegex(DataMigrationError, "install migrated file"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertTrue(source.exists())
        self.assertFalse((data / "storage.json").exists())
        journal = self._read_json(data / "migration-journal.json")
        self.assertEqual(journal["artifacts"]["settings.json"]["state"], "pending")

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)
        self.assertIn("settings.json", result.migrated)
        self.assertFalse(source.exists())

    def test_cleanup_failure_resumes_after_durable_target_install(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_text('{"language":"English"}', encoding="utf8")
        real_unlink = data_migration.durable_unlink
        failed = False

        def fail_source_cleanup(path: Path, *, require_regular: bool = True) -> bool:
            nonlocal failed
            if path == source and not failed:
                failed = True
                raise OSError("injected cleanup failure")
            return real_unlink(path, require_regular=require_regular)

        with (
            patch.object(data_migration, "durable_unlink", side_effect=fail_source_cleanup),
            self.assertRaisesRegex(DataMigrationError, "remove migrated source"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertTrue((data / "settings.json").exists())
        self.assertTrue(source.exists())
        self.assertFalse((data / "storage.json").exists())
        journal = self._read_json(data / "migration-journal.json")
        self.assertEqual(
            journal["artifacts"]["settings.json"]["state"],
            "installed",
        )

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)
        self.assertIn("settings.json", result.migrated)
        self.assertFalse(source.exists())
        self.assertTrue((data / "storage.json").exists())

    def test_changed_source_is_never_deleted_during_cleanup_recovery(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_text('{"language":"first"}', encoding="utf8")
        real_unlink = data_migration.durable_unlink
        failed = False

        def interrupt_cleanup(path: Path, *, require_regular: bool = True) -> bool:
            nonlocal failed
            if path == source and not failed:
                failed = True
                raise OSError("injected cleanup interruption")
            return real_unlink(path, require_regular=require_regular)

        with (
            patch.object(data_migration, "durable_unlink", side_effect=interrupt_cleanup),
            self.assertRaisesRegex(DataMigrationError, "remove migrated source"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        source.write_text('{"language":"changed"}', encoding="utf8")
        with self.assertRaisesRegex(DataMigrationError, "changed before cleanup"):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual(source.read_text(encoding="utf8"), '{"language":"changed"}')
        self.assertFalse((data / "storage.json").exists())

    def test_marker_failure_resumes_without_needing_deleted_sources(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        (legacy / "settings.json").write_text("{}", encoding="utf8")
        real_write = data_migration.atomic_write_bytes

        def fail_marker(path: Path, contents: bytes) -> None:
            if path == data / "storage.json":
                raise OSError("injected marker failure")
            real_write(path, contents)

        with (
            patch.object(data_migration, "atomic_write_bytes", side_effect=fail_marker),
            self.assertRaisesRegex(DataMigrationError, "persist migration state"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertFalse((legacy / "settings.json").exists())
        self.assertTrue((data / "settings.json").exists())
        self.assertFalse((data / "storage.json").exists())

        migrate_legacy_data(legacy_dir=legacy, data_dir=data)
        self.assertTrue((data / "storage.json").exists())

    def test_forward_artifact_version_is_rejected_without_cleanup(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "session_history.json"
        source.write_text('{"version":99,"sessions":[]}', encoding="utf8")

        with self.assertRaisesRegex(DataMigrationError, "newer than supported"):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertTrue(source.exists())
        self.assertFalse((data / "session_history.json").exists())
        self.assertFalse((data / "storage.json").exists())

    def test_forward_storage_and_journal_versions_are_rejected(self) -> None:
        for filename, payload, expected in (
            (
                "storage.json",
                {"version": data_migration.STORAGE_VERSION + 1},
                "newer than supported",
            ),
            (
                "migration-journal.json",
                {
                    "version": data_migration.JOURNAL_VERSION + 1,
                    "target_storage_version": data_migration.STORAGE_VERSION,
                },
                "journal version",
            ),
        ):
            with self.subTest(filename=filename):
                temporary, legacy, data = self._directories()
                self.addCleanup(temporary.cleanup)
                data.mkdir()
                (data / filename).write_text(json.dumps(payload), encoding="utf8")
                with self.assertRaisesRegex(DataMigrationError, expected):
                    migrate_legacy_data(legacy_dir=legacy, data_dir=data)
                self.assertFalse((data / "settings.json").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_source_and_destination_symlinks_are_rejected_without_following(self) -> None:
        for destination in (False, True):
            with self.subTest(destination=destination):
                temporary, legacy, data = self._directories()
                self.addCleanup(temporary.cleanup)
                protected = Path(temporary.name) / "protected.json"
                protected.write_text('{"protected":true}', encoding="utf8")
                if destination:
                    data.mkdir()
                    link = data / "settings.json"
                else:
                    link = legacy / "settings.json"
                try:
                    link.symlink_to(protected)
                except OSError as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")

                with self.assertRaisesRegex(DataMigrationError, "not a regular file"):
                    migrate_legacy_data(legacy_dir=legacy, data_dir=data)

                self.assertEqual(
                    protected.read_text(encoding="utf8"),
                    '{"protected":true}',
                )
                self.assertFalse((data / "storage.json").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_symlinked_cache_parent_is_rejected_without_external_access(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        outside = Path(temporary.name) / "outside"
        outside.mkdir()
        sentinel = outside / "mapping.json"
        sentinel.write_text('{"outside":true}', encoding="utf8")
        try:
            (legacy / "cache").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaisesRegex(DataMigrationError, "directory is not real"):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual(sentinel.read_text(encoding="utf8"), '{"outside":true}')
        self.assertFalse((data / "cache" / "mapping.json").exists())
        self.assertFalse((data / "storage.json").exists())

    def test_journal_output_paths_cannot_escape_the_data_directory(self) -> None:
        temporary, _legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()

        with self.assertRaisesRegex(DataMigrationError, "escaped"):
            data_migration._safe_data_path(data, "../outside")

    def test_binary_replace_and_source_unlink_fsync_after_the_change(self) -> None:
        temporary, _legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        path = data / "credential.bin"
        observations: list[tuple[bool, bytes | None]] = []

        def observe(_path: Path) -> None:
            observations.append(
                (path.exists(), path.read_bytes() if path.exists() else None)
            )

        with patch("utils._fsync_parent_directory", side_effect=observe):
            atomic_write_bytes(path, b"secret")
            durable_unlink(path)

        self.assertEqual(observations, [(True, b"secret"), (False, None)])

    def test_oversized_artifact_is_rejected_without_completion(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_bytes(b"x" * (4 * 1024 * 1024 + 1))

        with self.assertRaisesRegex(DataMigrationError, "size limit"):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertTrue(source.exists())
        self.assertFalse((data / "storage.json").exists())

    def test_portable_appimage_source_and_macos_layout_fixtures(self) -> None:
        layouts = (
            Path("Windows Portable") / "Twitch Drops Miner",
            Path("Downloads") / "Twitch.Drops.Miner-x86_64.AppImage.files",
            Path("source-checkout"),
            Path("Twitch Drops Miner.app") / "Contents" / "MacOS",
        )
        for layout in layouts:
            with self.subTest(layout=str(layout)):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    legacy = root / layout
                    data = root / "profile" / "TwitchDropsMiner"
                    legacy.mkdir(parents=True)
                    (legacy / "settings.json").write_text(
                        '{"language":"English"}',
                        encoding="utf8",
                    )

                    result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

                    self.assertIn("settings.json", result.migrated)
                    self.assertTrue((data / "settings.json").exists())
                    self.assertFalse((legacy / "settings.json").exists())

    @unittest.skipUnless(
        sys.platform in {"win32", "linux", "darwin"},
        "platform lock protocol unavailable",
    )
    def test_running_legacy_process_blocks_new_startup_lock_set(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        legacy_lock = legacy / "lock.file"
        current_lock = data / "lock.file"
        script = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                "from utils import lock_file",
                "ok, held = lock_file(Path(sys.argv[1]))",
                "print('ready' if ok else 'failed', flush=True)",
                "sys.stdin.readline()",
                "held.close()",
            )
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(legacy_lock)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.kill)
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "ready")
        try:
            success, locks = lock_file_set((legacy_lock, current_lock))
            self.assertFalse(success)
            self.assertEqual(locks, ())
            self.assertFalse(current_lock.exists())
        finally:
            assert process.stdin is not None
            process.stdin.write("\n")
            process.stdin.flush()
            _stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)

    def test_lock_set_uses_legacy_then_new_order_and_releases_on_collision(self) -> None:
        legacy = Path("legacy") / "lock.file"
        current = Path("data") / "lock.file"
        calls: list[Path] = []
        closed: list[str] = []

        def acquire(path: Path):
            calls.append(path)
            lock = _TrackedLock(path.parent.name, closed)
            return (path == legacy, lock)

        with patch("utils.lock_file", side_effect=acquire):
            success, files = lock_file_set((legacy, current))

        self.assertFalse(success)
        self.assertEqual(files, ())
        self.assertEqual(calls, [legacy, current])
        self.assertEqual(closed, ["data", "legacy"])


if __name__ == "__main__":
    unittest.main()
