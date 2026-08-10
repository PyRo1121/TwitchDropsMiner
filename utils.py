from __future__ import annotations

import io
import os
import re
import sys
import json
import random
import secrets
import unicodedata
import string
import asyncio
import logging
import traceback
from math import isfinite
import tempfile
import stat
import webbrowser
from copy import deepcopy
from enum import Enum
from pathlib import Path
from contextlib import suppress
from datetime import datetime, timezone
from collections import abc
from typing import Any, Literal, Callable, Generic, Mapping, TypeVar, ParamSpec, cast

from yarl import URL  # pyright: ignore[reportMissingImports]

from exceptions import ExitRequest, ReloadRequest
from constants import DUMP_PATH, IS_PACKAGED, JsonType, PriorityMode


_T = TypeVar("_T")  # type
_D = TypeVar("_D")  # default
_P = ParamSpec("_P")  # params
_JSON_T = TypeVar("_JSON_T", bound=Mapping[Any, Any])
logger = logging.getLogger("TwitchDrops")

# Matches an RFC3339 fractional-seconds group (e.g. ".123456789") so the
# microseconds portion can be capped for Python 3.10's fromisoformat.
_FRACTION_RE = re.compile(r"\.(\d+)")


def open_dump(mode: Literal["w", "a"]):
    """Open the trusted application debug dump with a controlled error."""
    try:
        return open(DUMP_PATH, mode, encoding="utf8")
    except OSError as exc:
        raise RuntimeError(f"Unable to open dump file: {DUMP_PATH}") from exc


async def cancel_tasks(tasks: abc.Iterable[asyncio.Future[Any]]) -> None:
    """Cancel tasks and consume their completion, including cancellation errors."""
    task_list = list(tasks)
    for task in task_list:
        if not task.done():
            task.cancel()
    if task_list:
        await asyncio.gather(*task_list, return_exceptions=True)


def chunk(to_chunk: abc.Iterable[_T], chunk_length: int) -> abc.Generator[list[_T], None, None]:
    list_to_chunk = list(to_chunk)
    for i in range(0, len(list_to_chunk), chunk_length):
        yield list_to_chunk[i:i + chunk_length]


def format_traceback(exc: BaseException, **kwargs: Any) -> str:
    """
    Like `traceback.print_exc` but returns a string. Uses the passed-in exception.
    Any additional `**kwargs` are passed to the underlying `traceback.format_exception`.
    """
    return ''.join(traceback.format_exception(type(exc), exc, **kwargs))


