from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import CookieError
from pathlib import Path
from pickle import UnpicklingError
from typing import Any, Final, Literal

import aiohttp  # pyright: ignore[reportMissingImports]

from constants import DATA_DIR, WORKING_DIR
from utils import atomic_write_bytes, durable_unlink

STORAGE_VERSION: Final = 2
JOURNAL_VERSION: Final = 1
_ARTIFACT_PLAN_VERSION: Final = 3
_CAPTURE_TOKEN_HEX_LENGTH: Final = 32
_MAX_METADATA_BYTES: Final = 64 * 1024
_MAX_JOURNAL_BYTES: Final = 1024 * 1024
_LEGACY_CREDENTIAL_COOKIE_NAMES: Final = frozenset(
    {"auth-token", "login", "name", "persistent", "twilight-user"}
)
_COOKIE_JSON_REQUIRED_FIELDS: Final = frozenset({"key", "value", "coded_value"})
_COOKIE_PARSER_ERRORS: Final = (
    AttributeError,
    CookieError,
    EOFError,
    IndexError,
    KeyError,
    OverflowError,
    RecursionError,
    TypeError,
    UnpicklingError,
    ValueError,
)
_COOKIE_JSON_OPTIONAL_FIELDS: Final = frozenset(
    {
        "comment",
        "domain",
        "expires",
        "expires_timestamp",
        "host_only",
        "httponly",
        "max-age",
        "path",
        "samesite",
        "secure",
        "version",
    }
)


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


def _validate_cookie_json(value: Mapping[str, Any]) -> None:
    """Validate the exact JSON shape emitted by the pinned aiohttp CookieJar."""
    for compound_key, cookie_group in value.items():
        if not isinstance(compound_key, str) or "|" not in compound_key:
            raise _InvalidArtifact("Cookie JSON has an invalid domain/path key")
        domain, _storage_path = compound_key.split("|", 1)
        if not isinstance(cookie_group, dict):
            raise _InvalidArtifact("Cookie JSON domain entry must be an object")
        for name, raw_morsel in cookie_group.items():
            if not isinstance(name, str) or not name:
                raise _InvalidArtifact("Cookie JSON has an invalid cookie name")
            if not isinstance(raw_morsel, dict):
                raise _InvalidArtifact("Cookie JSON cookie entry must be an object")
            fields = set(raw_morsel)
            missing = _COOKIE_JSON_REQUIRED_FIELDS - fields
            unknown = fields - (
                _COOKIE_JSON_REQUIRED_FIELDS | _COOKIE_JSON_OPTIONAL_FIELDS
            )
            if missing or unknown:
                raise _InvalidArtifact("Cookie JSON cookie fields are invalid")
            for field in _COOKIE_JSON_REQUIRED_FIELDS:
                if not isinstance(raw_morsel[field], str):
                    raise _InvalidArtifact("Cookie JSON cookie value is invalid")
            if raw_morsel["key"] != name:
                raise _InvalidArtifact("Cookie JSON cookie key does not match its name")
            for field in fields - _COOKIE_JSON_REQUIRED_FIELDS:
                raw_value = raw_morsel[field]
                if field == "host_only":
                    if type(raw_value) is not bool:
                        raise _InvalidArtifact("Cookie JSON host-only flag is invalid")
                elif field == "expires_timestamp":
                    if (
                        isinstance(raw_value, bool)
                        or not isinstance(raw_value, (int, float))
                        or not math.isfinite(raw_value)
                    ):
                        raise _InvalidArtifact("Cookie JSON expiry is invalid")
                elif not isinstance(raw_value, str):
                    raise _InvalidArtifact("Cookie JSON attribute is invalid")
            morsel_domain = raw_morsel.get("domain")
            if (
                isinstance(morsel_domain, str)
                and morsel_domain.lstrip(".").lower() != domain.lower()
            ):
                raise _InvalidArtifact("Cookie JSON domain metadata is inconsistent")
            if raw_morsel.get("host_only") and not domain:
                raise _InvalidArtifact("Cookie JSON host-only cookie has no host")


