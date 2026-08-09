from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from constants import DATA_DIR, WORKING_DIR

STORAGE_VERSION: Final = 1
_MAX_METADATA_BYTES: Final = 64 * 1024


class DataMigrationError(RuntimeError):
    """Raised when mutable storage cannot be migrated safely."""


@dataclass(frozen=True)
class MigrationResult:
    copied: tuple[str, ...]
    quarantined: tuple[str, ...]
    skipped: tuple[str, ...]


# Durable files are copied before caches. Diagnostic logs and dumps are deliberately
# left in place: they are not application state and can be unexpectedly large.
_LEGACY_FILES: Final = {
    "settings.json": 4 * 1024 * 1024,
    "cookies.jar": 8 * 1024 * 1024,
    "oauth.json": 1024 * 1024,
    "session_history.json": 64 * 1024 * 1024,
}
_LEGACY_CACHE_FILES: Final = {
    "mapping.json": 8 * 1024 * 1024,
    "steam-metadata.json": 8 * 1024 * 1024,
}


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise DataMigrationError(f"Unable to inspect data directory: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise DataMigrationError(f"Data directory is not a real directory: {path}")
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise DataMigrationError(f"Unable to protect data directory: {path}") from exc


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise DataMigrationError(f"Refusing legacy symlink: {path}")
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DataMigrationError(f"Unable to read legacy file: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DataMigrationError(f"Legacy path is not a regular file: {path}")
        if info.st_size > maximum_bytes:
            raise DataMigrationError(f"Legacy file exceeds migration limit: {path}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum_bytes:
            raise DataMigrationError(f"Legacy file exceeds migration limit: {path}")
        return data
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    _ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.migration.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    except OSError as exc:
        raise DataMigrationError(f"Unable to install migrated file: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _metadata_version(metadata_path: Path) -> int | None:
    if not metadata_path.exists():
        return None
    data = _read_regular_file(metadata_path, _MAX_METADATA_BYTES)
    try:
        value = json.loads(data.decode("utf8"))
    except ValueError as exc:
        raise DataMigrationError("Mutable-storage metadata is corrupt") from exc
    if not isinstance(value, dict):
        raise DataMigrationError("Mutable-storage metadata must be an object")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DataMigrationError("Mutable-storage metadata has an invalid version")
    return version


def _copy_json_file(
    source: Path,
    destination: Path,
    maximum_bytes: int,
    *,
    label: str,
    copied: list[str],
    quarantined: list[str],
    skipped: list[str],
    quarantine_path: Path,
) -> None:
    if destination.exists() or destination.is_symlink():
        skipped.append(f"{label}:destination-exists")
        return
    if not source.exists() and not source.is_symlink():
        return
    try:
        data = _read_regular_file(source, maximum_bytes)
    except DataMigrationError as exc:
        skipped.append(f"{label}:{exc}")
        return
    valid_json = True
    try:
        json.loads(data.decode("utf8"))
    except ValueError:
        valid_json = False
    if not valid_json:
        quarantine = quarantine_path / f"{destination.name}.legacy-corrupt"
        if not quarantine.exists() and not quarantine.is_symlink():
            _atomic_write_bytes(quarantine, data)
        quarantined.append(label)
        return
    _atomic_write_bytes(destination, data)
    copied.append(label)


def migrate_legacy_data(
    *,
    legacy_dir: Path = WORKING_DIR,
    data_dir: Path = DATA_DIR,
) -> MigrationResult:
    """Install legacy mutable data once without overwriting newer destinations.

    The source is never deleted. Each destination is installed atomically, so an
    interrupted migration can be rerun safely. The completion marker is written
    last and unknown forward versions are rejected before the application writes.
    """
    metadata_path = data_dir / "storage.json"
    quarantine_path = data_dir / "migration-quarantine"
    _ensure_private_directory(data_dir)
    version = _metadata_version(metadata_path)
    if version is not None:
        if version > STORAGE_VERSION:
            raise DataMigrationError(
                f"Mutable data version {version} is newer than supported version "
                f"{STORAGE_VERSION}"
            )
        if version == STORAGE_VERSION:
            return MigrationResult((), (), ())

    copied: list[str] = []
    quarantined: list[str] = []
    skipped: list[str] = []
    try:
        same_location = legacy_dir.resolve() == data_dir.resolve()
    except OSError:
        same_location = False

    if not same_location:
        for name, maximum_bytes in _LEGACY_FILES.items():
            _copy_json_file(
                legacy_dir / name,
                data_dir / name,
                maximum_bytes,
                label=name,
                copied=copied,
                quarantined=quarantined,
                skipped=skipped,
                quarantine_path=quarantine_path,
            )
        for name, maximum_bytes in _LEGACY_CACHE_FILES.items():
            _copy_json_file(
                legacy_dir / "cache" / name,
                data_dir / "cache" / name,
                maximum_bytes,
                label=f"cache/{name}",
                copied=copied,
                quarantined=quarantined,
                skipped=skipped,
                quarantine_path=quarantine_path,
            )

    metadata = {
        "version": STORAGE_VERSION,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "legacy_source": str(legacy_dir),
        "copied": copied,
        "quarantined": quarantined,
        "skipped": skipped,
    }
    payload = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf8") + b"\n"
    _atomic_write_bytes(metadata_path, payload)
    return MigrationResult(tuple(copied), tuple(quarantined), tuple(skipped))
