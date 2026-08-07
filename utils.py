from __future__ import annotations

import io
import os
import re
import sys
import json
import random
import unicodedata
import string
import asyncio
import logging
import traceback
import webbrowser
from copy import deepcopy
from enum import Enum
from pathlib import Path
from contextlib import suppress
from datetime import datetime, timezone
from collections import abc
from typing import Any, Literal, Callable, Generic, Mapping, TypeVar, ParamSpec, cast

from yarl import URL

from exceptions import ExitRequest, ReloadRequest
from constants import IS_PACKAGED, JsonType, PriorityMode


_T = TypeVar("_T")  # type
_D = TypeVar("_D")  # default
_P = ParamSpec("_P")  # params
_JSON_T = TypeVar("_JSON_T", bound=Mapping[Any, Any])
logger = logging.getLogger("TwitchDrops")

# Matches an RFC3339 fractional-seconds group (e.g. ".123456789") so the
# microseconds portion can be capped for Python 3.10's fromisoformat.
_FRACTION_RE = re.compile(r"\.(\d+)")


async def cancel_tasks(tasks: abc.Iterable[asyncio.Task[Any]]) -> None:
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
    Any additional `**kwargs` are passed to the underlaying `traceback.format_exception`.
    """
    return ''.join(traceback.format_exception(type(exc), exc, **kwargs))


def lock_file(path: Path) -> tuple[bool, io.TextIOWrapper]:
    file = path.open('w', encoding="utf8")
    file.write('ツ')
    file.flush()
    if sys.platform == "win32":
        import msvcrt
        try:
            # we need to lock at least one byte for this to work
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, max(path.stat().st_size, 1))
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


def redact_log_value(value: Any, *, key: Any = None) -> Any:
    """Return a log-safe copy of a value without changing the original."""
    normalised_key = _normalise_log_key(key) if key is not None else None
    if key is not None and _is_sensitive_log_key(key):
        return _LOG_REDACTED
    if normalised_key in {"data", "json"} and not isinstance(
        value, (abc.Mapping, list, tuple, set)
    ):
        return _LOG_REDACTED
    if isinstance(value, URL):
        return _redact_log_url(value)
    if isinstance(value, abc.Mapping):
        return {
            item_key: redact_log_value(item, key=item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_log_value(item) for item in value)
    if isinstance(value, set):
        return {redact_log_value(item) for item in value}
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
    except ValueError as exc:
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
    return ''.join(random.choices(chars, k=length))


def safe_int(value: Any) -> int | None:
    """Parse an int, returning None on missing or non-integer input."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def require_int(value: Any, message: str) -> int:
    """Parse an int or raise ``ValueError`` with the caller's context."""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc


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
                pass
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
SERIALIZE_ENV: dict[str, Callable[[Any], object]] = {
    "set": set,
    "URL": URL,
    "PriorityMode": PriorityMode,
    "datetime": lambda d: datetime.fromtimestamp(d, timezone.utc),
}


def _remove_missing(obj: JsonType) -> JsonType:
    # this modifies obj in place, but we return it just in case
    for key, value in obj.copy().items():
        if value is _MISSING:
            del obj[key]
        elif isinstance(value, dict):
            _remove_missing(value)
            if not value:
                # the dict is empty now, so remove it's key entirely
                del obj[key]
    return obj


def _deserialize(obj: JsonType) -> Any:
    if "__type" in obj:
        obj_type = obj["__type"]
        if obj_type in SERIALIZE_ENV:
            return SERIALIZE_ENV[obj_type](obj["data"])
        else:
            return _MISSING
    return obj


def _merge_json(obj: JsonType, template: Mapping[Any, Any]) -> None:
    # NOTE: This modifies object in place
    for k, v in list(obj.items()):
        if k not in template:
            # unknown key: overwrite from template
            del obj[k]
        elif type(v) is not type(template[k]):
            # types don't match: overwrite from template
            obj[k] = template[k]
        elif isinstance(v, dict):
            if not isinstance(template[k], dict):
                raise TypeError(f"Template value for {k!r} must be a mapping")
            _merge_json(v, template[k])
    # ensure the object is not missing any keys
    for k in template.keys():
        if k not in obj:
            obj[k] = template[k]


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
    new_path: Path = path.with_name(f"{path.name}.new")
    combined: JsonType | None = None
    # try new file first
    if new_path.exists():
        try:
            with new_path.open('r', encoding="utf8") as file:
                loaded = json.load(file, object_hook=_deserialize)
            if not isinstance(loaded, dict):
                raise ValueError("JSON root must be an object")
            combined = _remove_missing(loaded)
        except (OSError, UnicodeError, ValueError):
            # remove invalid file
            new_path.unlink(missing_ok=True)
    # try the old file
    if combined is None and path.exists():
        try:
            with path.open('r', encoding="utf8") as file:
                loaded = json.load(file, object_hook=_deserialize)
            if not isinstance(loaded, dict):
                raise ValueError("JSON root must be an object")
            combined = _remove_missing(loaded)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"Unable to load JSON from {path}") from exc
    # handle defaults and merging
    defaults_copy: JsonType = deepcopy(dict(defaults))
    if combined is None:
        combined = defaults_copy
    elif merge:
        if not isinstance(combined, dict):
            raise ValueError(f"JSON root must be an object: {path}")
        _merge_json(combined, defaults_copy)
    return cast(_JSON_T, combined)


def json_save(path: Path, contents: Mapping[Any, Any], *, sort: bool = False) -> None:
    def writer(new_path: Path) -> None:
        with new_path.open("w", encoding="utf8") as file:
            json.dump(contents, file, default=_serialize, sort_keys=sort, indent=4)

    atomic_write(path, writer, mode=None)


def atomic_write(
    path: Path, writer: Callable[[Path], None], *, mode: int | None = 0o600
) -> None:
    """Write `path` atomically via a `<path>.new` sibling, then `replace()` over it.

    Guards against symlink substitution on the temp path, optionally applies ``mode``
    to both the temp and final file, and removes the temp even on failure so the last
    good file survives.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.new")
    try:
        if temporary_path.is_symlink():
            raise OSError(f"Temporary path is a symlink: {temporary_path}")
        writer(temporary_path)
        if mode is not None:
            temporary_path.chmod(mode)
        temporary_path.replace(path)
        if mode is not None:
            path.chmod(mode)
    finally:
        with suppress(OSError):
            temporary_path.unlink()


def remove_stale_new(path: Path) -> None:
    """Remove both ``path`` and any stale ``<path>.new`` sibling."""
    path.unlink(missing_ok=True)
    path.with_name(f"{path.name}.new").unlink(missing_ok=True)


def webopen(url: URL | str):
    url_str = str(url)
    if IS_PACKAGED and sys.platform == "linux":
        # https://pyinstaller.org/en/stable/
        # runtime-information.html#ld-library-path-libpath-considerations
        # NOTE: All 4 cases need to be handled here: either of the two values can be there or not.
        ld_env = "LD_LIBRARY_PATH"
        ld_path_curr = os.environ.get(ld_env)
        ld_path_orig = os.environ.get(f"{ld_env}_ORIG")
        if ld_path_orig is not None:
            os.environ[ld_env] = ld_path_orig
        elif ld_path_curr is not None:
            # pop current
            os.environ.pop(ld_env)

        webbrowser.open_new_tab(url_str)

        if ld_path_curr is not None:
            os.environ[ld_env] = ld_path_curr
        elif ld_path_orig is not None:
            # pop original
            os.environ.pop(ld_env)
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
        return self

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
