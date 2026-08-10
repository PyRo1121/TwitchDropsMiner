from __future__ import annotations

import asyncio
import hashlib
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

    @staticmethod
    def _conflict_path(
        data: Path,
        artifact: str,
        role: str,
        reason: str,
        contents: bytes,
    ) -> Path:
        digest = hashlib.sha256(contents).hexdigest()
        safe_artifact = artifact.replace("/", "-")
        return (
            data
            / "migration-quarantine"
            / f"{safe_artifact}.{role}.{reason}.{digest}"
        )

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
            "cookie-pickle-v1-exact-host-credentials",
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
        canonical_bytes = b'{"language":"old"}'
        recovery_bytes = b'{"language":"recovered"}'
        canonical.write_bytes(canonical_bytes)
        recovery.write_bytes(recovery_bytes)

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("settings.json", result.recovered)
        self.assertIn("settings.json", result.quarantined)
        self.assertEqual(
            self._read_json(data / "settings.json"),
            {"language": "recovered"},
        )
        conflict = self._conflict_path(
            data,
            "settings.json",
            "canonical",
            "source-conflict",
            canonical_bytes,
        )
        self.assertEqual(conflict.read_bytes(), canonical_bytes)
        journal = self._read_json(data / "migration-journal.json")
        self.assertIn(
            str(conflict.relative_to(data)),
            [item["path"] for item in journal["artifacts"]["settings.json"]["outputs"]],
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

    def test_same_location_recovers_new_only_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            recovery = data / "settings.json.new"
            recovery.write_bytes(b'{"language":"recovered"}')

            result = migrate_legacy_data(legacy_dir=data, data_dir=data)

            self.assertIn("settings.json", result.recovered)
            self.assertEqual(
                self._read_json(data / "settings.json"),
                {"language": "recovered"},
            )
            self.assertFalse(recovery.exists())
            self.assertTrue((data / "storage.json").exists())

    def test_same_location_equal_canonical_and_new_keeps_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            contents = b'{"language":"same"}'
            canonical = data / "settings.json"
            recovery = data / "settings.json.new"
            canonical.write_bytes(contents)
            recovery.write_bytes(contents)

            result = migrate_legacy_data(legacy_dir=data, data_dir=data)

            self.assertIn("settings.json", result.destination_wins)
            self.assertEqual(canonical.read_bytes(), contents)
            self.assertFalse(recovery.exists())
            self.assertNotIn("settings.json", result.quarantined)

    def test_same_location_conflicting_new_wins_and_preserves_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            canonical = data / "settings.json"
            recovery = data / "settings.json.new"
            canonical_bytes = b'{"language":"canonical"}'
            recovery_bytes = b'{"language":"recovery"}'
            canonical.write_bytes(canonical_bytes)
            recovery.write_bytes(recovery_bytes)

            result = migrate_legacy_data(legacy_dir=data, data_dir=data)

            self.assertIn("settings.json", result.recovered)
            self.assertIn("settings.json", result.quarantined)
            self.assertEqual(self._read_json(canonical), {"language": "recovery"})
            preserved = self._conflict_path(
                data,
                "settings.json",
                "canonical",
                "recovery-conflict",
                canonical_bytes,
            )
            self.assertEqual(preserved.read_bytes(), canonical_bytes)
            self.assertFalse(recovery.exists())

    def test_same_location_recovery_conflict_is_journaled_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            canonical = data / "settings.json"
            recovery = data / "settings.json.new"
            canonical_bytes = b'{"language":"canonical"}'
            recovery_bytes = b'{"language":"recovery"}'
            canonical.write_bytes(canonical_bytes)
            recovery.write_bytes(recovery_bytes)
            real_install = data_migration._install_payload
            interrupted = False

            def interrupt_after_install(*args: Any, **kwargs: Any) -> bytes:
                nonlocal interrupted
                installed = real_install(*args, **kwargs)
                if not interrupted:
                    interrupted = True
                    raise DataMigrationError("injected post-install interruption")
                return installed

            with (
                patch.object(
                    data_migration,
                    "_install_payload",
                    side_effect=interrupt_after_install,
                ),
                self.assertRaisesRegex(DataMigrationError, "post-install interruption"),
            ):
                migrate_legacy_data(legacy_dir=data, data_dir=data)

            preserved = self._conflict_path(
                data,
                "settings.json",
                "canonical",
                "recovery-conflict",
                canonical_bytes,
            )
            journal = self._read_json(data / "migration-journal.json")
            self.assertEqual(preserved.read_bytes(), canonical_bytes)
            self.assertIn(
                str(preserved.relative_to(data)),
                [
                    item["path"]
                    for item in journal["artifacts"]["settings.json"]["outputs"]
                ],
            )
            self.assertEqual(self._read_json(canonical), {"language": "recovery"})
            self.assertTrue(recovery.exists())

            migrate_legacy_data(legacy_dir=data, data_dir=data)

            self.assertEqual(preserved.read_bytes(), canonical_bytes)
            self.assertFalse(recovery.exists())
            self.assertTrue((data / "storage.json").exists())

    def test_same_location_current_marker_does_not_strand_new_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "storage.json").write_text(
                json.dumps({"version": data_migration.STORAGE_VERSION}),
                encoding="utf8",
            )
            recovery = data / "settings.json.new"
            recovery.write_bytes(b'{"language":"late-recovery"}')

            result = migrate_legacy_data(legacy_dir=data, data_dir=data)

            self.assertIn("settings.json", result.recovered)
            self.assertEqual(
                self._read_json(data / "settings.json"),
                {"language": "late-recovery"},
            )
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
            self._conflict_path(
                data,
                "settings.json",
                "canonical",
                "destination-conflict",
                source_bytes,
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
        preserved = self._conflict_path(
            data,
            "oauth.json",
            "canonical",
            "destination-conflict",
            source_bytes,
        )
        self.assertEqual(preserved.read_bytes(), source_bytes)
        self.assertFalse(source.exists())
        if os.name != "nt":
            self.assertEqual(preserved.stat().st_mode & 0o777, 0o600)

    def test_legacy_installed_journal_replans_before_source_cleanup(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        canonical_bytes = b'{"language":"canonical"}'
        recovery_bytes = b'{"language":"recovery"}'
        (legacy / "settings.json").write_bytes(canonical_bytes)
        (legacy / "settings.json.new").write_bytes(recovery_bytes)
        (data / "settings.json").write_bytes(recovery_bytes + b"\n")
        journal = data_migration._new_journal(legacy)
        for record in journal["artifacts"].values():
            record.pop("plan_version")
            record["state"] = "complete"
        journal["artifacts"]["settings.json"].update(
            {
                "state": "installed",
                "outputs": [],
                "cleanup": [
                    {
                        "role": "canonical",
                        "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
                        "removed": False,
                    },
                    {
                        "role": "new",
                        "sha256": hashlib.sha256(recovery_bytes).hexdigest(),
                        "removed": False,
                    },
                ],
            }
        )
        (data / "migration-journal.json").write_text(
            json.dumps(journal),
            encoding="utf8",
        )

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("settings.json", result.destination_wins)
        preserved = self._conflict_path(
            data,
            "settings.json",
            "canonical",
            "destination-conflict",
            canonical_bytes,
        )
        self.assertEqual(preserved.read_bytes(), canonical_bytes)
        self.assertFalse((legacy / "settings.json").exists())
        self.assertFalse((legacy / "settings.json.new").exists())

    def test_old_complete_journal_replans_new_without_current_marker(self) -> None:
        for marker_version in (None, 1):
            with self.subTest(marker_version=marker_version), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                recovery_bytes = b'{"language":"recovered"}'
                (data / "settings.json.new").write_bytes(recovery_bytes)
                if marker_version is not None:
                    (data / "storage.json").write_text(
                        json.dumps({"version": marker_version}),
                        encoding="utf8",
                    )
                quarantine = data / "migration-quarantine"
                quarantine.mkdir()
                prior_output = quarantine / "settings-prior-output"
                prior_bytes = b"prior-journaled-output"
                prior_output.write_bytes(prior_bytes)
                journal = data_migration._new_journal(data)
                for record in journal["artifacts"].values():
                    record.pop("plan_version")
                    record["state"] = "complete"
                    record["result"] = "absent"
                journal["artifacts"]["settings.json"]["outputs"] = [
                    {
                        "path": str(prior_output.relative_to(data)),
                        "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                    }
                ]
                (data / "migration-journal.json").write_text(
                    json.dumps(journal),
                    encoding="utf8",
                )

                result = migrate_legacy_data(legacy_dir=data, data_dir=data)

                self.assertIn("settings.json", result.recovered)
                self.assertEqual(
                    self._read_json(data / "settings.json"),
                    {"language": "recovered"},
                )
                self.assertFalse((data / "settings.json.new").exists())
                self.assertTrue(prior_output.exists())
                completed = self._read_json(data / "migration-journal.json")
                settings_record = completed["artifacts"]["settings.json"]
                self.assertEqual(settings_record["plan_version"], 3)
                self.assertEqual(settings_record["state"], "complete")
                self.assertIn(
                    str(prior_output.relative_to(data)),
                    [output["path"] for output in settings_record["outputs"]],
                )
                self.assertEqual(
                    self._read_json(data / "storage.json")["version"],
                    2,
                )

    def test_old_complete_replan_replaces_stale_destination_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            canonical_bytes = b'{"language":"canonical"}'
            recovery_bytes = b'{"language":"recovered"}'
            destination = data / "settings.json"
            recovery = data / "settings.json.new"
            destination.write_bytes(canonical_bytes)
            recovery.write_bytes(recovery_bytes)
            quarantine = data / "migration-quarantine"
            quarantine.mkdir()
            prior_output = quarantine / "settings-prior-conflict"
            prior_bytes = b"immutable-prior-conflict"
            prior_output.write_bytes(prior_bytes)
            journal = data_migration._new_journal(data)
            for record in journal["artifacts"].values():
                record.pop("plan_version")
                record["state"] = "complete"
                record["result"] = "absent"
            journal["artifacts"]["settings.json"].update(
                {
                    "result": "destination-wins",
                    "outputs": [
                        {
                            "path": "settings.json",
                            "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
                        },
                        {
                            "path": str(prior_output.relative_to(data)),
                            "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                        },
                    ],
                }
            )
            (data / "migration-journal.json").write_text(
                json.dumps(journal),
                encoding="utf8",
            )

            result = migrate_legacy_data(legacy_dir=data, data_dir=data)

            self.assertIn("settings.json", result.recovered)
            self.assertIn("settings.json", result.quarantined)
            self.assertEqual(
                self._read_json(destination),
                {"language": "recovered"},
            )
            self.assertFalse(recovery.exists())
            loser = self._conflict_path(
                data,
                "settings.json",
                "canonical",
                "recovery-conflict",
                canonical_bytes,
            )
            self.assertEqual(loser.read_bytes(), canonical_bytes)
            self.assertEqual(prior_output.read_bytes(), prior_bytes)

            completed = self._read_json(data / "migration-journal.json")
            settings_record = completed["artifacts"]["settings.json"]
            destination_outputs = [
                output
                for output in settings_record["outputs"]
                if output["path"] == "settings.json"
            ]
            current_destination = destination.read_bytes()
            self.assertEqual(
                destination_outputs,
                [
                    {
                        "path": "settings.json",
                        "sha256": hashlib.sha256(current_destination).hexdigest(),
                    }
                ],
            )
            journaled_paths = {
                output["path"] for output in settings_record["outputs"]
            }
            self.assertIn(str(loser.relative_to(data)), journaled_paths)
            self.assertIn(str(prior_output.relative_to(data)), journaled_paths)
            settings_spec = next(
                spec for spec in data_migration._ARTIFACTS if spec.key == "settings.json"
            )
            data_migration._verify_outputs(
                settings_record,
                data,
                settings_spec.maximum_bytes,
            )
            self.assertEqual(
                self._read_json(data / "storage.json")["version"],
                2,
            )

            rerun = migrate_legacy_data(legacy_dir=data, data_dir=data)
            self.assertEqual(rerun.recovered, ())
            self.assertEqual(rerun.cleaned, ())

    def test_destination_wins_preserves_both_distinct_source_generations(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        destination = data / "settings.json"
        destination.write_bytes(b'{"language":"destination"}')
        canonical_bytes = b'{"language":"canonical"}'
        recovery_bytes = b'{"language":"recovery"}'
        (legacy / "settings.json").write_bytes(canonical_bytes)
        (legacy / "settings.json.new").write_bytes(recovery_bytes)

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("settings.json", result.destination_wins)
        self.assertIn("settings.json", result.quarantined)
        for role, contents in (
            ("canonical", canonical_bytes),
            ("new", recovery_bytes),
        ):
            with self.subTest(role=role):
                preserved = self._conflict_path(
                    data,
                    "settings.json",
                    role,
                    "destination-conflict",
                    contents,
                )
                self.assertEqual(preserved.read_bytes(), contents)
        self.assertFalse((legacy / "settings.json").exists())
        self.assertFalse((legacy / "settings.json.new").exists())

    def test_destination_wins_preserves_all_distinct_oauth_generations(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        data.mkdir()
        destination_bytes = b'{"client_id":"client","refresh_token":"current"}'
        canonical_bytes = b'{"client_id":"client","refresh_token":"canonical"}'
        recovery_bytes = b'{"client_id":"client","refresh_token":"recovery"}'
        quarantine_bytes = b'{"client_id":"client","refresh_token":"quarantine"}'
        (data / "oauth.json").write_bytes(destination_bytes)
        (legacy / "oauth.json").write_bytes(canonical_bytes)
        (legacy / "oauth.json.new").write_bytes(recovery_bytes)
        quarantine = data / "migration-quarantine"
        quarantine.mkdir()
        (quarantine / "oauth.json.legacy-corrupt").write_bytes(quarantine_bytes)

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("oauth.json", result.destination_wins)
        self.assertEqual((data / "oauth.json").read_bytes(), destination_bytes)
        journal = self._read_json(data / "migration-journal.json")
        journaled_outputs = {
            item["path"] for item in journal["artifacts"]["oauth.json"]["outputs"]
        }
        for role, contents in (
            ("canonical", canonical_bytes),
            ("new", recovery_bytes),
            ("v1-quarantine", quarantine_bytes),
        ):
            with self.subTest(role=role):
                preserved = self._conflict_path(
                    data,
                    "oauth.json",
                    role,
                    "destination-conflict",
                    contents,
                )
                self.assertEqual(preserved.read_bytes(), contents)
                self.assertIn(str(preserved.relative_to(data)), journaled_outputs)
                if os.name != "nt":
                    self.assertEqual(preserved.stat().st_mode & 0o777, 0o600)

    def test_transient_install_failure_keeps_source_and_does_not_complete(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_text('{"language":"English"}', encoding="utf8")
        real_write = data_migration.atomic_write_bytes

        def fail_target(
            path: Path,
            contents: bytes,
            **kwargs: Any,
        ) -> None:
            if path == data / "settings.json":
                raise OSError("injected target failure")
            real_write(path, contents, **kwargs)

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

    def test_capture_failure_resumes_after_durable_target_install(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_text('{"language":"English"}', encoding="utf8")
        real_secure = data_migration._make_capture_private
        failed = False

        def interrupt_capture(path: Path) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise DataMigrationError("injected capture interruption")
            real_secure(path)

        with (
            patch.object(
                data_migration,
                "_make_capture_private",
                side_effect=interrupt_capture,
            ),
            self.assertRaisesRegex(DataMigrationError, "capture interruption"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertTrue((data / "settings.json").exists())
        self.assertFalse(source.exists())
        captures = tuple(
            legacy.glob(".settings.json.migration-capture-canonical-*.quarantine")
        )
        self.assertEqual(len(captures), 1)
        self.assertFalse((data / "storage.json").exists())
        journal = self._read_json(data / "migration-journal.json")
        self.assertEqual(
            journal["artifacts"]["settings.json"]["state"],
            "installed",
        )

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)
        self.assertIn("settings.json", result.migrated)
        self.assertFalse(source.exists())
        self.assertEqual(captures[0].read_text(encoding="utf8"), '{"language":"English"}')
        self.assertTrue((data / "storage.json").exists())

    def test_changed_source_is_never_deleted_during_cleanup_recovery(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_text('{"language":"first"}', encoding="utf8")
        failed = False

        def interrupt_capture(_path: Path) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise DataMigrationError("injected capture interruption")

        with (
            patch.object(
                data_migration,
                "_make_capture_private",
                side_effect=interrupt_capture,
            ),
            self.assertRaisesRegex(DataMigrationError, "capture interruption"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertFalse(source.exists())
        self.assertEqual(
            len(
                tuple(
                    legacy.glob(
                        ".settings.json.migration-capture-canonical-*.quarantine"
                    )
                )
            ),
            1,
        )
        changed = b'{"language":"changed"}'
        source.write_bytes(changed)
        with self.assertRaisesRegex(DataMigrationError, "replaced during cleanup"):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual(source.read_bytes(), changed)
        preserved = self._conflict_path(
            data,
            "settings.json",
            "canonical",
            "replacement-conflict",
            changed,
        )
        self.assertEqual(preserved.read_bytes(), changed)
        self.assertFalse((data / "storage.json").exists())

    def test_replacement_after_snapshot_is_preserved_not_unlinked(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        original = b'{"language":"original"}'
        replacement = b'{"language":"replacement"}'
        source.write_bytes(original)
        real_read = data_migration._read_regular_file
        replaced = False

        def replace_after_snapshot(path: Path, maximum_bytes: int) -> bytes:
            nonlocal replaced
            contents = real_read(path, maximum_bytes)
            if path == source and not replaced:
                replaced = True
                atomic_write_bytes(source, replacement)
            return contents

        with (
            patch.object(
                data_migration,
                "_read_regular_file",
                side_effect=replace_after_snapshot,
            ),
            self.assertRaisesRegex(DataMigrationError, "changed during verification"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual(self._read_json(data / "settings.json"), {"language": "original"})
        preserved = self._conflict_path(
            data,
            "settings.json",
            "canonical",
            "replacement-conflict",
            replacement,
        )
        self.assertEqual(preserved.read_bytes(), replacement)
        captures = tuple(
            legacy.glob(".settings.json.migration-capture-canonical-*.quarantine")
        )
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0].read_bytes(), replacement)
        self.assertFalse((data / "storage.json").exists())

    def test_replacement_of_capture_after_read_is_preserved_and_retryable(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        original = b'{"language":"original"}'
        replacement = b'{"language":"replacement"}'
        source.write_bytes(original)
        real_read = data_migration._read_regular_file
        replaced = False

        def replace_capture_after_read(path: Path, maximum_bytes: int) -> bytes:
            nonlocal replaced
            contents = real_read(path, maximum_bytes)
            if ".migration-capture-canonical-" in path.name and not replaced:
                replaced = True
                atomic_write_bytes(path, replacement)
            return contents

        with (
            patch.object(
                data_migration,
                "_read_regular_file",
                side_effect=replace_capture_after_read,
            ),
            self.assertRaisesRegex(DataMigrationError, "changed during verification"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        captures = tuple(
            legacy.glob(".settings.json.migration-capture-canonical-*.quarantine")
        )
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0].read_bytes(), replacement)
        captured_original = self._conflict_path(
            data,
            "settings.json",
            "canonical",
            "captured-source",
            original,
        )
        preserved_replacement = self._conflict_path(
            data,
            "settings.json",
            "canonical",
            "replacement-conflict",
            replacement,
        )
        self.assertEqual(captured_original.read_bytes(), original)
        self.assertEqual(preserved_replacement.read_bytes(), replacement)
        journal = self._read_json(data / "migration-journal.json")
        settings_record = journal["artifacts"]["settings.json"]
        self.assertEqual(settings_record["state"], "installed")
        journaled_outputs = {output["path"] for output in settings_record["outputs"]}
        self.assertIn(str(captured_original.relative_to(data)), journaled_outputs)
        self.assertIn(str(preserved_replacement.relative_to(data)), journaled_outputs)
        self.assertFalse((data / "storage.json").exists())

        result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertIn("settings.json", result.migrated)
        self.assertEqual(captures[0].read_bytes(), replacement)
        self.assertEqual(captured_original.read_bytes(), original)
        self.assertEqual(preserved_replacement.read_bytes(), replacement)
        self.assertTrue((data / "storage.json").exists())

    def test_replacement_created_after_staged_read_remains_at_source_path(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        source = legacy / "settings.json"
        source.write_bytes(b'{"language":"original"}')
        replacement = b'{"language":"replacement"}'
        real_read = data_migration._read_regular_file
        replaced = False

        def replace_after_staged_read(path: Path, maximum_bytes: int) -> bytes:
            nonlocal replaced
            contents = real_read(path, maximum_bytes)
            if ".migration-capture-canonical-" in path.name and not replaced:
                replaced = True
                atomic_write_bytes(source, replacement)
            return contents

        with (
            patch.object(
                data_migration,
                "_read_regular_file",
                side_effect=replace_after_staged_read,
            ),
            self.assertRaisesRegex(DataMigrationError, "replaced during cleanup"),
        ):
            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

        self.assertEqual(source.read_bytes(), replacement)
        self.assertEqual(
            len(
                tuple(
                    legacy.glob(
                        ".settings.json.migration-capture-canonical-*.quarantine"
                    )
                )
            ),
            1,
        )
        self.assertFalse((data / "storage.json").exists())

    def test_marker_failure_resumes_without_needing_deleted_sources(self) -> None:
        temporary, legacy, data = self._directories()
        self.addCleanup(temporary.cleanup)
        (legacy / "settings.json").write_text("{}", encoding="utf8")
        real_write = data_migration.atomic_write_bytes

        def fail_marker(
            path: Path,
            contents: bytes,
            **kwargs: Any,
        ) -> None:
            if path == data / "storage.json":
                raise OSError("injected marker failure")
            real_write(path, contents, **kwargs)

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
