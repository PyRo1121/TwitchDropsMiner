from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pickle import UnpicklingError
from typing import Any, Final, Literal

import aiohttp  # pyright: ignore[reportMissingImports]

from constants import DATA_DIR, WORKING_DIR
from utils import atomic_write_bytes, durable_unlink

STORAGE_VERSION: Final = 2
JOURNAL_VERSION: Final = 1
_MAX_METADATA_BYTES: Final = 64 * 1024
_MAX_JOURNAL_BYTES: Final = 1024 * 1024


class DataMigrationError(RuntimeError):
    """Raised when mutable storage cannot be migrated safely."""


class _InvalidArtifact(ValueError):
    """Raised for permanently invalid legacy artifact bytes."""


class _ForwardArtifact(DataMigrationError):
    """Raised when an artifact was written by a newer unsupported version."""


@dataclass(frozen=True)
class MigrationResult:
    migrated: tuple[str, ...]
    recovered: tuple[str, ...]
    destination_wins: tuple[str, ...]
    quarantined: tuple[str, ...]
    cleaned: tuple[str, ...]


@dataclass(frozen=True)
class _ReadResult:
    payload: bytes
    source_version: int
    source_format: str


_Reader = Callable[[bytes, Path], _ReadResult]


@dataclass(frozen=True)
class _ArtifactSpec:
    key: str
    source_relative: Path
    destination_relative: Path
    maximum_bytes: int
    reader_version: int
    reader: _Reader
    credential: bool = False


_SourceRole = Literal["canonical", "new", "v1-quarantine"]