def lock_file(path: Path) -> tuple[bool, io.TextIOWrapper]:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise OSError(f"Lock path is a symlink: {path}")
    descriptor = os.open(path, flags, 0o600)
    file: io.TextIOWrapper | None = None
    try:
        descriptor_chmod = getattr(os, "fchmod", None)
        if descriptor_chmod is not None:
            descriptor_chmod(descriptor, 0o600)
        else:
            path.chmod(0o600)
        file = os.fdopen(descriptor, "r+", encoding="utf8")
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if file is None:
        raise RuntimeError("Unable to open lock file")
    file.seek(0, os.SEEK_END)
    if file.tell() == 0:
        file.write("ツ")
        file.flush()
    file.seek(0)
    if sys.platform == "win32":
        import msvcrt
        try:
            # we need to lock at least one byte for this to work
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False, file
        return True, file
    if sys.platform in ("linux", "darwin"):
        import fcntl
        try:
            fcntl.lockf(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False, file
        return True, file
    # for unsupported systems, just always return True
    return True, file


def lock_file_set(
    paths: abc.Iterable[Path],
) -> tuple[bool, tuple[io.TextIOWrapper, ...]]:
    """Acquire distinct lock files in caller-supplied order or release all.

    The order is part of the protocol. Startup uses the legacy lock first and
    the per-user lock second so old and new binaries cannot run concurrently.
    """
    acquired: list[io.TextIOWrapper] = []
    seen: set[str] = set()
    try:
        for path in paths:
            identity = os.path.abspath(os.fspath(path))
            if identity in seen:
                continue
            seen.add(identity)
            success, file = lock_file(path)
            if not success:
                file.close()
                for held in reversed(acquired):
                    held.close()
                return False, ()
            acquired.append(file)
    except BaseException:
        for held in reversed(acquired):
            held.close()
        raise
    return True, tuple(acquired)


def json_minify(data: JsonType | list[JsonType]) -> str:
    """
    Returns minified JSON for payload usage.
    """
    return json.dumps(data, separators=(',', ':'))


_LOG_REDACTED = "<redacted>"
_LOG_SENSITIVE_KEYS = frozenset({
    "access_token",
    "auth_token",
    "authorization",
    "authy_token",
    "captcha",
    "captcha_proof",
    "client_secret",
    "client_session_id",
    "cookie",
    "cookies",
    "device_code",
    "password",
    "passwd",
    "passphrase",
    "proxy",
    "refresh_token",
    "secret",
    "session_id",
    "token",
    "twitchguard_code",
    "user_code",
    "username",
    "x_device_id",
})
_LOG_SENSITIVE_QUERY_KEYS = frozenset({"sig", "signature"})


def _normalise_log_key(key: Any) -> str:
    return str(key).casefold().replace("-", "_")


def _is_sensitive_log_key(key: Any) -> bool:
    normalised = _normalise_log_key(key)
    return (
        normalised in _LOG_SENSITIVE_KEYS
        or normalised.endswith("_token")
        or normalised.endswith("_password")
        or normalised.endswith("_secret")
        or normalised in _LOG_SENSITIVE_QUERY_KEYS
    )


def _redact_log_url(value: URL) -> str:
    if value.user is not None:
        value = value.with_user(None).with_password(None)
    if value.query:
        value = value.with_query(
            [
                (
                    key,
                    _LOG_REDACTED if _is_sensitive_log_key(key) else item,
                )
                for key, item in value.query.items()
            ]
        )
    return str(value)


def redact_log_value(
    value: Any,
    *,
    key: Any = None,
    _redact_scalars: bool = False,
) -> Any:
    """Return a log-safe copy of a value without changing the original."""
    normalised_key = _normalise_log_key(key) if key is not None else None
    if key is not None and _is_sensitive_log_key(key):
        return _LOG_REDACTED
    redact_scalars = _redact_scalars or normalised_key in {"data", "json"}
    if redact_scalars and not isinstance(
        value, (abc.Mapping, list, tuple, set)
    ):
        return _LOG_REDACTED
    if isinstance(value, URL):
        return _redact_log_url(value)
    if isinstance(value, abc.Mapping):
        return {
            item_key: redact_log_value(
                item,
                key=item_key,
                _redact_scalars=redact_scalars,
            )
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_log_value(item, _redact_scalars=redact_scalars)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_log_value(item, _redact_scalars=redact_scalars)
            for item in value
        )
    if isinstance(value, set):
        return {
            redact_log_value(item, _redact_scalars=redact_scalars)
            for item in value
        }
    if normalised_key in {"url", "uri"} and isinstance(value, str):
        try:
            parsed = URL(value)
        except ValueError:
            return value
        if parsed.scheme and parsed.host:
            return _redact_log_url(parsed)
    return value


def timestamp(value: str) -> datetime:
    """Parse Twitch's RFC3339 timestamps and normalize them to UTC."""
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    # Python 3.10's fromisoformat rejects more than 6 fractional digits, while
    # Twitch often sends nanosecond precision. Cap the fraction to microseconds.
    normalized = _FRACTION_RE.sub(
        lambda match: (match.group(0)[:1] + match.group(1)[:6]), normalized
    )
    try:
        parsed = datetime.fromisoformat(normalized)
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError(f"Invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isonow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", 'Z')


def format_duration(seconds: float, *, pad_hours: bool = True) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    hours_text = f"{hours:02}" if pad_hours else f"{hours:>2}"
    return f"{hours_text}:{minutes:02}:{seconds_part:02}"


def slugify(name: str) -> str:
    """Create Twitch's dash-separated game slug from a display name."""
    slug_text = re.sub(r"'", "", name.lower())
    slug_text = re.sub(r"\W+", "-", slug_text)
    return re.sub(r"-{2,}", "-", slug_text.strip("-"))


def normalize_key(name: str) -> str:
    """Create a conservative ASCII key for exact name matching."""
    normalized = unicodedata.normalize("NFKD", name).encode(
        "ascii", "ignore"
    ).decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", normalized.lower())


CHARS_ASCII = string.ascii_letters + string.digits
CHARS_HEX_LOWER = string.digits + "abcdef"


def create_nonce(chars: str, length: int) -> str:
    return ''.join(secrets.choice(chars) for _ in range(length))


def safe_int(value: Any) -> int | None:
    """Parse an integer without accepting booleans or lossy numeric coercion."""
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def require_int(value: Any, message: str) -> int:
    """Parse a lossless integer or raise with the caller's context."""
    parsed = safe_int(value)
    if parsed is None:
        raise ValueError(message)
    return parsed


def _handle_task_exception(
    exc: BaseException,
    task_name: str,
    args: tuple[Any, ...],
    critical: bool,
) -> None:
    if not isinstance(exc, Exception):
        raise exc
    logger.exception(f"Exception in {task_name} task")
    if not critical:
        return
    # A critical task's death should trigger a termination. There isn't an
    # easy and sure way to obtain the Twitch instance here, but we can
    # improvise finding it from the first argument.
    from twitch import Twitch  # cyclic import
    probe = args[0] if args else None
    if isinstance(probe, Twitch):
        probe.close()
    elif probe is not None:
        owner = getattr(probe, "_twitch", None)
        if isinstance(owner, Twitch):
            owner.close()


def task_wrapper(
    afunc: abc.Callable[..., Any] | None = None, *, critical: bool = False
) -> Any:
    def decorator(afunc: abc.Callable[..., Any]) -> abc.Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> None:
            try:
                await afunc(*args, **kwargs)
            except (ExitRequest, ReloadRequest):
                return
            except BaseException as exc:
                _handle_task_exception(exc, afunc.__name__, args, critical)
                raise
        wrapper.__name__ = afunc.__name__
        wrapper.__qualname__ = afunc.__qualname__
        return wrapper
    if afunc is None:
        return decorator
    return decorator(afunc)


def _serialize(obj: Any) -> Any:
    # convert data
    d: int | str | float | list[Any] | JsonType
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            # assume naive objects are UTC
            obj = obj.replace(tzinfo=timezone.utc)
        d = obj.timestamp()
    elif isinstance(obj, set):
        d = list(obj)
    elif isinstance(obj, Enum):
        # NOTE: IntEnum cannot be used, as it will get serialized as a plain integer,
        # then loaded back as an integer as well.
        d = obj.value
    elif isinstance(obj, URL):
        d = str(obj)
    else:
        raise TypeError(obj)
    # store with type
    return {
        "__type": type(obj).__name__,
        "data": d,
    }


_MISSING = object()


def _deserialize_set(data: Any) -> set[Any]:
    if not isinstance(data, list):
        raise ValueError("Serialized set data must be a list")
    try:
        return set(data)
    except TypeError as exc:
        raise ValueError("Serialized set contains an unhashable value") from exc


def _deserialize_url(data: Any) -> URL:
    if not isinstance(data, str):
        raise ValueError("Serialized URL data must be a string")
    return URL(data)


def _deserialize_priority_mode(data: Any) -> PriorityMode:
    if type(data) is not int:
        raise ValueError("Serialized priority mode must be an integer")
    return PriorityMode(data)


def _deserialize_datetime(data: Any) -> datetime:
    if isinstance(data, bool) or not isinstance(data, (int, float)):
        raise ValueError("Serialized datetime must be a finite timestamp")
    try:
        numeric_value = float(data)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Serialized datetime must be a finite timestamp") from exc
    if not isfinite(numeric_value):
        raise ValueError("Serialized datetime must be a finite timestamp")
    try:
        return datetime.fromtimestamp(numeric_value, timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("Serialized datetime is out of range") from exc


SERIALIZE_ENV: dict[str, Callable[[Any], object]] = {
    "set": _deserialize_set,
    "URL": _deserialize_url,
    "PriorityMode": _deserialize_priority_mode,
    "datetime": _deserialize_datetime,
}


def _remove_missing(obj: Any) -> Any:
    """Remove unknown serialized values recursively from mappings and lists."""
    if isinstance(obj, dict):
        for key, value in tuple(obj.items()):
            cleaned = _remove_missing(value)
            if cleaned is _MISSING or isinstance(cleaned, dict) and not cleaned:
                del obj[key]
            else:
                obj[key] = cleaned
    elif isinstance(obj, list):
        obj[:] = [
            cleaned
            for value in obj
            if (cleaned := _remove_missing(value)) is not _MISSING
        ]
    return obj


def _deserialize(obj: JsonType) -> Any:
    if "__type" not in obj:
        return obj
    obj_type = obj.get("__type")
    if not isinstance(obj_type, str):
        raise ValueError("Serialized type tag must be a string")
    decoder = SERIALIZE_ENV.get(obj_type)
    if decoder is None:
        return _MISSING
    if "data" not in obj:
        raise ValueError(f"Serialized {obj_type} value is missing data")
    try:
        return decoder(obj["data"])
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Serialized {obj_type} value is invalid") from exc


def _merge_mappings(
    primary: Mapping[Any, Any],
    secondary: Mapping[Any, Any],
    combine: Callable[[Any, Any], Any],
    *,
    keep_primary_only: bool,
) -> dict[Any, Any]:
    """Apply one recursive mapping policy to two JSON objects."""
    merged: dict[Any, Any] = {}
    for key in set(primary.keys()) | set(secondary.keys()):
        if key in primary and key in secondary:
            merged[key] = combine(primary[key], secondary[key])
        elif key in primary:
            if keep_primary_only:
                merged[key] = primary[key]
        else:
            merged[key] = secondary[key]
    return merged


def _default_value(value: Any, default: Any) -> Any:
    if type(value) is not type(default):
        return default
    if isinstance(value, dict):
        _apply_defaults(value, default)
    return value


def _apply_defaults(obj: JsonType, template: Mapping[Any, Any]) -> None:
    # NOTE: This modifies object in place
    merged = _merge_mappings(
        obj,
        template,
        _default_value,
        keep_primary_only=False,
    )
    obj.clear()
    obj.update(merged)


def merge_primary_json(primary: JsonType, secondary: JsonType) -> JsonType:
    """Merge API payloads while preserving primary values and progress."""
    return _merge_mappings(
        primary,
        secondary,
        _merge_primary_value,
        keep_primary_only=True,
    )


def _merge_primary_value(primary: Any, secondary: Any) -> Any:
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    if type(primary) is not type(secondary):
        raise TypeError("Inconsistent merge data")
    if isinstance(primary, dict):
        return merge_primary_json(primary, secondary)
    if isinstance(primary, list):
        return _merge_primary_lists(primary, secondary)
    return primary


def _merge_primary_lists(primary: list[Any], secondary: list[Any]) -> list[Any]:
    """Merge ID-keyed API lists without losing primary progress fields."""
    if not primary or not secondary:
        return secondary if not primary else primary
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in primary):
        return primary
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in secondary):
        return primary
    secondary_by_id = {item["id"]: item for item in secondary}
    merged: list[Any] = []
    for item in primary:
        detail = secondary_by_id.pop(item["id"], None)
        merged.append(merge_primary_json(item, detail) if detail is not None else item)
    merged.extend(secondary_by_id.values())
    return merged