def _apply_legacy_cookie_policy(jar: aiohttp.CookieJar) -> None:
    """Conservatively scope legacy credentials whose host-only bit was lost.

    aiohttp's old pickle contained only ``_cookies``. An explicit Domain equal
    to the response host and a host-only cookie are therefore indistinguishable.
    Credential names are restricted to their exact stored host; non-credential
    cookies retain inferable domain-cookie behavior.
    """
    try:
        cookies = jar._cookies
        host_only = jar._host_only_cookies
        if not isinstance(cookies, Mapping) or not isinstance(host_only, set):
            raise _InvalidArtifact("Legacy cookie jar has an invalid container")
        for domain_path, cookie_group in cookies.items():
            if (
                not isinstance(domain_path, tuple)
                or len(domain_path) != 2
                or not all(isinstance(item, str) for item in domain_path)
                or not isinstance(cookie_group, Mapping)
            ):
                raise _InvalidArtifact("Legacy cookie jar has an invalid domain entry")
            domain, _path = domain_path
            for name, morsel in cookie_group.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or getattr(morsel, "key", None) != name
                ):
                    raise _InvalidArtifact("Legacy cookie jar has an invalid cookie")
                if name.casefold() in _LEGACY_CREDENTIAL_COOKIE_NAMES:
                    if not domain:
                        raise _InvalidArtifact(
                            "Legacy credential cookie has no exact host"
                        )
                    host_only.add((domain, name))
    except _InvalidArtifact:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise _InvalidArtifact("Legacy cookie jar has an invalid shape") from exc


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


def _decode_cookie_source_json(data: bytes) -> dict[str, Any] | None:
    appears_to_be_json = data.lstrip().startswith((b"{", b"["))
    try:
        text = data.decode("utf8")
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        if appears_to_be_json:
            raise DataMigrationError("Legacy cookie JSON is malformed") from exc
        return None
    except (RecursionError, TypeError, ValueError) as exc:
        raise DataMigrationError("Legacy cookie JSON is malformed") from exc
    if not isinstance(value, dict):
        raise DataMigrationError(
            "Legacy cookie JSON is malformed: root must be an object"
        )
    try:
        _validate_cookie_json(value)
    except _InvalidArtifact as exc:
        raise DataMigrationError("Legacy cookie JSON is malformed") from exc
    return value


def _load_cookie_source(
    jar: aiohttp.CookieJar,
    source_path: Path,
    *,
    source_is_json: bool,
) -> None:
    try:
        jar.load(source_path)
        if not source_is_json:
            _apply_legacy_cookie_policy(jar)
    except Exception as exc:
        if isinstance(exc, _InvalidArtifact):
            raise
        if isinstance(exc, OSError):
            raise DataMigrationError("Unable to read cookie conversion input") from exc
        if isinstance(exc, _COOKIE_PARSER_ERRORS):
            if source_is_json:
                raise DataMigrationError("Legacy cookie JSON is malformed") from exc
            raise _InvalidArtifact("Legacy cookie pickle is invalid") from exc
        raise DataMigrationError("Unexpected cookie parser failure") from exc


def _serialize_cookie_jar(
    jar: aiohttp.CookieJar,
    output_path: Path,
    *,
    source_is_json: bool,
) -> dict[str, Any]:
    try:
        jar.save(output_path)
        output = _read_regular_file(output_path, 8 * 1024 * 1024)
        value = _decode_json_object(output, "Converted cookie jar")
        _validate_cookie_json(value)
        return value
    except Exception as exc:
        if isinstance(exc, DataMigrationError):
            raise
        if isinstance(exc, _InvalidArtifact):
            if source_is_json:
                raise DataMigrationError("Legacy cookie JSON is malformed") from exc
            raise
        if isinstance(exc, OSError):
            raise DataMigrationError("Unable to persist converted cookie jar") from exc
        if isinstance(exc, _COOKIE_PARSER_ERRORS):
            if source_is_json:
                raise DataMigrationError("Legacy cookie JSON is malformed") from exc
            raise _InvalidArtifact("Legacy cookie pickle is invalid") from exc
        raise DataMigrationError("Unexpected cookie serialization failure") from exc


