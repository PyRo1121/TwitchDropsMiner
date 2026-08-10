"""Transactional, versioned OAuth storage backed by the native OS vault.

The maintained ``keyring`` package supplies fixed platform adapters for Windows
Credential Manager, macOS Keychain, and Freedesktop Secret Service on Linux.
All credential and sidecar-state operations are serialized by an in-process and
cross-process lock.  A mode-0600 file is used only when no native provider has
ever been provisioned for this application.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Final, Iterator, Protocol, cast

from keyring.backend import KeyringBackend
from keyring.backends.SecretService import Keyring as SecretServiceKeyring
from keyring.backends.Windows import WinVaultKeyring
from keyring.backends.macOS import Keyring as MacOSKeyring
from keyring.errors import NoKeyringError

from utils import atomic_write, lock_file, remove_file


CREDENTIAL_VERSION: Final = 2
STATE_VERSION: Final = 2
LOGOUT_MARKER_VERSION: Final = 1
VAULT_SERVICE: Final = "io.github.devilxd.twitchdropsminer.oauth"
VAULT_ACCOUNT: Final = "twitch-session"
VAULT_LOGOUT_ACCOUNT: Final = "twitch-session-logout"
_MAX_CREDENTIAL_BYTES: Final = 1024 * 1024
_MAX_STATE_BYTES: Final = 16 * 1024
_LOCK_TIMEOUT_SECONDS: Final = 30.0
_LOCK_RETRY_SECONDS: Final = 0.05
_PROVENANCE_LEGACY: Final = "legacy"
_PROVENANCE_FALLBACK: Final = "fallback"
_PROVENANCE_NATIVE: Final = "native"
_CLEANUP_NONE: Final = "none"
_CLEANUP_PENDING: Final = "pending"
_CLEANUP_LOGGED_OUT: Final = "logged_out"


class CredentialStorageError(RuntimeError):
    """A credential could not be handled without risking its integrity."""


class CredentialVaultError(CredentialStorageError):
    """The selected native credential vault failed an operation."""


class CredentialVaultUnavailableError(CredentialStorageError):
    """The native vault is unavailable and plaintext fallback is unsafe."""


class CredentialFallbackEligibilityError(CredentialVaultUnavailableError):
    """File fallback requires an explicit, durable eligibility decision."""


class CredentialMigrationError(CredentialStorageError):
    """A credential could not be migrated transactionally."""


class CredentialConflictError(CredentialStorageError):
    """Independent credential writers produced conflicting durable values."""


class CredentialLoggedOutError(CredentialStorageError):
    """A logout tombstone rejected automatic credential persistence."""


class CredentialTransactionError(CredentialStorageError):
    """The cross-process credential transaction lock could not be acquired."""


class UnsupportedCredentialVersionError(CredentialStorageError):
    """A record was written by a newer, unsupported application version."""


class CredentialCleanupError(CredentialStorageError):
    """Local logout did not yet reach a fully verified durable state."""

    def __init__(
        self,
        *,
        vault_pending: bool,
        file_pending: bool,
        marker_pending: bool,
        tombstone_persisted: bool,
    ) -> None:
        self.vault_pending = vault_pending
        self.file_pending = file_pending
        self.marker_pending = marker_pending
        self.tombstone_persisted = tombstone_persisted
        super().__init__("Local OAuth credential cleanup remains pending")


class CredentialVault(Protocol):
    """The typed subset of ``keyring.backend.KeyringBackend`` used here."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True, repr=False)
class _CredentialRecord:
    version: int
    client_id: str
    refresh_token: str
    provenance: str


@dataclass(frozen=True)
class _StorageState:
    version: int = STATE_VERSION
    vault_provisioned: bool = False
    fallback_eligible: bool = False
    cleanup: str = _CLEANUP_NONE