def extract_available_drops(response: JsonType) -> list[JsonType]:
    """Return viewer drop campaigns from an AvailableDrops response."""
    try:
        channel_data = response["data"]["channel"]
    except (KeyError, TypeError):
        return []
    if not isinstance(channel_data, dict):
        return []
    campaigns = channel_data.get("viewerDropCampaigns") or []
    return campaigns if isinstance(campaigns, list) else []


def json_load(path: Path, defaults: _JSON_T, *, merge: bool = True) -> _JSON_T:
    combined: JsonType | None = None
    if path.exists():
        try:
            with path.open('r', encoding="utf8") as file:
                loaded = json.load(file, object_hook=_deserialize)
            if not isinstance(loaded, dict):
                raise ValueError("JSON root must be an object")
            combined = _remove_missing(loaded)
        except (
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
            OverflowError,
        ) as exc:
            raise ValueError(f"Unable to load JSON from {path}") from exc
    # handle defaults and merging
    defaults_copy: JsonType = deepcopy(dict(defaults))
    if combined is None:
        combined = defaults_copy
    elif merge:
        if not isinstance(combined, dict):
            raise ValueError(f"JSON root must be an object: {path}")
        _apply_defaults(combined, defaults_copy)
    return cast(_JSON_T, combined)


def json_save(path: Path, contents: Mapping[Any, Any], *, sort: bool = False) -> None:
    def writer(file: io.TextIOWrapper) -> None:
        json.dump(contents, file, default=_serialize, sort_keys=sort, indent=4)

    atomic_write(path, writer)


