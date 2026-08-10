from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import oauth_storage
from oauth_storage import (
    CREDENTIAL_VERSION,
    VAULT_ACCOUNT,
    VAULT_SERVICE,
    CredentialMigrationError,
    CredentialStorageError,
    CredentialVaultError,
    OAuthTokenStore,
    UnsupportedCredentialVersionError,
    system_vault_type,
)


class _MemoryVault:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.get_error: Exception | None = None
        self.set_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.corrupt_writes = False
        self.calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username))
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
        if self.set_error is not None:
            raise self.set_error
        self.value = "corrupt" if self.corrupt_writes else password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
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
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault(_record("client-a", "destination"))
            store = OAuthTokenStore(path, vault=vault)

            self.assertEqual(store.load("client-a"), "destination")
            self.assertFalse(path.exists())
            self.assertFalse(any(call[0] == "set" for call in vault.calls))

    def test_mismatched_destination_wins_without_deleting_other_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
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

    def test_failed_migration_verification_rolls_back_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(_record("client-a", "legacy"), encoding="utf8")
            vault = _MemoryVault()
            vault.corrupt_writes = True
            store = OAuthTokenStore(path, vault=vault)

            with self.assertRaises(CredentialVaultError):
                store.load("client-a")

            self.assertTrue(path.exists())
            self.assertIsNone(vault.value)

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

    def test_no_keyring_error_is_the_only_operational_file_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            vault = _MemoryVault()
            vault.get_error = oauth_storage.NoKeyringError("no backend")
            store = OAuthTokenStore(path, vault=vault)

            store.save("client-a", "fallback-secret")

            self.assertTrue(path.exists())
            self.assertEqual(store.load("client-a"), "fallback-secret")

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
