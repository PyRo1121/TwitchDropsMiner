from __future__ import annotations

import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import oauth_storage
from oauth_storage import (
    CREDENTIAL_VERSION,
    VAULT_ACCOUNT,
    VAULT_LOGOUT_ACCOUNT,
    VAULT_SERVICE,
    CredentialMigrationError,
    CredentialStorageError,
    CredentialVaultError,
    OAuthTokenStore,
    UnsupportedCredentialVersionError,
    system_vault_type,
)
from utils import lock_file


def _load_fallback_in_fresh_process(
    credential_path: str,
    result_path: str,
) -> None:
    try:
        value = OAuthTokenStore(
            Path(credential_path),
            use_system_vault=False,
        ).load("client-a")
        result: dict[str, object] = {"value": value}
    except Exception as exc:
        result = {"error": type(exc).__name__}
    Path(result_path).write_text(json.dumps(result), encoding="utf8")


def _hold_interprocess_lock(
    lock_path: str,
    ready_path: str,
    release_path: str,
) -> None:
    acquired, handle = lock_file(Path(lock_path))
    if not acquired:
        handle.close()
        raise RuntimeError("child could not acquire credential lock")
    try:
        Path(ready_path).write_text("ready", encoding="utf8")
        deadline = time.monotonic() + 5
        while not Path(release_path).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("child lock release timed out")
            time.sleep(0.01)
    finally:
        handle.close()


class _MemoryVault:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.marker_value: str | None = None
        self.get_error: Exception | None = None
        self.set_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.marker_get_error: Exception | None = None
        self.marker_set_error: Exception | None = None
        self.marker_delete_error: Exception | None = None
        self.corrupt_writes = False
        self.calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username))
        if username == VAULT_LOGOUT_ACCOUNT:
            if self.marker_get_error is not None:
                raise self.marker_get_error
            return self.marker_value
        if self.get_error is not None:
            raise self.get_error
        return self.value

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None:
        self.calls.append(("set", service, username))
        if username == VAULT_LOGOUT_ACCOUNT:
            if self.marker_set_error is not None:
                raise self.marker_set_error
            self.marker_value = password
            return
        if self.set_error is not None:
            raise self.set_error
        self.value = "corrupt" if self.corrupt_writes else password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
        if username == VAULT_LOGOUT_ACCOUNT:
            if self.marker_delete_error is not None:
                raise self.marker_delete_error
            self.marker_value = None
            return
        if self.delete_error is not None:
            raise self.delete_error
        self.value = None


def _record(client_id: str, token: str, *, version: int = 1) -> str:
    return json.dumps(
        {
            "client_id": client_id,
            "refresh_token": token,
            "version": version,
        }
    )


def _legacy_record(client_id: str, token: str) -> str:
    return json.dumps({"client_id": client_id, "refresh_token": token})