@dataclass(frozen=True)
class _SourceSnapshot:
    role: _SourceRole
    path: Path
    data: bytes
    digest: str
    parsed: _ReadResult | None


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise DataMigrationError(f"Unable to inspect private directory: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise DataMigrationError(f"Private path is not a real directory: {path}")
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise DataMigrationError(f"Unable to protect private directory: {path}") from exc


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise DataMigrationError(f"Refusing symlinked migration input: {path}")
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DataMigrationError(f"Unable to read migration file: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DataMigrationError(f"Migration path is not a regular file: {path}")
        if info.st_size > maximum_bytes:
            raise DataMigrationError(f"Migration file exceeds its size limit: {path}")
        descriptor_chmod = getattr(os, "fchmod", None)
        if descriptor_chmod is not None:
            try:
                descriptor_chmod(descriptor, 0o600)
            except OSError as exc:
                raise DataMigrationError(
                    f"Unable to protect migration file: {path}"
                ) from exc
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
            raise DataMigrationError(f"Migration file exceeds its size limit: {path}")
        return data
    finally:
        os.close(descriptor)


def _path_exists(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DataMigrationError(f"Unable to inspect migration path: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise DataMigrationError(f"Migration path is not a regular file: {path}")
    return True


def _validate_directory_chain(root: Path, directory: Path) -> bool:
    root_absolute = Path(os.path.abspath(os.fspath(root)))
    directory_absolute = Path(os.path.abspath(os.fspath(directory)))
    try:
        relative = directory_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise DataMigrationError(f"Migration path escaped its root: {directory}") from exc
    current = root_absolute
    for part in (".", *relative.parts):
        if part != ".":
            current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise DataMigrationError(f"Unable to inspect migration directory: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DataMigrationError(f"Migration directory is not real: {current}")
    return True


def _safe_data_path(data_dir: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise DataMigrationError("Migration journal output escaped data directory")
    path = data_dir / relative_path
    if not _validate_directory_chain(data_dir, path.parent):
        raise DataMigrationError("Migration journal output directory is missing")
    return path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant is unsupported: {value}")


def _decode_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf8"),
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise _InvalidArtifact(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise _InvalidArtifact(f"{label} root must be an object")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise _InvalidArtifact("Artifact contains unsupported JSON values") from exc
    return text.encode("utf8") + b"\n"


def _document_version(
    value: Mapping[str, Any],
    *,
    label: str,
    maximum: int,
    required: bool = False,
) -> int:
    raw_version = value.get("version")
    if raw_version is None and not required:
        return 1
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
        raise _InvalidArtifact(f"{label} has an invalid version")
    if raw_version > maximum:
        raise _ForwardArtifact(
            f"{label} version {raw_version} is newer than supported version {maximum}"
        )
    return raw_version


def _read_settings_v1(data: bytes, _work_dir: Path) -> _ReadResult:
    value = _decode_json_object(data, "Legacy settings")
    version = _document_version(value, label="Legacy settings", maximum=1)
    return _ReadResult(_canonical_json(value), version, "settings-json-v1")


def _read_oauth_v1(data: bytes, _work_dir: Path) -> _ReadResult:
    value = _decode_json_object(data, "Legacy OAuth token store")
    version = _document_version(value, label="Legacy OAuth token store", maximum=1)
    client_id = value.get("client_id")
    refresh_token = value.get("refresh_token")
    if not isinstance(client_id, str) or not client_id:
        raise _InvalidArtifact("Legacy OAuth token store has no client ID")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise _InvalidArtifact("Legacy OAuth token store has no refresh token")
    return _ReadResult(_canonical_json(value), version, "oauth-json-v1")


def _read_history_v1(data: bytes, _work_dir: Path) -> _ReadResult:
    value = _decode_json_object(data, "Legacy session history")
    version = _document_version(
        value,
        label="Legacy session history",
        maximum=1,
        required=True,
    )
    if not isinstance(value.get("sessions"), list):
        raise _InvalidArtifact("Legacy session history has no session list")
    return _ReadResult(_canonical_json(value), version, "history-json-v1")


def _read_cache_v1(data: bytes, _work_dir: Path) -> _ReadResult:
    value = _decode_json_object(data, "Legacy cache")
    version = _document_version(value, label="Legacy cache", maximum=1)
    return _ReadResult(_canonical_json(value), version, "cache-json-v1")


def _private_temp_file(work_dir: Path, prefix: str, data: bytes) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=work_dir)
    path = Path(raw_path)
    try:
        descriptor_chmod = getattr(os, "fchmod", None)
        if descriptor_chmod is not None:
            descriptor_chmod(descriptor, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            durable_unlink(path)
        except OSError as cleanup_exc:
            raise DataMigrationError(
                f"Unable to clean failed migration temporary file: {path}"
            ) from cleanup_exc
        raise
    os.close(descriptor)
    return path


def _read_cookies_v2(data: bytes, work_dir: Path) -> _ReadResult:
    """Convert current JSON or legacy pickle through aiohttp's restricted reader.

    The pinned aiohttp CookieJar first validates its JSON format and otherwise
    uses its restricted legacy-cookie unpickler. No general pickle loader is
    imported or invoked here. Input is bounded before reaching aiohttp.
    """
    source_format = "cookie-json-v2"
    try:
        _decode_json_object(data, "Legacy cookie jar")
    except _InvalidArtifact:
        source_format = "cookie-pickle-v1"

    source_path = _private_temp_file(work_dir, "cookie-input-", data)
    output_path = _private_temp_file(work_dir, "cookie-output-", b"")
    loop = asyncio.new_event_loop()
    try:
        jar = aiohttp.CookieJar(loop=loop)
        try:
            jar.load(source_path)
        except (
            AttributeError,
            EOFError,
            RecursionError,
            TypeError,
            UnpicklingError,
            ValueError,
        ) as exc:
            # CookieJar.load owns both JSON validation and its restricted
            # legacy unpickler; parser failures are quarantinable. Importing
            # UnpicklingError does not invoke Python's general pickle loader.
            raise _InvalidArtifact("Legacy cookie jar is invalid") from exc
        except OSError as exc:
            raise DataMigrationError("Unable to read cookie conversion input") from exc
        try:
            jar.save(output_path)
            output = _read_regular_file(output_path, 8 * 1024 * 1024)
            value = _decode_json_object(output, "Converted cookie jar")
        except _InvalidArtifact:
            raise
        except (AttributeError, RecursionError, TypeError, ValueError) as exc:
            raise _InvalidArtifact("Legacy cookie jar is invalid") from exc
        except OSError as exc:
            raise DataMigrationError("Unable to persist converted cookie jar") from exc
        return _ReadResult(
            _canonical_json(value),
            1 if source_format == "cookie-pickle-v1" else 2,
            source_format,
        )
    finally:
        loop.close()
        for path in (source_path, output_path):
            durable_unlink(path)


_ARTIFACTS: Final = (
    _ArtifactSpec(
        "settings.json",
        Path("settings.json"),
        Path("settings.json"),
        4 * 1024 * 1024,
        1,
        _read_settings_v1,
    ),
    _ArtifactSpec(
        "cookies.jar",
        Path("cookies.jar"),
        Path("cookies.jar"),
        8 * 1024 * 1024,
        2,
        _read_cookies_v2,
        credential=True,
    ),
    _ArtifactSpec(
        "oauth.json",
        Path("oauth.json"),
        Path("oauth.json"),
        1024 * 1024,
        1,
        _read_oauth_v1,
        credential=True,
    ),
    _ArtifactSpec(
        "session_history.json",
        Path("session_history.json"),
        Path("session_history.json"),
        64 * 1024 * 1024,
        1,
        _read_history_v1,
    ),
    _ArtifactSpec(
        "cache/mapping.json",
        Path("cache/mapping.json"),
        Path("cache/mapping.json"),
        8 * 1024 * 1024,
        1,
        _read_cache_v1,
    ),
    _ArtifactSpec(
        "cache/steam-metadata.json",
        Path("cache/steam-metadata.json"),
        Path("cache/steam-metadata.json"),
        8 * 1024 * 1024,
        1,
        _read_cache_v1,
    ),
)


def _clean_work_directory(work_dir: Path) -> None:
    _ensure_private_directory(work_dir)
    try:
        entries = tuple(work_dir.iterdir())
    except OSError as exc:
        raise DataMigrationError("Unable to inspect migration work directory") from exc
    for path in entries:
        if not _path_exists(path):
            continue
        try:
            durable_unlink(path)
        except OSError as exc:
            raise DataMigrationError(f"Unable to remove stale migration work: {path}") from exc


def _metadata_version(metadata_path: Path) -> int | None:
    if not _path_exists(metadata_path):
        return None
    data = _read_regular_file(metadata_path, _MAX_METADATA_BYTES)
    try:
        value = _decode_json_object(data, "Mutable-storage metadata")
    except _InvalidArtifact as exc:
        raise DataMigrationError(str(exc)) from exc
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DataMigrationError("Mutable-storage metadata has an invalid version")
    return version


def _new_journal(legacy_dir: Path) -> dict[str, Any]:
    return {
        "version": JOURNAL_VERSION,
        "target_storage_version": STORAGE_VERSION,
        "legacy_source": os.path.abspath(os.fspath(legacy_dir)),
        "artifacts": {
            spec.key: {
                "state": "pending",
                "reader_version": spec.reader_version,
                "credential": spec.credential,
            }
            for spec in _ARTIFACTS
        },
    }


def _load_journal(path: Path, legacy_dir: Path) -> dict[str, Any]:
    if not _path_exists(path):
        return _new_journal(legacy_dir)
    data = _read_regular_file(path, _MAX_JOURNAL_BYTES)
    try:
        value = _decode_json_object(data, "Migration journal")
    except _InvalidArtifact as exc:
        raise DataMigrationError(str(exc)) from exc
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DataMigrationError("Migration journal has an invalid version")
    if version > JOURNAL_VERSION:
        raise DataMigrationError(
            f"Migration journal version {version} is newer than supported version "
            f"{JOURNAL_VERSION}"
        )
    if value.get("target_storage_version") != STORAGE_VERSION:
        raise DataMigrationError("Migration journal targets a different storage version")
    expected_source = os.path.abspath(os.fspath(legacy_dir))
    if value.get("legacy_source") != expected_source:
        raise DataMigrationError("Migration journal belongs to a different legacy source")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DataMigrationError("Migration journal has no artifact states")
    for spec in _ARTIFACTS:
        record = artifacts.get(spec.key)
        if not isinstance(record, dict):
            raise DataMigrationError(f"Migration journal is missing {spec.key}")
        if record.get("reader_version") != spec.reader_version:
            raise DataMigrationError(f"Migration journal reader changed for {spec.key}")
        if record.get("credential") is not spec.credential:
            raise DataMigrationError(f"Migration journal trust class changed for {spec.key}")
        if record.get("state") not in {"pending", "installed", "complete"}:
            raise DataMigrationError(f"Migration journal state is invalid for {spec.key}")
    return value


def _write_json_file(path: Path, value: Mapping[str, Any]) -> None:
    try:
        atomic_write_bytes(path, _canonical_json(value))
    except (OSError, TypeError, ValueError) as exc:
        raise DataMigrationError(f"Unable to persist migration state: {path}") from exc


def _relative_output(data_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(data_dir))
    except ValueError as exc:
        raise DataMigrationError("Migration output escaped the data directory") from exc


def _quarantine_path(
    data_dir: Path,
    spec: _ArtifactSpec,
    suffix: str,
) -> Path:
    safe_name = spec.key.replace("/", "-")
    return data_dir / "migration-quarantine" / f"{safe_name}.{suffix}"


def _write_preserved_bytes(path: Path, data: bytes) -> None:
    _ensure_private_directory(path.parent)
    if _path_exists(path):
        existing = _read_regular_file(path, len(data))
        if existing != data:
            raise DataMigrationError(f"Migration quarantine conflict: {path}")
        return
    try:
        atomic_write_bytes(path, data)
    except OSError as exc:
        raise DataMigrationError(f"Unable to preserve migration input: {path}") from exc
    installed = _read_regular_file(path, len(data))
    if installed != data:
        raise DataMigrationError(f"Migration quarantine verification failed: {path}")


def _read_source_snapshots(
    spec: _ArtifactSpec,
    legacy_dir: Path,
    data_dir: Path,
    work_dir: Path,
    *,
    include_legacy_paths: bool,
) -> list[_SourceSnapshot]:
    canonical = legacy_dir / spec.source_relative
    old_quarantine = (
        data_dir
        / "migration-quarantine"
        / f"{spec.destination_relative.name}.legacy-corrupt"
    )
    legacy_candidates: tuple[tuple[_SourceRole, Path, Path], ...] = (
        ("canonical", canonical, legacy_dir),
        ("new", canonical.with_name(f"{canonical.name}.new"), legacy_dir),
    )
    candidates: tuple[tuple[_SourceRole, Path, Path], ...]
    if include_legacy_paths:
        candidates = (
            *legacy_candidates,
            ("v1-quarantine", old_quarantine, data_dir),
        )
    else:
        candidates = (("v1-quarantine", old_quarantine, data_dir),)
    snapshots: list[_SourceSnapshot] = []
    for role, path, root in candidates:
        if not _validate_directory_chain(root, path.parent):
            continue
        if not _path_exists(path):
            continue
        data = _read_regular_file(path, spec.maximum_bytes)
        try:
            parsed = spec.reader(data, work_dir)
        except _InvalidArtifact:
            parsed = None
        snapshots.append(_SourceSnapshot(role, path, data, _digest(data), parsed))
    return snapshots


def _validate_destination(
    spec: _ArtifactSpec,
    destination: Path,
    work_dir: Path,
) -> tuple[_ReadResult, bytes] | None:
    if not _path_exists(destination):
        return None
    data = _read_regular_file(destination, spec.maximum_bytes)
    try:
        parsed = spec.reader(data, work_dir)
    except _ForwardArtifact:
        raise
    except _InvalidArtifact as exc:
        raise DataMigrationError(
            f"Destination-wins file is invalid and was not overwritten: {destination}"
        ) from exc
    return parsed, data


def _output_record(data_dir: Path, path: Path, data: bytes) -> dict[str, str]:
    return {
        "path": _relative_output(data_dir, path),
        "sha256": _digest(data),
    }


def _verify_outputs(
    record: Mapping[str, Any],
    data_dir: Path,
    maximum_bytes: int,
) -> None:
    outputs = record.get("outputs", [])
    if not isinstance(outputs, list):
        raise DataMigrationError("Migration journal outputs are invalid")
    for output in outputs:
        if not isinstance(output, dict):
            raise DataMigrationError("Migration journal output is invalid")
        relative = output.get("path")
        digest = output.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise DataMigrationError("Migration journal output metadata is invalid")
        path = _safe_data_path(data_dir, relative)
        data = _read_regular_file(path, maximum_bytes)
        if _digest(data) != digest:
            raise DataMigrationError(f"Migration output changed before completion: {path}")


def _cleanup_sources(
    record: dict[str, Any],
    spec: _ArtifactSpec,
    legacy_dir: Path,
    data_dir: Path,
    journal: dict[str, Any],
    journal_path: Path,
) -> list[str]:
    cleanup = record.get("cleanup", [])
    if not isinstance(cleanup, list):
        raise DataMigrationError(f"Migration cleanup state is invalid for {spec.key}")
    cleaned: list[str] = []
    canonical = legacy_dir / spec.source_relative
    source_paths = {
        "canonical": canonical,
        "new": canonical.with_name(f"{canonical.name}.new"),
        "v1-quarantine": (
            data_dir
            / "migration-quarantine"
            / f"{spec.destination_relative.name}.legacy-corrupt"
        ),
    }
    for item in cleanup:
        if not isinstance(item, dict):
            raise DataMigrationError(f"Migration cleanup item is invalid for {spec.key}")
        role = item.get("role")
        digest = item.get("sha256")
        removed = item.get("removed", False)
        if role not in source_paths or not isinstance(digest, str) or type(removed) is not bool:
            raise DataMigrationError(f"Migration cleanup metadata is invalid for {spec.key}")
        path = source_paths[role]
        if removed:
            if _path_exists(path):
                raise DataMigrationError(f"Removed migration source reappeared: {path}")
            continue
        if _path_exists(path):
            current = _read_regular_file(path, spec.maximum_bytes)
            if _digest(current) != digest:
                raise DataMigrationError(f"Migration source changed before cleanup: {path}")
            try:
                durable_unlink(path)
            except OSError as exc:
                raise DataMigrationError(f"Unable to remove migrated source: {path}") from exc
            cleaned.append(f"{spec.key}:{role}")
        item["removed"] = True
        _write_json_file(journal_path, journal)
    return cleaned


def _prepare_artifact(
    spec: _ArtifactSpec,
    record: dict[str, Any],
    *,
    legacy_dir: Path,
    data_dir: Path,
    work_dir: Path,
    same_location: bool,
) -> None:
    destination = data_dir / spec.destination_relative
    _ensure_private_directory(destination.parent)
    destination_state = _validate_destination(spec, destination, work_dir)
    snapshots = _read_source_snapshots(
        spec,
        legacy_dir,
        data_dir,
        work_dir,
        include_legacy_paths=not same_location,
    )
    valid = [snapshot for snapshot in snapshots if snapshot.parsed is not None]
    invalid = [snapshot for snapshot in snapshots if snapshot.parsed is None]
    selected = next(
        (snapshot for snapshot in valid if snapshot.role == "new"),
        next(
            (snapshot for snapshot in valid if snapshot.role == "canonical"),
            valid[0] if valid else None,
        ),
    )

    outputs: list[dict[str, str]] = []
    quarantined = False
    for snapshot in invalid:
        quarantine = _quarantine_path(
            data_dir,
            spec,
            f"{snapshot.role}.invalid",
        )
        _write_preserved_bytes(quarantine, snapshot.data)
        outputs.append(_output_record(data_dir, quarantine, snapshot.data))
        quarantined = True

    result = "absent"
    source_format: str | None = None
    source_version: int | None = None
    if destination_state is not None:
        destination_result, destination_data = destination_state
        result = "destination-wins"
        outputs.append(_output_record(data_dir, destination, destination_data))
        if selected is not None and selected.parsed is not None:
            source_format = selected.parsed.source_format
            source_version = selected.parsed.source_version
            if selected.parsed.payload != destination_result.payload:
                conflict = _quarantine_path(
                    data_dir,
                    spec,
                    f"{selected.role}.destination-conflict",
                )
                _write_preserved_bytes(conflict, selected.data)
                outputs.append(_output_record(data_dir, conflict, selected.data))
                quarantined = True
    elif selected is not None and selected.parsed is not None:
        source_format = selected.parsed.source_format
        source_version = selected.parsed.source_version
        try:
            atomic_write_bytes(destination, selected.parsed.payload)
        except OSError as exc:
            raise DataMigrationError(f"Unable to install migrated file: {destination}") from exc
        installed_state = _validate_destination(spec, destination, work_dir)
        if installed_state is None:
            raise DataMigrationError(f"Migrated file verification failed: {destination}")
        installed, installed_data = installed_state
        if installed.payload != selected.parsed.payload:
            raise DataMigrationError(f"Migrated file verification failed: {destination}")
        outputs.append(_output_record(data_dir, destination, installed_data))
        result = (
            "recovered"
            if selected.role in {"new", "v1-quarantine"}
            else "migrated"
        )
    elif invalid:
        result = "quarantined"

    record.update(
        {
            "state": "installed",
            "result": result,
            "source_format": source_format,
            "source_version": source_version,
            "quarantined": quarantined,
            "outputs": outputs,
            "cleanup": [
                {
                    "role": snapshot.role,
                    "sha256": snapshot.digest,
                    "removed": False,
                }
                for snapshot in snapshots
            ],
        }
    )


def _migrate_artifact(
    spec: _ArtifactSpec,
    journal: dict[str, Any],
    *,
    journal_path: Path,
    legacy_dir: Path,
    data_dir: Path,
    work_dir: Path,
    same_location: bool,
) -> list[str]:
    artifacts = journal["artifacts"]
    record = artifacts[spec.key]
    if record["state"] == "complete":
        return []
    if record["state"] == "pending":
        _prepare_artifact(
            spec,
            record,
            legacy_dir=legacy_dir,
            data_dir=data_dir,
            work_dir=work_dir,
            same_location=same_location,
        )
        _write_json_file(journal_path, journal)
    _verify_outputs(record, data_dir, spec.maximum_bytes)
    cleaned = _cleanup_sources(
        record,
        spec,
        legacy_dir,
        data_dir,
        journal,
        journal_path,
    )
    record["state"] = "complete"
    _write_json_file(journal_path, journal)
    return cleaned


def _remove_empty_legacy_cache(legacy_dir: Path, data_dir: Path) -> None:
    cache_dir = legacy_dir / "cache"
    try:
        if cache_dir != data_dir / "cache":
            cache_dir.rmdir()
    except (FileNotFoundError, OSError):
        return


def migrate_legacy_data(
    *,
    legacy_dir: Path = WORKING_DIR,
    data_dir: Path = DATA_DIR,
) -> MigrationResult:
    """Transactionally migrate executable-relative mutable data.

    The caller must hold the legacy lock followed by the per-user lock for the
    entire process lifetime. The journal is written after every durable phase;
    the storage marker is written only after all targets/quarantines verify and
    every obsolete source path is durably removed.
    """
    _ensure_private_directory(data_dir)
    metadata_path = data_dir / "storage.json"
    version = _metadata_version(metadata_path)
    if version is not None:
        if version > STORAGE_VERSION:
            raise DataMigrationError(
                f"Mutable data version {version} is newer than supported version "
                f"{STORAGE_VERSION}"
            )
        if version == STORAGE_VERSION:
            return MigrationResult((), (), (), (), ())

    work_dir = data_dir / "migration-work"
    _clean_work_directory(work_dir)
    _ensure_private_directory(data_dir / "migration-quarantine")
    journal_path = data_dir / "migration-journal.json"
    journal = _load_journal(journal_path, legacy_dir)
    if not _path_exists(journal_path):
        _write_json_file(journal_path, journal)

    try:
        same_location = os.path.abspath(os.fspath(legacy_dir)) == os.path.abspath(
            os.fspath(data_dir)
        )
        cleaned: list[str] = []
        for spec in _ARTIFACTS:
            cleaned.extend(
                _migrate_artifact(
                    spec,
                    journal,
                    journal_path=journal_path,
                    legacy_dir=legacy_dir,
                    data_dir=data_dir,
                    work_dir=work_dir,
                    same_location=same_location,
                )
            )
    finally:
        _clean_work_directory(work_dir)

    records = journal["artifacts"]
    if any(record.get("state") != "complete" for record in records.values()):
        raise DataMigrationError("Migration journal did not reach completion")

    _remove_empty_legacy_cache(legacy_dir, data_dir)
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    journal["completed_at"] = completed_at
    _write_json_file(journal_path, journal)
    metadata = {
        "version": STORAGE_VERSION,
        "journal_version": JOURNAL_VERSION,
        "completed_at": completed_at,
        "legacy_source": os.path.abspath(os.fspath(legacy_dir)),
        "artifacts": {
            key: {
                "result": record.get("result"),
                "reader_version": record.get("reader_version"),
                "credential": record.get("credential"),
                "source_format": record.get("source_format"),
                "source_version": record.get("source_version"),
                "quarantined": record.get("quarantined", False),
            }
            for key, record in records.items()
        },
    }
    _write_json_file(metadata_path, metadata)

    return MigrationResult(
        migrated=tuple(
            key for key, record in records.items() if record.get("result") == "migrated"
        ),
        recovered=tuple(
            key for key, record in records.items() if record.get("result") == "recovered"
        ),
        destination_wins=tuple(
            key
            for key, record in records.items()
            if record.get("result") == "destination-wins"
        ),
        quarantined=tuple(
            key for key, record in records.items() if record.get("quarantined")
        ),
        cleaned=tuple(cleaned),
    )