class _VaultMarkerStatus(Enum):
    ABSENT = "absent"
    PRESENT = "present"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _CleanupTombstones:
    """Durable logout evidence grouped by its availability boundary."""

    state: bool = False
    file: bool = False
    vault: _VaultMarkerStatus = _VaultMarkerStatus.ABSENT

    def safe_to_close(
        self,
        *,
        fallback_credential_pending: bool,
    ) -> bool:
        # A vault-only marker can disappear in the same provider outage that
        # makes a retained fallback credential eligible for use. File-backed
        # evidence remains visible wherever that credential remains readable.
        return (
            self.state
            or self.file
            or (
                self.vault is _VaultMarkerStatus.PRESENT
                and not fallback_credential_pending
            )
        )


_SYSTEM_VAULT_TYPES: Final[dict[str, type[KeyringBackend]]] = {
    "linux": SecretServiceKeyring,
    "darwin": MacOSKeyring,
    "win32": WinVaultKeyring,
}
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def system_vault_type(platform: str) -> type[KeyringBackend] | None:
    """Return the frozen, native backend type selected for ``platform``."""
    return _SYSTEM_VAULT_TYPES.get(platform)


def _create_system_vault() -> CredentialVault | None:
    """Create only a recommended native backend, never an alternate keyring."""
    vault_type = system_vault_type(sys.platform)
    if vault_type is None:
        return None
    try:
        if vault_type.priority < 1:
            return None
        return cast(CredentialVault, vault_type())
    except (ImportError, RuntimeError):
        # Backend priority is keyring's documented availability probe.  The
        # durable state sidecar decides whether file fallback is still safe.
        return None


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.abspath(path)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