def _new_atomic_temp(
    path: Path,
    *,
    remove_legacy_new: bool = True,
) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if remove_legacy_new:
        with suppress(OSError):
            path.with_name(f"{path.name}.new").unlink()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    return descriptor, Path(temporary_name)


def _fsync_parent_directory(path: Path) -> None:
    """Durably commit a directory-entry replacement where supported."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_atomic_temp(path: Path, temporary_path: Path) -> None:
    os.replace(temporary_path, path)
    _fsync_parent_directory(path)


def atomic_write(
    path: Path,
    writer: Callable[[io.TextIOWrapper], None],
) -> None:
    """Atomically replace ``path`` using an exclusive, mode-0600 temp file."""
    descriptor, temporary_path = _new_atomic_temp(path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf8") as file:
            descriptor = -1
            writer(file)
            file.flush()
            os.fsync(file.fileno())
        _replace_atomic_temp(path, temporary_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary_path.unlink()


def atomic_write_path(path: Path, writer: Callable[[Path], None]) -> None:
    """Atomically replace ``path`` for a trusted API that requires a file path."""
    descriptor, temporary_path = _new_atomic_temp(path)
    os.close(descriptor)
    try:
        writer(temporary_path)
        file_status = temporary_path.lstat()
        if not stat.S_ISREG(file_status.st_mode):
            raise OSError("Atomic writer replaced its temporary file")
        temporary_path.chmod(0o600)
        with temporary_path.open("rb") as file:
            os.fsync(file.fileno())
        _replace_atomic_temp(path, temporary_path)
    finally:
        with suppress(OSError):
            temporary_path.unlink()


def atomic_write_bytes(
    path: Path,
    contents: bytes,
    *,
    remove_legacy_new: bool = True,
) -> None:
    """Durably replace ``path`` with private binary contents."""
    descriptor, temporary_path = _new_atomic_temp(
        path,
        remove_legacy_new=remove_legacy_new,
    )
    try:
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
        _replace_atomic_temp(path, temporary_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary_path.unlink()


def durable_unlink(path: Path, *, require_regular: bool = True) -> bool:
    """Unlink a path and fsync its parent without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if require_regular and not stat.S_ISREG(info.st_mode):
        raise OSError(f"Refusing to remove non-regular path: {path}")
    path.unlink()
    _fsync_parent_directory(path)
    return True


