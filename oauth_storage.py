"""Versioned OAuth refresh-token storage backed by the native OS vault.

The maintained ``keyring`` package supplies the platform adapters used here:
Windows Credential Manager, macOS Keychain, and Freedesktop Secret Service on
Linux.  A mode-0600 ``oauth.json`` record is retained only as a durable
fallback when the native vault is genuinely unavailable.
"""
from __future__ import annotations

import errno
import io
import json
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, Final, Protocol, cast

from keyring.backend import KeyringBackend
from keyring.backends.SecretService import Keyring as SecretServiceKeyring
from keyring.backends.Windows import WinVaultKeyring
from keyring.backends.macOS import Keyring as MacOSKeyring
from keyring.errors import NoKeyringError

from utils import atomic_write, remove_file


CREDENTIAL_VERSION: Final = 1
VAULT_SERVICE: Final = "io.github.devilxd.twitchdropsminer.oauth"
VAULT_ACCOUNT: Final = "twitch-session"
_MAX_LEGACY_BYTES: Final = 1024 * 1024


class CredentialStorageError(RuntimeError):
    """A credential could not be handled without risking its integrity."""


class CredentialVaultError(CredentialStorageError):
    """The selected native credential vault failed an operation."""


class CredentialVaultUnavailableError(CredentialStorageError):
    """No native credential vault is available for the current session."""


class CredentialMigrationError(CredentialStorageError):
    """A legacy credential could not be migrated transactionally."""


class UnsupportedCredentialVersionError(CredentialStorageError):
    """A credential was written by a newer, unsupported application version."""


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


_SYSTEM_VAULT_TYPES: Final[dict[str, type[KeyringBackend]]] = {
    "linux": SecretServiceKeyring,
    "darwin": MacOSKeyring,
    "win32": WinVaultKeyring,
}


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
        # Backend priority is keyring's documented availability probe.  These
        # failures mean that the platform API, daemon, or required adapter is
        # absent; ordinary read/write failures are deliberately not downgraded.
        return None