def _read_cookies_v2(data: bytes, work_dir: Path) -> _ReadResult:
    """Convert current JSON or legacy pickle through aiohttp's restricted reader.

    Current JSON is shape-validated before aiohttp sees it so malformed JSON
    cannot fall through into pickle parsing. Legacy pickle uses aiohttp's
    restricted unpickler; no general pickle loader is imported or invoked.
    """
    source_format = "cookie-json-v2"
    source_json = _decode_cookie_source_json(data)
    if source_json is None:
        source_format = "cookie-pickle-v1-exact-host-credentials"

    source_path = _private_temp_file(work_dir, "cookie-input-", data)
    output_path = _private_temp_file(work_dir, "cookie-output-", b"")
    loop = asyncio.new_event_loop()
    try:
        jar = aiohttp.CookieJar(loop=loop)
        source_is_json = source_json is not None
        _load_cookie_source(jar, source_path, source_is_json=source_is_json)
        value = _serialize_cookie_jar(
            jar,
            output_path,
            source_is_json=source_is_json,
        )
        return _ReadResult(
            _canonical_json(value),
            1 if source_json is None else 2,
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


def _new_artifact_record(spec: _ArtifactSpec) -> dict[str, Any]:
    return {
        "state": "pending",
        "plan_version": _ARTIFACT_PLAN_VERSION,
        "reader_version": spec.reader_version,
        "credential": spec.credential,
    }


def _replan_artifact_record(
    spec: _ArtifactSpec,
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    outputs = previous.get("outputs", [])
    if not isinstance(outputs, list) or any(
        not isinstance(output, dict) for output in outputs
    ):
        raise DataMigrationError(f"Migration outputs are invalid for {spec.key}")
    record = _new_artifact_record(spec)
    record["outputs"] = list(outputs)
    record["quarantined"] = bool(previous.get("quarantined", False))
    return record


def _new_journal(legacy_dir: Path) -> dict[str, Any]:
    return {
        "version": JOURNAL_VERSION,
        "target_storage_version": STORAGE_VERSION,
        "legacy_source": os.path.abspath(os.fspath(legacy_dir)),
        "artifacts": {spec.key: _new_artifact_record(spec) for spec in _ARTIFACTS},
    }


def _upgrade_artifact_record(
    spec: _ArtifactSpec,
    record: dict[str, Any],
) -> dict[str, Any]:
    raw_plan_version = record.get("plan_version", 1)
    if (
        isinstance(raw_plan_version, bool)
        or not isinstance(raw_plan_version, int)
        or raw_plan_version < 1
    ):
        raise DataMigrationError(f"Migration plan is invalid for {spec.key}")
    if raw_plan_version > _ARTIFACT_PLAN_VERSION:
        raise DataMigrationError(f"Migration plan is newer than supported for {spec.key}")
    if raw_plan_version == _ARTIFACT_PLAN_VERSION:
        return record

    state = record.get("state")
    if state == "pending":
        return _replan_artifact_record(spec, record)
    if state == "installed":
        cleanup = record.get("cleanup", [])
        if not isinstance(cleanup, list) or any(
            not isinstance(item, dict) for item in cleanup
        ):
            raise DataMigrationError(f"Legacy cleanup state is invalid for {spec.key}")
        removed_values = [item.get("removed", False) for item in cleanup]
        if any(type(removed) is not bool for removed in removed_values):
            raise DataMigrationError(f"Legacy cleanup state is invalid for {spec.key}")
        if any(removed_values):
            raise DataMigrationError(
                f"Legacy cleanup already began before migration plan upgrade for {spec.key}"
            )
        if raw_plan_version == 1:
            return _replan_artifact_record(spec, record)
        record["plan_version"] = _ARTIFACT_PLAN_VERSION
        return record
    if state == "complete":
        record["plan_version"] = _ARTIFACT_PLAN_VERSION
        return record
    raise DataMigrationError(f"Migration journal state is invalid for {spec.key}")


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
        artifacts[spec.key] = _upgrade_artifact_record(spec, record)
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
    same_location: bool,
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
    if same_location:
        # The canonical path is the destination in this mode. Its `.new`
        # sibling is still a recovery generation and must never be stranded.
        candidates = (
            legacy_candidates[1],
            ("v1-quarantine", old_quarantine, data_dir),
        )
    else:
        candidates = (
            *legacy_candidates,
            ("v1-quarantine", old_quarantine, data_dir),
        )
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


def _add_output(
    outputs: list[dict[str, str]],
    data_dir: Path,
    path: Path,
    data: bytes,
) -> None:
    output = _output_record(data_dir, path, data)
    if output not in outputs:
        outputs.append(output)


def _preserve_generation(
    outputs: list[dict[str, str]],
    data_dir: Path,
    spec: _ArtifactSpec,
    *,
    role: str,
    reason: str,
    data: bytes,
) -> Path:
    path = _quarantine_path(
        data_dir,
        spec,
        f"{role}.{reason}.{_digest(data)}",
    )
    _write_preserved_bytes(path, data)
    _add_output(outputs, data_dir, path, data)
    return path


def _preserve_distinct_losers(
    outputs: list[dict[str, str]],
    data_dir: Path,
    spec: _ArtifactSpec,
    snapshots: list[_SourceSnapshot],
    *,
    winner_payload: bytes,
    reason: str,
) -> bool:
    seen_payloads = {_digest(winner_payload)}
    preserved = False
    for snapshot in snapshots:
        if snapshot.parsed is None:
            continue
        payload_digest = _digest(snapshot.parsed.payload)
        if payload_digest in seen_payloads:
            continue
        _preserve_generation(
            outputs,
            data_dir,
            spec,
            role=snapshot.role,
            reason=reason,
            data=snapshot.data,
        )
        seen_payloads.add(payload_digest)
        preserved = True
    return preserved


def _install_payload(
    spec: _ArtifactSpec,
    destination: Path,
    payload: bytes,
    work_dir: Path,
) -> bytes:
    try:
        atomic_write_bytes(
            destination,
            payload,
            remove_legacy_new=False,
        )
    except OSError as exc:
        raise DataMigrationError(f"Unable to install migrated file: {destination}") from exc
    installed_state = _validate_destination(spec, destination, work_dir)
    if installed_state is None:
        raise DataMigrationError(f"Migrated file verification failed: {destination}")
    installed, installed_data = installed_state
    if installed.payload != payload:
        raise DataMigrationError(f"Migrated file verification failed: {destination}")
    return installed_data


def _preserve_losers_and_install(
    outputs: list[dict[str, str]],
    data_dir: Path,
    spec: _ArtifactSpec,
    destination: Path,
    work_dir: Path,
    valid: list[_SourceSnapshot],
    selected: _SourceSnapshot,
) -> bool:
    if selected.parsed is None:
        raise DataMigrationError("Selected migration source is invalid")
    preserved = _preserve_distinct_losers(
        outputs,
        data_dir,
        spec,
        valid,
        winner_payload=selected.parsed.payload,
        reason="source-conflict",
    )
    installed_data = _install_payload(
        spec,
        destination,
        selected.parsed.payload,
        work_dir,
    )
    _add_output(outputs, data_dir, destination, installed_data)
    return preserved


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


def _legacy_cleanup_staging_path(path: Path, role: str, digest: str) -> Path:
    return path.with_name(f".{path.name}.migration-{role}-{digest}.staged")


def _capture_path(
    path: Path,
    role: str,
    item: dict[str, Any],
) -> tuple[Path, bool]:
    prefix = f".{path.name}.migration-capture-{role}-"
    suffix = ".quarantine"
    capture_name = item.get("capture_name")
    created = False
    if capture_name is None:
        capture_name = f"{prefix}{secrets.token_hex(16)}{suffix}"
        item["capture_name"] = capture_name
        created = True
    if not isinstance(capture_name, str) or Path(capture_name).name != capture_name:
        raise DataMigrationError(f"Migration capture path is invalid: {path}")
    if not capture_name.startswith(prefix) or not capture_name.endswith(suffix):
        raise DataMigrationError(f"Migration capture path is invalid: {path}")
    token = capture_name[len(prefix) : -len(suffix)]
    if (
        len(token) != _CAPTURE_TOKEN_HEX_LENGTH
        or token.lower() != token
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise DataMigrationError(f"Migration capture path is invalid: {path}")
    return path.with_name(capture_name), created


def _regular_file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DataMigrationError(f"Unable to inspect migration capture: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise DataMigrationError(f"Migration capture is not a regular file: {path}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_stable_capture(
    path: Path,
    maximum_bytes: int,
) -> tuple[bytes, bytes, bool]:
    before = _regular_file_identity(path)
    first = _read_regular_file(path, maximum_bytes)
    between = _regular_file_identity(path)
    second = _read_regular_file(path, maximum_bytes)
    after = _regular_file_identity(path)
    return first, second, before == between == after and first == second


def _make_capture_private(path: Path) -> None:
    try:
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(path, 0o600, follow_symlinks=False)
        else:
            os.chmod(path, 0o600)
    except OSError as exc:
        raise DataMigrationError(f"Unable to secure migration capture: {path}") from exc


def _atomic_stage_source(source: Path, staging: Path) -> bool:
    """Atomically move the current source name to a no-follow staging name."""
    if source.parent != staging.parent:
        raise DataMigrationError("Migration cleanup staging escaped its directory")
    if _path_exists(staging):
        return True

    use_directory_fd = (
        os.name != "nt"
        and os.rename in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )
    if use_directory_fd:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            directory_fd = os.open(source.parent, flags)
        except OSError as exc:
            raise DataMigrationError(
                f"Unable to open migration source directory: {source.parent}"
            ) from exc
        try:
            try:
                info = os.stat(source.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(info.st_mode):
                raise DataMigrationError(
                    f"Migration path is not a regular file: {source}"
                )
            try:
                os.stat(staging.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                staging_exists = False
            else:
                staging_exists = True
            if staging_exists:
                raise DataMigrationError(
                    f"Migration cleanup staging path already exists: {staging}"
                )
            try:
                os.rename(
                    source.name,
                    staging.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return False
            os.fsync(directory_fd)
            return True
        except OSError as exc:
            raise DataMigrationError(
                f"Unable to stage migrated source: {source}"
            ) from exc
        finally:
            os.close(directory_fd)

    try:
        info = source.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DataMigrationError(f"Unable to inspect migration source: {source}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise DataMigrationError(f"Migration path is not a regular file: {source}")
    if _path_exists(staging):
        raise DataMigrationError(
            f"Migration cleanup staging path already exists: {staging}"
        )
    try:
        os.rename(source, staging)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DataMigrationError(f"Unable to stage migrated source: {source}") from exc
    return True


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
    outputs = record.get("outputs", [])
    if not isinstance(outputs, list):
        raise DataMigrationError(f"Migration outputs are invalid for {spec.key}")
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
        staged = item.get("staged", False)
        retained = item.get("retained", False)
        if (
            role not in source_paths
            or not isinstance(digest, str)
            or type(removed) is not bool
            or type(staged) is not bool
            or type(retained) is not bool
        ):
            raise DataMigrationError(f"Migration cleanup metadata is invalid for {spec.key}")
        path = source_paths[role]
        capture, capture_created = _capture_path(path, role, item)
        if capture_created:
            _write_json_file(journal_path, journal)
        if removed:
            if _path_exists(path):
                raise DataMigrationError(f"Removed migration source reappeared: {path}")
            if retained and not _path_exists(capture):
                raise DataMigrationError(f"Retained migration capture disappeared: {capture}")
            continue

        legacy_staging = _legacy_cleanup_staging_path(path, role, digest)
        if not _path_exists(capture):
            source_to_capture = (
                legacy_staging if _path_exists(legacy_staging) else path
            )
            if not _atomic_stage_source(source_to_capture, capture):
                raise DataMigrationError(
                    f"Migration source disappeared before durable capture: {path}"
                )
            item["staged"] = True
            item["retained"] = True
            _write_json_file(journal_path, journal)

        _make_capture_private(capture)
        first, current, stable = _read_stable_capture(
            capture,
            spec.maximum_bytes,
        )
        first_digest = _digest(first)
        current_digest = _digest(current)
        if first_digest == digest:
            _preserve_generation(
                outputs,
                data_dir,
                spec,
                role=role,
                reason="captured-source",
                data=first,
            )
            record["outputs"] = outputs
            _write_json_file(journal_path, journal)

        if not stable or first_digest != digest or current_digest != digest:
            preserved = _preserve_generation(
                outputs,
                data_dir,
                spec,
                role=role,
                reason="replacement-conflict",
                data=current,
            )
            record["outputs"] = outputs
            record["quarantined"] = True
            previous_replacement = item.get("replacement_sha256")
            item["replacement_sha256"] = current_digest
            _write_json_file(journal_path, journal)
            if stable and previous_replacement == current_digest:
                cleaned.append(f"{spec.key}:{role}")
                item["removed"] = True
                item["staged"] = True
                item["retained"] = True
                _write_json_file(journal_path, journal)
                continue
            raise DataMigrationError(
                f"Migration capture changed during verification and was preserved at "
                f"{preserved}"
            )

        if _path_exists(path):
            replacement = _read_regular_file(path, spec.maximum_bytes)
            preserved = _preserve_generation(
                outputs,
                data_dir,
                spec,
                role=role,
                reason="replacement-conflict",
                data=replacement,
            )
            record["outputs"] = outputs
            record["quarantined"] = True
            _write_json_file(journal_path, journal)
            raise DataMigrationError(
                f"Migration source was replaced during cleanup and was preserved at "
                f"{preserved}"
            )

        cleaned.append(f"{spec.key}:{role}")
        item["removed"] = True
        item["staged"] = True
        item["retained"] = True
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
    journal: dict[str, Any],
    journal_path: Path,
) -> None:
    destination = data_dir / spec.destination_relative
    _ensure_private_directory(destination.parent)
    destination_state = _validate_destination(spec, destination, work_dir)
    snapshots = _read_source_snapshots(
        spec,
        legacy_dir,
        data_dir,
        work_dir,
        same_location=same_location,
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

    raw_outputs = record.get("outputs", [])
    if not isinstance(raw_outputs, list) or any(
        not isinstance(output, dict) for output in raw_outputs
    ):
        raise DataMigrationError(f"Migration journal outputs are invalid for {spec.key}")
    outputs: list[dict[str, str]] = list(raw_outputs)
    if outputs:
        _verify_outputs(record, data_dir, spec.maximum_bytes)
    quarantined = bool(record.get("quarantined", False))
    for snapshot in snapshots:
        _preserve_generation(
            outputs,
            data_dir,
            spec,
            role=snapshot.role,
            reason="captured-source",
            data=snapshot.data,
        )
    for snapshot in invalid:
        quarantine = _quarantine_path(data_dir, spec, f"{snapshot.role}.invalid")
        _write_preserved_bytes(quarantine, snapshot.data)
        _add_output(outputs, data_dir, quarantine, snapshot.data)
        quarantined = True

    result = "absent"
    source_format: str | None = None
    source_version: int | None = None
    if selected is not None and selected.parsed is not None:
        source_format = selected.parsed.source_format
        source_version = selected.parsed.source_version

    recover_new_over_destination = (
        same_location
        and destination_state is not None
        and selected is not None
        and selected.role == "new"
        and selected.parsed is not None
        and selected.parsed.payload != destination_state[0].payload
    )
    if recover_new_over_destination:
        if (
            destination_state is None
            or selected is None
            or selected.parsed is None
        ):
            raise DataMigrationError("Same-location recovery state is inconsistent")
        _destination_result, destination_data = destination_state
        _preserve_generation(
            outputs,
            data_dir,
            spec,
            role="canonical",
            reason="recovery-conflict",
            data=destination_data,
        )
        _preserve_distinct_losers(
            outputs,
            data_dir,
            spec,
            valid,
            winner_payload=selected.parsed.payload,
            reason="source-conflict",
        )
        record["outputs"] = outputs
        record["quarantined"] = True
        _write_json_file(journal_path, journal)
        installed_data = _install_payload(
            spec,
            destination,
            selected.parsed.payload,
            work_dir,
        )
        _add_output(outputs, data_dir, destination, installed_data)
        quarantined = True
        result = "recovered"
    elif destination_state is not None:
        destination_result, destination_data = destination_state
        _add_output(outputs, data_dir, destination, destination_data)
        quarantined = (
            _preserve_distinct_losers(
                outputs,
                data_dir,
                spec,
                valid,
                winner_payload=destination_result.payload,
                reason="destination-conflict",
            )
            or quarantined
        )
        result = "destination-wins"
    elif selected is not None and selected.parsed is not None:
        quarantined = (
            _preserve_losers_and_install(
                outputs,
                data_dir,
                spec,
                destination,
                work_dir,
                valid,
                selected,
            )
            or quarantined
        )
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
                    "staged": False,
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
            journal=journal,
            journal_path=journal_path,
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


def _same_location_recovery_keys(data_dir: Path) -> tuple[str, ...]:
    keys: list[str] = []
    for spec in _ARTIFACTS:
        canonical = data_dir / spec.source_relative
        recovery = canonical.with_name(f"{canonical.name}.new")
        if not _validate_directory_chain(data_dir, recovery.parent):
            continue
        if _path_exists(recovery):
            keys.append(spec.key)
    return tuple(keys)


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
    same_location = os.path.abspath(os.fspath(legacy_dir)) == os.path.abspath(
        os.fspath(data_dir)
    )
    recovery_keys = _same_location_recovery_keys(data_dir) if same_location else ()
    metadata_path = data_dir / "storage.json"
    version = _metadata_version(metadata_path)
    if version is not None:
        if version > STORAGE_VERSION:
            raise DataMigrationError(
                f"Mutable data version {version} is newer than supported version "
                f"{STORAGE_VERSION}"
            )
        if version == STORAGE_VERSION and not recovery_keys:
            return MigrationResult((), (), (), (), ())

    work_dir = data_dir / "migration-work"
    _clean_work_directory(work_dir)
    _ensure_private_directory(data_dir / "migration-quarantine")
    journal_path = data_dir / "migration-journal.json"
    journal = _load_journal(journal_path, legacy_dir)
    if recovery_keys:
        artifacts = journal["artifacts"]
        for spec in _ARTIFACTS:
            if spec.key in recovery_keys:
                artifacts[spec.key] = _replan_artifact_record(
                    spec,
                    artifacts[spec.key],
                )
        journal.pop("completed_at", None)
        _write_json_file(journal_path, journal)
    elif not _path_exists(journal_path):
        _write_json_file(journal_path, journal)

    try:
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