def quarantine_file(path: Path, *, reason: str = "invalid") -> Path | None:
    """Move a malformed regular file aside before a replacement is written."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Refusing to quarantine non-regular path: {path}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = path.with_name(f"{path.name}.{reason}-{timestamp}")
    os.replace(path, destination)
    _fsync_parent_directory(path)
    return destination


def remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def webopen(url: URL | str) -> None:
    parsed = URL(url)
    if parsed.scheme not in {"http", "https"} or parsed.host is None:
        raise ValueError("Only HTTP(S) browser URLs are allowed")
    url_str = str(parsed)
    if IS_PACKAGED and sys.platform == "linux":
        # https://pyinstaller.org/en/stable/
        # runtime-information.html#ld-library-path-libpath-considerations
        # NOTE: All 4 cases need to be handled here: either of the two values can be there or not.
        ld_env = "LD_LIBRARY_PATH"
        had_current = ld_env in os.environ
        ld_path_curr = os.environ.get(ld_env)
        ld_path_orig = os.environ.get(f"{ld_env}_ORIG")
        if ld_path_orig is not None:
            os.environ[ld_env] = ld_path_orig
        else:
            os.environ.pop(ld_env, None)

        try:
            webbrowser.open_new_tab(url_str)
        finally:
            if had_current and ld_path_curr is not None:
                os.environ[ld_env] = ld_path_curr
            else:
                os.environ.pop(ld_env, None)
    else:
        webbrowser.open_new_tab(url_str)


class ExponentialBackoff:
    def __init__(
        self,
        *,
        base: float = 2,
        variance: float | tuple[float, float] = 0.1,
        shift: float = 0,
        maximum: float = 300,
    ):
        try:
            self.base = float(base)
            self.shift = float(shift)
            self.maximum = float(maximum)
        except (TypeError, ValueError) as exc:
            raise ValueError("Backoff parameters must be numeric") from exc
        if self.base <= 1:
            raise ValueError("Base has to be greater than 1")
        self.steps: int = 0
        self.variance_min: float
        self.variance_max: float
        if isinstance(variance, tuple):
            self.variance_min, self.variance_max = variance
        else:
            self.variance_min = 1 - variance
            self.variance_max = 1 + variance

    def __iter__(self) -> abc.Iterator[float]:
        while True:
            yield next(self)

    def __next__(self) -> float:
        value: float = (
            pow(self.base, self.steps)
            * random.uniform(self.variance_min, self.variance_max)
            + self.shift
        )
        if value > self.maximum:
            return self.maximum
        # NOTE: variance can cause the returned value to be lower than the previous one already,
        # so this should be safe to move past the first return,
        # to prevent the exponent from getting very big after reaching max and many iterations
        self.steps += 1
        return value

    def reset(self) -> None:
        self.steps = 0


class RateLimiter:
    def __init__(self, *, capacity: int, window: int):
        self.total: int = 0
        self.concurrent: int = 0
        self.window: int = window
        self.capacity: int = capacity
        self._reset_task: asyncio.Task[None] | None = None
        self._cond: asyncio.Condition = asyncio.Condition()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.concurrent}/{self.total}/{self.capacity})"

    def __del__(self) -> None:
        if self._reset_task is not None:
            self._reset_task.cancel()

    def _can_proceed(self) -> bool:
        return max(self.total, self.concurrent) < self.capacity

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(self._can_proceed)
            self.total += 1
            self.concurrent += 1
            if self._reset_task is None:
                self._reset_task = asyncio.create_task(self._rtask())

    async def __aexit__(self, exc_type, exc, tb):
        self.concurrent -= 1
        async with self._cond:
            self._cond.notify(self.capacity - self.concurrent)

    async def _reset(self) -> None:
        if self._reset_task is not None:
            self._reset_task = None
        async with self._cond:
            self.total = 0
            if self.concurrent < self.capacity:
                self._cond.notify(self.capacity - self.concurrent)

    async def _rtask(self) -> None:
        await asyncio.sleep(self.window)
        await self._reset()


class AwaitableValue(Generic[_T]):
    def __init__(self):
        self._value: _T
        self._event = asyncio.Event()

    def wait(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._event.wait()

    def get_with_default(self, default: _D) -> _T | _D:
        if self._event.is_set():
            return self._value
        return default

    async def get(self) -> _T:
        await self._event.wait()
        return self._value

    def set(self, value: _T) -> None:
        self._value = value
        self._event.set()

    def clear(self) -> None:
        self._event.clear()