def _current_record(client_id: str, token: str, *, vault: bool) -> str:
    return json.dumps(
        {
            "client_id": client_id,
            "provenance": "native" if vault else "fallback",
            "refresh_token": token,
            "version": CREDENTIAL_VERSION,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_record(encoded: str) -> dict[str, object]:
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise AssertionError("test credential record was not JSON") from exc
    if not isinstance(payload, dict):
        raise AssertionError("test credential record was not an object")
    return payload


class OAuthTokenStoreTests(unittest.TestCase):
    def test_vault_save_load_and_logout_clear_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault()
            store = OAuthTokenStore(path, vault=vault)

            store.save("client-a", "refresh-secret")

            self.assertFalse(path.exists())
            self.assertIsNotNone(vault.value)
            payload = _json_record(vault.value or "")
            self.assertEqual(payload["version"], CREDENTIAL_VERSION)
            self.assertEqual(store.load("client-a"), "refresh-secret")
            self.assertIsNone(store.load("client-b"))

            store.clear()

            self.assertIsNone(vault.value)
            self.assertFalse(path.exists())
            self.assertIn(("delete", VAULT_SERVICE, VAULT_ACCOUNT), vault.calls)

    def test_unversioned_legacy_record_migrates_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(
                '{"client_id":"client-a","refresh_token":"legacy"}',
                encoding="utf8",
            )
            vault = _MemoryVault()
            store = OAuthTokenStore(path, vault=vault)

            self.assertEqual(store.load("client-a"), "legacy")

            self.assertFalse(path.exists())
            self.assertEqual(
                OAuthTokenStore._decode_record(
                    vault.value or "",
                    "client-a",
                    allow_unversioned=False,
                    source="vault",
                ),
                "legacy",
            )

    def test_versioned_file_fallback_remains_readable_and_later_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            fallback = OAuthTokenStore(path, use_system_vault=False)

            fallback.save("client-a", "fallback-secret")

            payload = _json_record(path.read_text(encoding="utf8"))
            self.assertEqual(payload["version"], CREDENTIAL_VERSION)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(fallback.load("client-a"), "fallback-secret")

            vault = _MemoryVault()
            migrated = OAuthTokenStore(path, vault=vault)
            self.assertEqual(migrated.load("client-a"), "fallback-secret")
            self.assertFalse(path.exists())
            self.assertIsNotNone(vault.value)

    def test_existing_destination_wins_over_legacy_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(
                _legacy_record("client-a", "legacy"),
                encoding="utf8",
            )
            vault = _MemoryVault(_record("client-a", "destination"))
            store = OAuthTokenStore(path, vault=vault)

            self.assertEqual(store.load("client-a"), "destination")
            self.assertFalse(path.exists())
            self.assertFalse(any(call[0] == "set" for call in vault.calls))

    def test_mismatched_destination_wins_without_deleting_other_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(
                _legacy_record("client-a", "legacy"),
                encoding="utf8",
            )
            vault = _MemoryVault(_record("client-b", "destination"))
            store = OAuthTokenStore(path, vault=vault)

            self.assertIsNone(store.load("client-a"))
            self.assertTrue(path.exists())
            self.assertFalse(any(call[0] == "set" for call in vault.calls))

    def test_forward_vault_version_is_rejected_without_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault(
                _record("client-a", "future", version=CREDENTIAL_VERSION + 1)
            )
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(UnsupportedCredentialVersionError):
                store.load("client-a")

            self.assertTrue(path.exists())
            self.assertFalse(any(call[0] == "set" for call in vault.calls))

    def test_forward_vault_logout_marker_blocks_every_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            credential = _current_record("client-a", "vault-secret", vault=True)
            vault = _MemoryVault(credential)
            future_marker = json.dumps(
                {
                    "logout": True,
                    "version": oauth_storage.LOGOUT_MARKER_VERSION + 1,
                }
            )
            vault.marker_value = future_marker
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(UnsupportedCredentialVersionError):
                store.load("client-a")

            self.assertEqual(vault.value, credential)
            self.assertEqual(vault.marker_value, future_marker)

    def test_forward_file_version_is_rejected_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(
                _record("client-a", "future", version=CREDENTIAL_VERSION + 1),
                encoding="utf8",
            )
            vault = _MemoryVault()
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(UnsupportedCredentialVersionError):
                store.load("client-a")

            self.assertTrue(path.exists())
            self.assertIsNone(vault.value)

    def test_malformed_legacy_record_is_reported_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text("not-json", encoding="utf8")
            store = OAuthTokenStore(path, use_system_vault=False)

            with self.assertRaises(CredentialStorageError):
                store.load("client-a")

            self.assertEqual(path.read_text(encoding="utf8"), "not-json")

    def test_migration_write_failure_does_not_fall_back_or_delete_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault()
            vault.set_error = RuntimeError("backend rejected refresh-secret")
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(CredentialVaultError) as raised:
                store.load("client-a")

            self.assertNotIn("refresh-secret", str(raised.exception))
            self.assertTrue(path.exists())
            self.assertIsNone(vault.value)

    def test_failed_verification_preserves_unrecognized_value_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault()
            vault.corrupt_writes = True
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(oauth_storage.CredentialConflictError):
                store.load("client-a")

            self.assertTrue(path.exists())
            self.assertEqual(vault.value, "corrupt")

    def test_state_write_failure_conditionally_restores_previous_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            previous = _current_record("client-a", "previous", vault=True)
            vault = _MemoryVault(previous)
            store = OAuthTokenStore(path, vault=vault)

            with patch.object(
                store,
                "_write_state",
                side_effect=OSError("state unavailable"),
            ):
                with self.assertRaises(OSError):
                    store.save("client-a", "new")

            self.assertEqual(vault.value, previous)
            self.assertFalse(path.exists())

    def test_failed_source_removal_rolls_back_new_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault()
            store = OAuthTokenStore(path, vault=vault)

            with patch(
                "oauth_storage.remove_file",
                side_effect=OSError("read-only source"),
            ):
                with self.assertRaises(CredentialMigrationError):
                    store.load("client-a")

            self.assertTrue(path.exists())
            self.assertIsNone(vault.value)

    def test_explicit_file_fallback_eligibility_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault()
            vault.get_error = oauth_storage.NoKeyringError("no backend")
            store = OAuthTokenStore(
                path,
                vault=vault,
                allow_file_fallback=True,
            )

            store.save("client-a", "fallback-secret")

            self.assertTrue(path.exists())
            fresh = OAuthTokenStore(path, vault=vault)
            self.assertEqual(fresh.load("client-a"), "fallback-secret")
            state = _json_record(
                path.with_name("oauth.json.state").read_text(encoding="utf8")
            )
            self.assertIs(state["fallback_eligible"], True)

    def test_generic_vault_read_failure_never_uses_plaintext_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault()
            vault.get_error = RuntimeError("locked")
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(CredentialVaultError):
                store.load("client-a")

            self.assertTrue(path.exists())

    def test_logout_clears_file_when_system_vault_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            store = OAuthTokenStore(path, use_system_vault=False)
            store.save("client-a", "fallback-secret")

            store.clear()

            self.assertFalse(path.exists())

    def test_initial_tombstone_write_failure_blocks_fresh_vault_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault()
            store = OAuthTokenStore(path, vault=vault)
            store.save("client-a", "vault-secret")
            vault.delete_error = RuntimeError("vault locked")

            with patch.object(
                store,
                "_write_state",
                side_effect=OSError("state unavailable"),
            ):
                with self.assertRaises(
                    oauth_storage.CredentialCleanupError
                ) as raised:
                    store.clear()

            self.assertTrue(raised.exception.tombstone_persisted)
            self.assertTrue(path.with_name("oauth.json.logout").exists())
            self.assertIsNotNone(vault.value)

            vault.delete_error = None
            fresh = OAuthTokenStore(path, vault=vault)
            self.assertIsNone(fresh.load("client-a"))
            self.assertIsNone(vault.value)
            self.assertFalse(path.with_name("oauth.json.logout").exists())

    def test_vault_marker_survives_correlated_file_marker_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault()
            store = OAuthTokenStore(path, vault=vault)
            store.save("client-a", "vault-secret")
            vault.delete_error = RuntimeError("vault locked")

            with (
                patch.object(
                    store,
                    "_write_logout_marker",
                    side_effect=OSError("directory read-only"),
                ),
                patch.object(
                    store,
                    "_write_state",
                    side_effect=OSError("directory read-only"),
                ),
            ):
                with self.assertRaises(
                    oauth_storage.CredentialCleanupError
                ) as raised:
                    store.clear()

            self.assertTrue(raised.exception.tombstone_persisted)
            self.assertTrue(raised.exception.vault_pending)
            self.assertLess(
                vault.calls.index(
                    ("set", VAULT_SERVICE, VAULT_LOGOUT_ACCOUNT)
                ),
                vault.calls.index(("delete", VAULT_SERVICE, VAULT_ACCOUNT)),
            )
            self.assertFalse(path.with_name("oauth.json.logout").exists())
            self.assertIsNotNone(vault.value)
            self.assertEqual(
                _json_record(vault.marker_value or ""),
                {
                    "logout": True,
                    "version": oauth_storage.LOGOUT_MARKER_VERSION,
                },
            )

            vault.delete_error = None
            fresh = OAuthTokenStore(path, vault=vault)
            self.assertIsNone(fresh.load("client-a"))
            self.assertIsNone(vault.value)
            self.assertIsNone(vault.marker_value)

    def test_initial_tombstone_write_failure_blocks_fresh_fallback_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            store = OAuthTokenStore(path, use_system_vault=False)
            store.save("client-a", "fallback-secret")

            with (
                patch.object(
                    store,
                    "_write_state",
                    side_effect=OSError("state unavailable"),
                ),
                patch.object(
                    store,
                    "_delete_file",
                    side_effect=OSError("file locked"),
                ),
            ):
                with self.assertRaises(
                    oauth_storage.CredentialCleanupError
                ) as raised:
                    store.clear()

            self.assertTrue(raised.exception.tombstone_persisted)
            self.assertTrue(path.with_name("oauth.json.logout").exists())
            self.assertTrue(path.exists())

            result_path = path.with_name("fresh-process-result.json")
            process = multiprocessing.get_context("spawn").Process(
                target=_load_fallback_in_fresh_process,
                args=(str(path), str(result_path)),
            )
            process.start()
            process.join(5)

            self.assertEqual(process.exitcode, 0)
            self.assertEqual(
                _json_record(result_path.read_text(encoding="utf8")),
                {"value": None},
            )
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("oauth.json.logout").exists())

    def test_logout_attempts_file_cleanup_when_vault_deletion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "fallback"), encoding="utf8")
            vault = _MemoryVault(_record("client-a", "vault"))
            vault.delete_error = RuntimeError("locked")
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(CredentialStorageError):
                store.clear()

            self.assertFalse(path.exists())
            self.assertIsNotNone(vault.value)

    def test_save_does_not_follow_a_legacy_temporary_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "oauth.json"
            target = root / "outside.txt"
            target.write_text("protected", encoding="utf8")
            path.with_name("oauth.json.new").symlink_to(target)
            store = OAuthTokenStore(path, use_system_vault=False)

            store.save("client-a", "fresh")

            self.assertEqual(store.load("client-a"), "fresh")
            self.assertEqual(target.read_text(encoding="utf8"), "protected")
            self.assertFalse(path.with_name("oauth.json.new").exists())

    def test_save_removes_an_obsolete_partial_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            temporary = path.with_name("oauth.json.new")
            temporary.write_text("partial", encoding="utf8")
            store = OAuthTokenStore(path, use_system_vault=False)

            store.save("client-a", "fresh")

            self.assertEqual(store.load("client-a"), "fresh")
            self.assertFalse(temporary.exists())

    def test_interprocess_lock_blocks_complete_fallback_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "oauth.json"
            ready = root / "ready"
            release = root / "release"
            lock_path = root / "oauth.json.lock"
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_hold_interprocess_lock,
                args=(str(lock_path), str(ready), str(release)),
            )
            process.start()
            deadline = time.monotonic() + 5
            while not ready.exists():
                if not process.is_alive():
                    self.fail("lock-holder process exited early")
                if time.monotonic() >= deadline:
                    self.fail("lock-holder process did not become ready")
                time.sleep(0.01)

            store = OAuthTokenStore(path, use_system_vault=False)
            failure: list[BaseException] = []

            def save() -> None:
                try:
                    store.save("client-a", "secret")
                except BaseException as exc:
                    failure.append(exc)

            thread = threading.Thread(target=save)
            thread.start()
            time.sleep(0.1)
            self.assertTrue(thread.is_alive())
            self.assertFalse(path.exists())
            release.write_text("release", encoding="utf8")
            process.join(5)
            thread.join(5)

            self.assertEqual(process.exitcode, 0)
            self.assertFalse(failure)
            self.assertFalse(thread.is_alive())
            self.assertEqual(store.load("client-a"), "secret")

    def test_concurrent_migration_and_save_preserve_newest_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            migration_started = threading.Event()
            release_migration = threading.Event()

            class BlockingVault(_MemoryVault):
                def set_password(
                    self,
                    service: str,
                    username: str,
                    password: str,
                ) -> None:
                    if not migration_started.is_set():
                        migration_started.set()
                        self.assert_release()
                    super().set_password(service, username, password)

                @staticmethod
                def assert_release() -> None:
                    if not release_migration.wait(2):
                        raise AssertionError("migration release timed out")

            vault = BlockingVault()
            migrating = OAuthTokenStore(path, vault=vault)
            saving = OAuthTokenStore(path, vault=vault)
            migration_result: list[str | None] = []
            failures: list[BaseException] = []

            def migrate() -> None:
                try:
                    migration_result.append(migrating.load("client-a"))
                except BaseException as exc:
                    failures.append(exc)

            def save_newest() -> None:
                try:
                    saving.save("client-a", "newest")
                except BaseException as exc:
                    failures.append(exc)

            migration_thread = threading.Thread(target=migrate)
            save_thread = threading.Thread(target=save_newest)
            migration_thread.start()
            self.assertTrue(migration_started.wait(2))
            save_thread.start()
            time.sleep(0.05)
            self.assertTrue(save_thread.is_alive())
            release_migration.set()
            migration_thread.join(2)
            save_thread.join(2)

            self.assertFalse(failures)
            self.assertEqual(migration_result, ["legacy"])
            self.assertFalse(path.exists())
            final = OAuthTokenStore._parse_record(
                vault.value or "",
                source="vault",
            )
            self.assertEqual(final.refresh_token, "newest")

    def test_conditional_rollback_preserves_external_concurrent_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault()
            store = OAuthTokenStore(path, vault=vault)
            concurrent = _current_record("client-a", "newest", vault=True)

            def concurrent_write_then_fail() -> None:
                vault.value = concurrent
                raise OSError("source removal failed")

            with patch.object(
                store,
                "_delete_file",
                side_effect=concurrent_write_then_fail,
            ):
                with self.assertRaises(oauth_storage.CredentialConflictError):
                    store.load("client-a")

            self.assertEqual(vault.value, concurrent)
            self.assertTrue(path.exists())

    def test_v1_vault_upgrade_outage_cannot_create_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault(_record("client-a", "v1-vault", version=1))
            vault.get_error = oauth_storage.NoKeyringError("provider offline")
            outage = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(
                oauth_storage.CredentialFallbackEligibilityError
            ):
                outage.load("client-a")
            with self.assertRaises(
                oauth_storage.CredentialFallbackEligibilityError
            ):
                outage.save("client-a", "new-session", new_session=True)

            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("oauth.json.state").exists())

            vault.get_error = None
            recovered = OAuthTokenStore(path, vault=vault)
            self.assertEqual(recovered.load("client-a"), "v1-vault")
            self.assertFalse(path.exists())

    def test_provisioned_vault_outage_fails_closed_and_logout_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault()
            available = OAuthTokenStore(path, vault=vault)
            available.save("client-a", "old-vault")
            outage = OAuthTokenStore(path, use_system_vault=False)

            with self.assertRaises(
                oauth_storage.CredentialVaultUnavailableError
            ):
                outage.save("client-a", "new-during-outage")
            self.assertFalse(path.exists())

            with self.assertRaises(
                oauth_storage.CredentialCleanupError
            ) as raised:
                outage.clear()
            self.assertTrue(raised.exception.vault_pending)

            recovered = OAuthTokenStore(path, vault=vault)
            self.assertIsNone(recovered.load("client-a"))
            self.assertIsNone(vault.value)
            with self.assertRaises(oauth_storage.CredentialLoggedOutError):
                recovered.save("client-a", "stale-rotation")

            recovered.save(
                "client-a",
                "new-login",
                new_session=True,
            )
            self.assertEqual(recovered.load("client-a"), "new-login")

    def test_recovery_preserves_differing_provenance_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            fallback = OAuthTokenStore(path, use_system_vault=False)
            fallback.save("client-a", "new-during-outage")
            vault_value = _current_record("client-a", "old-vault", vault=True)
            vault = _MemoryVault(vault_value)
            recovered = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(oauth_storage.CredentialConflictError):
                recovered.load("client-a")

            self.assertEqual(vault.value, vault_value)
            self.assertTrue(path.exists())
            preserved = OAuthTokenStore._parse_record(
                path.read_text(encoding="utf8"),
                source="file",
            )
            self.assertEqual(
                preserved.refresh_token,
                "new-during-outage",
            )

    def test_save_rejects_forward_vault_and_file_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            future = _record(
                "client-a",
                "future",
                version=CREDENTIAL_VERSION + 1,
            )
            vault = _MemoryVault(future)
            vault_store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(UnsupportedCredentialVersionError):
                vault_store.save("client-a", "older-writer")
            self.assertEqual(vault.value, future)

            path.write_text(future, encoding="utf8")
            file_store = OAuthTokenStore(path, use_system_vault=False)
            with self.assertRaises(UnsupportedCredentialVersionError):
                file_store.save("client-a", "older-writer")
            self.assertEqual(path.read_text(encoding="utf8"), future)

    def test_concurrent_forward_writer_is_serialized_before_older_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault()
            older = OAuthTokenStore(path, vault=vault)
            newer = OAuthTokenStore(path, vault=vault)
            future = _record(
                "client-a",
                "future",
                version=CREDENTIAL_VERSION + 1,
            )
            newer_holds_lock = threading.Event()
            release_newer = threading.Event()
            older_error: list[BaseException] = []

            def write_future() -> None:
                with newer._transaction():
                    vault.value = future
                    newer_holds_lock.set()
                    if not release_newer.wait(2):
                        raise AssertionError("newer writer release timed out")

            def save_older() -> None:
                try:
                    older.save("client-a", "older")
                except BaseException as exc:
                    older_error.append(exc)

            newer_thread = threading.Thread(target=write_future)
            older_thread = threading.Thread(target=save_older)
            newer_thread.start()
            self.assertTrue(newer_holds_lock.wait(2))
            older_thread.start()
            time.sleep(0.05)
            self.assertTrue(older_thread.is_alive())
            release_newer.set()
            newer_thread.join(2)
            older_thread.join(2)

            self.assertEqual(len(older_error), 1)
            self.assertIsInstance(
                older_error[0],
                UnsupportedCredentialVersionError,
            )
            self.assertEqual(vault.value, future)

    def test_forward_state_version_blocks_every_save_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            state_path = path.with_name("oauth.json.state")
            future_state = json.dumps(
                {
                    "cleanup": "none",
                    "vault_provisioned": False,
                    "version": oauth_storage.STATE_VERSION + 1,
                }
            )
            state_path.write_text(future_state, encoding="utf8")
            store = OAuthTokenStore(path, use_system_vault=False)

            with self.assertRaises(UnsupportedCredentialVersionError):
                store.save("client-a", "older")

            self.assertEqual(
                state_path.read_text(encoding="utf8"),
                future_state,
            )
            self.assertFalse(path.exists())

    def test_failed_logout_tombstone_prevents_automatic_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault(_current_record("client-a", "secret", vault=True))
            vault.delete_error = RuntimeError("locked")
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(oauth_storage.CredentialCleanupError):
                store.clear()
            with self.assertRaises(oauth_storage.CredentialCleanupError):
                store.load("client-a")

            vault.delete_error = None
            self.assertIsNone(store.load("client-a"))
            self.assertIsNone(vault.value)
            with self.assertRaises(oauth_storage.CredentialLoggedOutError):
                store.save("client-a", "stale")

    def test_frozen_platform_mapping_uses_only_native_backends(self) -> None:
        self.assertEqual(
            system_vault_type("win32").__module__,  # type: ignore[union-attr]
            "keyring.backends.Windows",
        )
        self.assertEqual(
            system_vault_type("darwin").__module__,  # type: ignore[union-attr]
            "keyring.backends.macOS",
        )
        self.assertEqual(
            system_vault_type("linux").__module__,  # type: ignore[union-attr]
            "keyring.backends.SecretService",
        )
        self.assertIsNone(system_vault_type("freebsd"))


if __name__ == "__main__":
    unittest.main()