class OAuthTokenStore:
    """Persist one versioned Twitch OAuth session credential.

    ``vault`` is injectable for tests.  Passing ``use_system_vault=False`` is
    the explicit way to exercise the secure-file fallback without probing the
    desktop session.
    """

    def __init__(
        self,
        path: Path,
        *,
        vault: CredentialVault | None = None,
        use_system_vault: bool = True,
    ) -> None:
        self._path = path
        self._vault = (
            _create_system_vault()
            if vault is None and use_system_vault
            else vault
        )

    def load(self, client_id: str) -> str | None:
        """Load a token, migrating a matching legacy record when possible.

        A valid vault record always wins over ``oauth.json``.  Unknown forward
        versions and vault failures are surfaced instead of being mistaken for
        missing credentials.
        """
        self._require_client_id(client_id)
        if self._vault is None:
            return self._load_legacy(client_id)

        try:
            destination = self._vault_get()
        except CredentialVaultUnavailableError:
            return self._load_legacy(client_id)

        if destination is not None:
            token = self._decode_record(
                destination,
                client_id,
                allow_unversioned=False,
                source="vault",
            )
            if token is not None:
                self._remove_matching_legacy(client_id)
            return token

        legacy_token = self._load_legacy(client_id)
        if legacy_token is None:
            return None
        encoded = self._encode_record(client_id, legacy_token)
        try:
            raced_destination = self._write_vault(
                encoded,
                only_if_missing=True,
            )
        except CredentialVaultUnavailableError:
            return legacy_token

        if raced_destination is not None:
            token = self._decode_record(
                raced_destination,
                client_id,
                allow_unversioned=False,
                source="vault",
            )
            if token is not None:
                self._remove_matching_legacy(client_id)
            return token

        try:
            self._delete_legacy()
        except OSError:
            self._rollback_vault(None)
            raise CredentialMigrationError(
                "OAuth credential migration could not remove its source"
            ) from None
        return legacy_token

    def save(self, client_id: str, refresh_token: str) -> None:
        """Save a versioned credential to the vault or unavailable fallback."""
        self._require_client_id(client_id)
        if not refresh_token:
            raise ValueError("OAuth token records require non-empty values")

        encoded = self._encode_record(client_id, refresh_token)
        if self._vault is None:
            self._save_legacy(encoded)
            return
        try:
            self._write_vault(encoded)
        except CredentialVaultUnavailableError:
            self._save_legacy(encoded)
            return

        try:
            self._delete_legacy()
        except OSError:
            # The verified destination contains the newest (possibly rotated)
            # token.  Rolling it back could restore an invalid old token, so
            # retain both copies and report the incomplete cleanup.
            raise CredentialMigrationError(
                "OAuth credential was secured but legacy cleanup failed"
            ) from None

    def clear(self) -> None:
        """Delete both vault and fallback credentials during local logout."""
        vault_error = False
        if self._vault is not None:
            try:
                current = self._vault_get()
                if current is not None:
                    self._vault_delete()
                    if self._vault_get() is not None:
                        raise CredentialVaultError(
                            "OS credential vault deletion was not durable"
                        )
            except CredentialStorageError:
                vault_error = True

        legacy_error = False
        try:
            self._delete_legacy()
        except OSError:
            legacy_error = True

        if vault_error or legacy_error:
            raise CredentialStorageError(
                "Local OAuth credential cleanup was incomplete"
            ) from None

    @staticmethod
    def _require_client_id(client_id: str) -> None:
        if not client_id:
            raise ValueError("OAuth token records require a non-empty client ID")

    @staticmethod
    def _encode_record(client_id: str, refresh_token: str) -> str:
        return json.dumps(
            {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "version": CREDENTIAL_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_record(
        encoded: str,
        client_id: str,
        *,
        allow_unversioned: bool,
        source: str,
    ) -> str | None:
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

        missing_version = 0 if allow_unversioned else None
        version = payload.get("version", missing_version)
        if type(version) is not int:
            raise CredentialStorageError(
                f"Stored OAuth {source} record has an invalid version"
            )
        if version > CREDENTIAL_VERSION:
            raise UnsupportedCredentialVersionError(
                f"Stored OAuth {source} record uses a newer version"
            )
        supported_versions = (0, CREDENTIAL_VERSION) if allow_unversioned else (
            CREDENTIAL_VERSION,
        )
        if version not in supported_versions:
            raise CredentialStorageError(
                f"Stored OAuth {source} record uses an unsupported version"
            )

        stored_client_id = payload.get("client_id")
        if not isinstance(stored_client_id, str) or not stored_client_id:
            raise CredentialStorageError(
                f"Stored OAuth {source} record is malformed"
            )
        if stored_client_id != client_id:
            return None
        token = payload.get("refresh_token")
        if not isinstance(token, str) or not token:
            raise CredentialStorageError(
                f"Stored OAuth {source} record is malformed"
            )
        return token

    def _load_legacy(self, client_id: str) -> str | None:
        encoded = self._read_legacy_text()
        if encoded is None:
            return None
        return self._decode_record(
            encoded,
            client_id,
            allow_unversioned=True,
            source="file",
        )

    def _read_legacy_text(self) -> str | None:
        if self._path.is_symlink():
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return None
            raise CredentialStorageError(
                "Stored OAuth file could not be opened safely"
            ) from None

        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise CredentialStorageError(
                    "Stored OAuth file is not a regular file"
                )
            if details.st_size > _MAX_LEGACY_BYTES:
                raise CredentialStorageError(
                    "Stored OAuth file exceeds the supported size"
                )
            with suppress(OSError):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf8") as file:
                descriptor = -1
                encoded = file.read(_MAX_LEGACY_BYTES + 1)
        except (OSError, UnicodeError):
            raise CredentialStorageError(
                "Stored OAuth file could not be read safely"
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if len(encoded.encode("utf8")) > _MAX_LEGACY_BYTES:
            raise CredentialStorageError(
                "Stored OAuth file exceeds the supported size"
            )
        return encoded

    def _save_legacy(self, encoded: str) -> None:
        def writer(file: io.TextIOWrapper) -> None:
            file.write(encoded)
            file.write("\n")

        atomic_write(self._path, writer)

    def _delete_legacy(self) -> None:
        remove_file(self._path)

    def _remove_matching_legacy(self, client_id: str) -> None:
        try:
            legacy_token = self._load_legacy(client_id)
        except CredentialStorageError:
            # Destination-wins means an unreadable or forward-version source
            # must never replace or block a valid vault destination.  Leave it
            # untouched for explicit recovery by the user or a newer version.
            return
        if legacy_token is None:
            return
        try:
            self._delete_legacy()
        except OSError:
            raise CredentialMigrationError(
                "Legacy OAuth credential cleanup failed"
            ) from None

    def _vault_get(self) -> str | None:
        if self._vault is None:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            )
        try:
            value = self._vault.get_password(VAULT_SERVICE, VAULT_ACCOUNT)
        except NoKeyringError:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            ) from None
        except Exception:
            raise CredentialVaultError(
                "OS credential vault read failed"
            ) from None
        if value is not None and not isinstance(value, str):
            raise CredentialVaultError(
                "OS credential vault returned an invalid value"
            )
        return value

    def _vault_set(self, encoded: str) -> None:
        if self._vault is None:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            )
        try:
            self._vault.set_password(VAULT_SERVICE, VAULT_ACCOUNT, encoded)
        except NoKeyringError:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            ) from None
        except Exception:
            raise CredentialVaultError(
                "OS credential vault write failed"
            ) from None

    def _vault_delete(self) -> None:
        if self._vault is None:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            )
        try:
            self._vault.delete_password(VAULT_SERVICE, VAULT_ACCOUNT)
        except NoKeyringError:
            raise CredentialVaultUnavailableError(
                "No native credential vault is available"
            ) from None
        except Exception:
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
        if only_if_missing and previous is not None:
            return previous
        try:
            self._vault_set(encoded)
            if self._vault_get() != encoded:
                raise CredentialVaultError(
                    "OS credential vault write verification failed"
                )
        except CredentialVaultUnavailableError:
            raise
        except CredentialStorageError as exc:
            self._rollback_vault(previous)
            raise exc
        return None

    def _rollback_vault(self, previous: str | None) -> None:
        try:
            current = self._vault_get()
            if previous is None:
                if current is not None:
                    self._vault_delete()
            elif current != previous:
                self._vault_set(previous)
            if self._vault_get() != previous:
                raise CredentialVaultError(
                    "OS credential vault rollback verification failed"
                )
        except CredentialStorageError:
            raise CredentialVaultError(
                "OS credential vault rollback failed; state is uncertain"
            ) from None