class OAuthTokenStore:
    """Persist one versioned Twitch OAuth session credential."""

    def __init__(
        self,
        path: Path,
        *,
        vault: CredentialVault | None = None,
        use_system_vault: bool = True,
        allow_file_fallback: bool = False,
    ) -> None:
        self._path = path
        self._state_path = path.with_name(f"{path.name}.state")
        self._logout_marker_path = path.with_name(f"{path.name}.logout")
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._process_lock = _process_lock(self._lock_path)
        self._explicit_file_fallback = allow_file_fallback or not use_system_vault
        self._vault = (
            _create_system_vault()
            if vault is None and use_system_vault
            else vault
        )

    def load(self, client_id: str) -> str | None:
        """Load or migrate a token under one complete transaction."""
        self._require_client_id(client_id)
        with self._transaction():
            return self._load_locked(client_id)

    def save(
        self,
        client_id: str,
        refresh_token: str,
        *,
        new_session: bool = False,
    ) -> None:
        """Persist a token without overwriting forward or conflicting state.

        ``new_session`` is reserved for a newly-authorized device flow.  It is
        the only write allowed to retire a completed logout tombstone.
        """
        self._require_client_id(client_id)
        if not refresh_token:
            raise ValueError("OAuth token records require non-empty values")
        with self._transaction():
            self._save_locked(
                client_id,
                refresh_token,
                new_session=new_session,
            )

    def clear(self) -> None:
        """Durably tombstone logout, then confirm vault and file deletion."""
        with self._transaction():
            state, tombstones = self._read_cleanup_state()
            pending = replace(state, cleanup=_CLEANUP_PENDING)
            file_marker_written = False
            try:
                self._write_logout_marker()
            except (CredentialStorageError, OSError):
                file_marker_written = False
            else:
                file_marker_written = True
            if file_marker_written:
                tombstones = replace(tombstones, file=True)

            state_marker_written = False
            try:
                self._write_state(pending)
            except (CredentialStorageError, OSError):
                state_marker_written = False
            else:
                state_marker_written = True
            if state_marker_written:
                tombstones = replace(tombstones, state=True)

            vault_marker_written = False
            try:
                self._write_vault_logout_marker()
            except CredentialStorageError:
                vault_marker_written = False
            else:
                vault_marker_written = True
            if vault_marker_written:
                tombstones = replace(
                    tombstones,
                    vault=_VaultMarkerStatus.PRESENT,
                )
            self._complete_cleanup(
                pending,
                tombstones=tombstones,
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._process_lock:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            lock_handle: io.TextIOWrapper | None = None
            while lock_handle is None:
                try:
                    acquired, candidate = lock_file(self._lock_path)
                except (OSError, RuntimeError):
                    raise CredentialTransactionError(
                        "Credential transaction lock could not be opened"
                    ) from None
                if acquired:
                    lock_handle = candidate
                    break
                candidate.close()
                if time.monotonic() >= deadline:
                    raise CredentialTransactionError(
                        "Credential transaction lock timed out"
                    )
                time.sleep(_LOCK_RETRY_SECONDS)
            try:
                yield
            finally:
                lock_handle.close()

    def _load_locked(self, client_id: str) -> str | None:
        state, tombstones = self._read_cleanup_state()
        if state.cleanup != _CLEANUP_NONE:
            self._complete_cleanup(
                state,
                tombstones=tombstones,
            )
            return None

        if tombstones.vault is _VaultMarkerStatus.UNAVAILABLE:
            # Keep the first marker observation authoritative for this entire
            # transaction. A provider recovery before the credential lookup
            # must not expose a credential hidden behind an unknown marker.
            return self._load_fallback_or_fail(state, client_id)
        if self._vault is None:
            return self._load_fallback_or_fail(state, client_id)
        try:
            destination_raw = self._vault_get()
        except CredentialVaultUnavailableError:
            return self._load_fallback_or_fail(state, client_id)

        if destination_raw is not None:
            provisioned = replace(state, vault_provisioned=True)
            if provisioned != state:
                self._write_state(provisioned)
            destination = self._parse_record(destination_raw, source="vault")
            source = self._read_file_record()
            self._reconcile_source(destination, source)
            return (
                destination.refresh_token
                if destination.client_id == client_id
                else None
            )

        source = self._read_file_record()
        if source is None or source.client_id != client_id:
            return None
        encoded = self._encode_record(
            client_id,
            source.refresh_token,
            provenance=_PROVENANCE_NATIVE,
        )
        try:
            raced_destination = self._write_vault(
                encoded,
                only_if_missing=True,
            )
        except CredentialVaultUnavailableError:
            return self._load_fallback_or_fail(state, client_id)
        if raced_destination is not None:
            provisioned = replace(state, vault_provisioned=True)
            self._write_state(provisioned)
            destination = self._parse_record(
                raced_destination,
                source="vault",
            )
            self._reconcile_source(destination, source)
            return (
                destination.refresh_token
                if destination.client_id == client_id
                else None
            )

        provisioned = replace(state, vault_provisioned=True)
        try:
            self._write_state(provisioned)
        except (CredentialStorageError, OSError):
            self._rollback_vault(None, encoded)
            raise
        try:
            self._delete_file()
        except OSError:
            self._rollback_vault(None, encoded)
            raise CredentialMigrationError(
                "OAuth migration could not remove its source"
            ) from None
        return source.refresh_token

    def _save_locked(
        self,
        client_id: str,
        refresh_token: str,
        *,
        new_session: bool,
    ) -> None:
        state, tombstones = self._read_cleanup_state()
        if state.cleanup != _CLEANUP_NONE:
            state = self._complete_cleanup(
                state,
                tombstones=tombstones,
            )
            if not new_session:
                raise CredentialLoggedOutError(
                    "Logout tombstone rejected automatic credential reuse"
                )

        if (
            tombstones.vault is _VaultMarkerStatus.UNAVAILABLE
            or self._vault is None
        ):
            self._save_fallback_or_fail(
                state,
                client_id,
                refresh_token,
                new_session=new_session,
            )
            return
        try:
            previous = self._vault_get()
        except CredentialVaultUnavailableError:
            self._save_fallback_or_fail(
                state,
                client_id,
                refresh_token,
                new_session=new_session,
            )
            return

        source = self._read_file_record()
        previous_record = (
            self._parse_record(previous, source="vault")
            if previous is not None
            else None
        )
        if previous_record is not None and source is not None:
            if (
                source.provenance != _PROVENANCE_LEGACY
                and (
                    source.client_id != previous_record.client_id
                    or source.refresh_token != previous_record.refresh_token
                )
            ):
                raise CredentialConflictError(
                    "Vault and fallback credentials conflict"
                )

        encoded = self._encode_record(
            client_id,
            refresh_token,
            provenance=_PROVENANCE_NATIVE,
        )
        self._write_vault(encoded)
        target_state = _StorageState(
            vault_provisioned=True,
            fallback_eligible=state.fallback_eligible,
            cleanup=_CLEANUP_NONE,
        )
        try:
            self._write_state(target_state)
        except (CredentialStorageError, OSError):
            self._rollback_vault(previous, encoded)
            raise
        try:
            self._delete_file()
        except OSError:
            # Keep the verified newest token.  The provenance-aware read path
            # will never discard a differing fallback on recovery.
            raise CredentialMigrationError(
                "OAuth credential was secured but fallback cleanup failed"
            ) from None

    def _load_fallback_or_fail(
        self,
        state: _StorageState,
        client_id: str,
    ) -> str | None:
        if state.vault_provisioned:
            raise CredentialVaultUnavailableError(
                "Provisioned OS credential vault is temporarily unavailable"
            )
        source = self._read_file_record()
        state = self._ensure_fallback_eligibility(
            state,
            source_exists=source is not None,
        )
        if source is None or source.client_id != client_id:
            return None
        return source.refresh_token

    def _save_fallback_or_fail(
        self,
        state: _StorageState,
        client_id: str,
        refresh_token: str,
        *,
        new_session: bool,
    ) -> None:
        if state.vault_provisioned:
            raise CredentialVaultUnavailableError(
                "Provisioned OS credential vault is temporarily unavailable"
            )
        previous = self._read_file_text()
        if previous is not None:
            self._parse_record(previous, source="file")
        state = self._ensure_fallback_eligibility(
            state,
            source_exists=previous is not None,
        )
        encoded = self._encode_record(
            client_id,
            refresh_token,
            provenance=_PROVENANCE_FALLBACK,
        )
        self._save_file(encoded)
        if new_session and state.cleanup != _CLEANUP_NONE:
            try:
                self._write_state(replace(state, cleanup=_CLEANUP_NONE))
            except (CredentialStorageError, OSError):
                self._rollback_file(previous, encoded)
                raise

    def _ensure_fallback_eligibility(
        self,
        state: _StorageState,
        *,
        source_exists: bool,
    ) -> _StorageState:
        if state.fallback_eligible:
            return state
        if not source_exists and not self._explicit_file_fallback:
            raise CredentialFallbackEligibilityError(
                "File fallback eligibility is unknown while the vault is unavailable"
            )
        eligible = replace(state, fallback_eligible=True)
        self._write_state(eligible)
        return eligible

    def _read_cleanup_state(
        self,
    ) -> tuple[_StorageState, _CleanupTombstones]:
        state = self._read_state()
        file_marker_exists = self._read_logout_marker()
        try:
            vault_marker_exists = self._read_vault_logout_marker()
        except CredentialVaultUnavailableError:
            vault_marker_status = _VaultMarkerStatus.UNAVAILABLE
        else:
            vault_marker_status = (
                _VaultMarkerStatus.PRESENT
                if vault_marker_exists
                else _VaultMarkerStatus.ABSENT
            )
        tombstones = _CleanupTombstones(
            state=state.cleanup != _CLEANUP_NONE,
            file=file_marker_exists,
            vault=vault_marker_status,
        )
        if (
            file_marker_exists
            or vault_marker_status is _VaultMarkerStatus.PRESENT
        ):
            state = replace(state, cleanup=_CLEANUP_PENDING)
        return state, tombstones

    def _complete_cleanup(
        self,
        state: _StorageState,
        *,
        tombstones: _CleanupTombstones,
    ) -> _StorageState:
        pending = replace(state, cleanup=_CLEANUP_PENDING)

        vault_pending = False
        if self._vault is None:
            vault_pending = state.vault_provisioned
        else:
            try:
                current = self._vault_get()
                if current is not None:
                    self._vault_delete()
                if self._vault_get() is not None:
                    vault_pending = True
            except CredentialStorageError:
                vault_pending = True

        file_pending = False
        try:
            self._delete_file()
            file_pending = os.path.lexists(self._path)
        except OSError:
            file_pending = True

        final_state = replace(
            pending,
            cleanup=(
                _CLEANUP_PENDING
                if vault_pending or file_pending
                else _CLEANUP_LOGGED_OUT
            ),
        )
        state_pending = False
        try:
            self._write_state(final_state)
        except (CredentialStorageError, OSError):
            state_pending = True
        else:
            tombstones = replace(tombstones, state=True)

        marker_pending = False
        if not state_pending:
            try:
                self._delete_logout_marker()
                marker_pending = os.path.lexists(self._logout_marker_path)
            except OSError:
                marker_pending = True

        if (
            not state_pending
            and not vault_pending
            and not file_pending
            and not marker_pending
        ):
            try:
                self._delete_vault_logout_marker()
            except CredentialStorageError as exc:
                if not isinstance(exc, CredentialVaultUnavailableError):
                    marker_pending = True
                elif state.vault_provisioned:
                    marker_pending = True

        if vault_pending or file_pending or state_pending or marker_pending:
            fallback_credential_pending = (
                file_pending and not state.vault_provisioned
            )
            raise CredentialCleanupError(
                vault_pending=vault_pending,
                file_pending=file_pending,
                marker_pending=marker_pending or state_pending,
                tombstone_persisted=tombstones.safe_to_close(
                    fallback_credential_pending=fallback_credential_pending,
                ),
            )
        return final_state

    def _reconcile_source(
        self,
        destination: _CredentialRecord,
        source: _CredentialRecord | None,
    ) -> None:
        if source is None:
            return
        same_credential = (
            source.client_id == destination.client_id
            and source.refresh_token == destination.refresh_token
        )
        destination_wins_legacy = (
            source.provenance == _PROVENANCE_LEGACY
            and source.client_id == destination.client_id
        )
        if same_credential or destination_wins_legacy:
            try:
                self._delete_file()
            except OSError:
                raise CredentialMigrationError(
                    "Legacy OAuth credential cleanup failed"
                ) from None
            return
        if source.provenance != _PROVENANCE_LEGACY:
            raise CredentialConflictError(
                "Vault and fallback credentials conflict"
            )

    @staticmethod
    def _require_client_id(client_id: str) -> None:
        if not client_id:
            raise ValueError("OAuth token records require a non-empty client ID")

    @staticmethod
    def _encode_record(
        client_id: str,
        refresh_token: str,
        *,
        provenance: str = _PROVENANCE_NATIVE,
    ) -> str:
        return json.dumps(
            {
                "client_id": client_id,
                "provenance": provenance,
                "refresh_token": refresh_token,
                "version": CREDENTIAL_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _parse_record(encoded: str, *, source: str) -> _CredentialRecord:
        try:
            payload: Any = json.loads(encoded)
        except (TypeError, ValueError):
            raise CredentialStorageError(
                f"Stored OAuth {source} record is malformed"
            ) from None
        if not isinstance(payload, dict):
            raise CredentialStorageError(
                f"Stored OAuth {source} record is malformed"
            )

        version = payload.get("version", 0)
        if type(version) is not int:
            raise CredentialStorageError(
                f"Stored OAuth {source} record has an invalid version"
            )
        if version > CREDENTIAL_VERSION:
            raise UnsupportedCredentialVersionError(
                f"Stored OAuth {source} record uses a newer version"
            )
        if version not in (0, 1, CREDENTIAL_VERSION):
            raise CredentialStorageError(
                f"Stored OAuth {source} record uses an unsupported version"
            )

        client_id = payload.get("client_id")
        refresh_token = payload.get("refresh_token")
        if (
            not isinstance(client_id, str)
            or not client_id
            or not isinstance(refresh_token, str)
            or not refresh_token
        ):
            raise CredentialStorageError(
                f"Stored OAuth {source} record is malformed"
            )
        if version == 0:
            provenance = (
                _PROVENANCE_LEGACY
                if source == "file"
                else _PROVENANCE_NATIVE
            )
        elif version == 1:
            # Version 1 was emitted by the first vault release.  Its file form
            # was app-created fallback, while its vault form was native.
            provenance = (
                _PROVENANCE_FALLBACK
                if source == "file"
                else _PROVENANCE_NATIVE
            )
        else:
            provenance = payload.get("provenance")
            expected = (
                _PROVENANCE_FALLBACK
                if source == "file"
                else _PROVENANCE_NATIVE
            )
            if provenance != expected:
                raise CredentialStorageError(
                    f"Stored OAuth {source} record has invalid provenance"
                )
        return _CredentialRecord(
            version=version,
            client_id=client_id,
            refresh_token=refresh_token,
            provenance=cast(str, provenance),
        )

    @staticmethod
    def _decode_record(
        encoded: str,
        client_id: str,
        *,
        allow_unversioned: bool,
        source: str,
    ) -> str | None:
        record = OAuthTokenStore._parse_record(encoded, source=source)
        if not allow_unversioned and record.version == 0:
            raise CredentialStorageError(
                f"Stored OAuth {source} record is unversioned"
            )
        return record.refresh_token if record.client_id == client_id else None

    def _read_file_record(self) -> _CredentialRecord | None:
        encoded = self._read_file_text()
        return (
            self._parse_record(encoded, source="file")
            if encoded is not None
            else None
        )

    def _read_file_text(self) -> str | None:
        return self._read_bounded_text(
            self._path,
            maximum=_MAX_CREDENTIAL_BYTES,
            label="OAuth credential",
            symlink_is_missing=True,
        )

    def _save_file(self, encoded: str) -> None:
        self._atomic_text(self._path, encoded)

    def _delete_file(self) -> None:
        remove_file(self._path)

    @staticmethod
    def _encode_logout_marker() -> str:
        return json.dumps(
            {"logout": True, "version": LOGOUT_MARKER_VERSION},
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _validate_logout_marker(encoded: str, *, source: str) -> None:
        try:
            payload: Any = json.loads(encoded)
        except (TypeError, ValueError):
            raise CredentialStorageError(
                f"{source} logout marker is malformed"
            ) from None
        if not isinstance(payload, dict):
            raise CredentialStorageError(f"{source} logout marker is malformed")
        version = payload.get("version")
        if type(version) is not int:
            raise CredentialStorageError(
                f"{source} logout marker has an invalid version"
            )
        if version > LOGOUT_MARKER_VERSION:
            raise UnsupportedCredentialVersionError(
                f"{source} logout marker uses a newer version"
            )
        logout = payload.get("logout")
        if (
            version != LOGOUT_MARKER_VERSION
            or type(logout) is not bool
            or not logout
        ):
            raise CredentialStorageError(f"{source} logout marker is malformed")

    def _read_logout_marker(self) -> bool:
        encoded = self._read_bounded_text(
            self._logout_marker_path,
            maximum=_MAX_STATE_BYTES,
            label="OAuth logout marker",
            symlink_is_missing=False,
        )
        if encoded is None:
            return False
        self._validate_logout_marker(encoded, source="OAuth file")
        return True

    def _write_logout_marker(self) -> None:
        self._atomic_text(
            self._logout_marker_path,
            self._encode_logout_marker(),
        )

    def _delete_logout_marker(self) -> None:
        remove_file(self._logout_marker_path)

    def _read_vault_logout_marker(self) -> bool:
        encoded = self._vault_get(VAULT_LOGOUT_ACCOUNT)
        if encoded is None:
            return False
        self._validate_logout_marker(encoded, source="OAuth vault")
        return True

    def _write_vault_logout_marker(self) -> None:
        encoded = self._encode_logout_marker()
        existing = self._vault_get(VAULT_LOGOUT_ACCOUNT)
        if existing is not None:
            self._validate_logout_marker(existing, source="OAuth vault")
            return
        self._vault_set(encoded, VAULT_LOGOUT_ACCOUNT)
        if self._vault_get(VAULT_LOGOUT_ACCOUNT) != encoded:
            raise CredentialVaultError(
                "OS credential vault logout marker verification failed"
            )

    def _delete_vault_logout_marker(self) -> None:
        existing = self._vault_get(VAULT_LOGOUT_ACCOUNT)
        if existing is None:
            return
        self._validate_logout_marker(existing, source="OAuth vault")
        self._vault_delete(VAULT_LOGOUT_ACCOUNT)
        if self._vault_get(VAULT_LOGOUT_ACCOUNT) is not None:
            raise CredentialVaultError(
                "OS credential vault logout marker deletion failed"
            )

    def _read_state(self) -> _StorageState:
        encoded = self._read_bounded_text(
            self._state_path,
            maximum=_MAX_STATE_BYTES,
            label="OAuth storage state",
            symlink_is_missing=False,
        )
        if encoded is None:
            return _StorageState()
        try:
            payload: Any = json.loads(encoded)
        except (TypeError, ValueError):
            raise CredentialStorageError(
                "OAuth storage state is malformed"
            ) from None
        if not isinstance(payload, dict):
            raise CredentialStorageError("OAuth storage state is malformed")
        version = payload.get("version")
        if type(version) is not int:
            raise CredentialStorageError(
                "OAuth storage state has an invalid version"
            )
        if version > STATE_VERSION:
            raise UnsupportedCredentialVersionError(
                "OAuth storage state uses a newer version"
            )
        provisioned = payload.get("vault_provisioned")
        cleanup = payload.get("cleanup")
        fallback_eligible = (
            False if version == 1 else payload.get("fallback_eligible")
        )
        if (
            version not in (1, STATE_VERSION)
            or type(provisioned) is not bool
            or type(fallback_eligible) is not bool
            or cleanup
            not in (_CLEANUP_NONE, _CLEANUP_PENDING, _CLEANUP_LOGGED_OUT)
        ):
            raise CredentialStorageError("OAuth storage state is malformed")
        return _StorageState(
            vault_provisioned=provisioned,
            fallback_eligible=fallback_eligible,
            cleanup=cleanup,
        )

    def _write_state(self, state: _StorageState) -> None:
        encoded = json.dumps(
            {
                "cleanup": state.cleanup,
                "fallback_eligible": state.fallback_eligible,
                "vault_provisioned": state.vault_provisioned,
                "version": state.version,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self._atomic_text(self._state_path, encoded)

    @staticmethod
    def _atomic_text(path: Path, encoded: str) -> None:
        def writer(file: io.TextIOWrapper) -> None:
            file.write(encoded)
            file.write("\n")

        atomic_write(path, writer)

    @staticmethod
    def _read_bounded_text(
        path: Path,
        *,
        maximum: int,
        label: str,
        symlink_is_missing: bool,
    ) -> str | None:
        if path.is_symlink():
            if symlink_is_missing:
                return None
            raise CredentialStorageError(f"{label} path is a symlink")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                return None
            raise CredentialStorageError(
                f"{label} could not be opened safely"
            ) from None
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise CredentialStorageError(f"{label} is not a regular file")
            if details.st_size > maximum:
                raise CredentialStorageError(
                    f"{label} exceeds the supported size"
                )
            with suppress(OSError):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf8") as file:
                descriptor = -1
                encoded = file.read(maximum + 1)
        except (OSError, UnicodeError):
            raise CredentialStorageError(
                f"{label} could not be read safely"
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(encoded.encode("utf8")) > maximum:
            raise CredentialStorageError(f"{label} exceeds the supported size")
        return encoded

    def _vault_get(self, account: str = VAULT_ACCOUNT) -> str | None:
        if self._vault is None:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            )
        try:
            value = self._vault.get_password(VAULT_SERVICE, account)
        except Exception as exc:
            if isinstance(exc, NoKeyringError):
                raise CredentialVaultUnavailableError(
                    "No native credential vault is available"
                ) from None
            raise CredentialVaultError("OS credential vault read failed") from None
        if value is not None and not isinstance(value, str):
            raise CredentialVaultError(
                "OS credential vault returned an invalid value"
            )
        return value

    def _vault_set(
        self,
        encoded: str,
        account: str = VAULT_ACCOUNT,
    ) -> None:
        if self._vault is None:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            )
        try:
            self._vault.set_password(VAULT_SERVICE, account, encoded)
        except Exception as exc:
            if isinstance(exc, NoKeyringError):
                raise CredentialVaultUnavailableError(
                    "No native credential vault is available"
                ) from None
            raise CredentialVaultError("OS credential vault write failed") from None

    def _vault_delete(self, account: str = VAULT_ACCOUNT) -> None:
        if self._vault is None:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            )
        try:
            self._vault.delete_password(VAULT_SERVICE, account)
        except Exception as exc:
            if isinstance(exc, NoKeyringError):
                raise CredentialVaultUnavailableError(
                    "No native credential vault is available"
                ) from None
            raise CredentialVaultError(
                "OS credential vault deletion failed"
            ) from None

    def _write_vault(
        self,
        encoded: str,
        *,
        only_if_missing: bool = False,
    ) -> str | None:
        previous = self._vault_get()
        if previous is not None:
            self._parse_record(previous, source="vault")
            if only_if_missing:
                return previous
        try:
            self._vault_set(encoded)
            current = self._vault_get()
            if current != encoded:
                self._rollback_vault(previous, encoded)
                raise CredentialConflictError(
                    "Vault changed during write verification"
                )
        except CredentialStorageError as exc:
            if isinstance(
                exc,
                (CredentialVaultUnavailableError, CredentialConflictError),
            ):
                raise
            self._rollback_vault(previous, encoded)
            raise
        return None

    def _rollback_vault(
        self,
        previous: str | None,
        written: str,
    ) -> None:
        try:
            current = self._vault_get()
            if current == previous:
                return
            if current != written:
                raise CredentialConflictError(
                    "Concurrent vault value preserved during rollback"
                )
            if previous is None:
                self._vault_delete()
            else:
                self._vault_set(previous)
            if self._vault_get() != previous:
                raise CredentialVaultError(
                    "OS credential vault rollback verification failed"
                )
        except CredentialStorageError as exc:
            if isinstance(exc, CredentialConflictError):
                raise
            raise CredentialVaultError(
                "OS credential vault rollback failed; state is uncertain"
            ) from None

    def _rollback_file(self, previous: str | None, written: str) -> None:
        current = self._read_file_text()
        if current == previous:
            return
        if current != written:
            raise CredentialConflictError(
                "Concurrent fallback value preserved during rollback"
            )
        if previous is None:
            self._delete_file()
        else:
            self._save_file(previous)
        if self._read_file_text() != previous:
            raise CredentialStorageError(
                "Fallback rollback verification failed"
            )
