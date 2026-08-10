#!/usr/bin/env python3
"""Create a normalized release ZIP while preserving bundle symlinks and modes."""

from __future__ import annotations

import argparse
import os
import posixpath
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterator, Sequence


@dataclass(frozen=True)
class ArchiveEntry:
    source: Path
    archive_name: PurePosixPath


def _normalized_name(value: str) -> PurePosixPath:
    name = PurePosixPath(value.replace("\\", "/"))
    if not value or name.is_absolute() or ".." in name.parts or name == PurePosixPath("."):
        raise ValueError(f"unsafe archive path: {value!r}")
    return name


def parse_entry(value: str) -> ArchiveEntry:
    try:
        source_value, archive_value = value.split("=", 1)
    except ValueError as error:
        raise ValueError("entries must use SOURCE=ARCHIVE_PATH syntax") from error
    source = Path(source_value)
    if not os.path.lexists(source):
        raise FileNotFoundError(source)
    return ArchiveEntry(source, _normalized_name(archive_value))


def _walk(entry: ArchiveEntry) -> Iterator[ArchiveEntry]:
    yield entry
    if entry.source.is_symlink() or not entry.source.is_dir():
        return
    for child in sorted(entry.source.iterdir(), key=lambda path: path.name):
        for descendant in _walk(ArchiveEntry(child, entry.archive_name / child.name)):
            yield descendant


def _zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    # ZIP timestamps start in 1980 and have a two-second resolution.
    bounded_epoch = max(epoch, 315_532_800)
    value = datetime.fromtimestamp(bounded_epoch, tz=timezone.utc)
    if value.year > 2107:
        raise ValueError("SOURCE_DATE_EPOCH is outside the ZIP timestamp range")
    return value.year, value.month, value.day, value.hour, value.minute, value.second // 2 * 2


def _mode_for(source: Path) -> int:
    source_mode = source.lstat().st_mode
    if stat.S_ISLNK(source_mode):
        return stat.S_IFLNK | 0o777
    if stat.S_ISDIR(source_mode):
        return stat.S_IFDIR | 0o755
    permissions = 0o755 if source_mode & 0o111 or source.suffix.lower() in {".appimage", ".exe"} else 0o644
    return stat.S_IFREG | permissions


def _validated_symlink_target(item: ArchiveEntry, archive_root: PurePosixPath) -> str:
    target = os.readlink(item.source)
    portable_target = target.replace("\\", "/")
    if (
        not target
        or PurePosixPath(portable_target).is_absolute()
        or PureWindowsPath(target).drive
    ):
        raise ValueError(
            f"unsafe symlink target for {item.archive_name}: {target!r}"
        )
    resolved = PurePosixPath(
        posixpath.normpath(
            f"{item.archive_name.parent.as_posix()}/{portable_target}"
        )
    )
    if resolved != archive_root and archive_root not in resolved.parents:
        raise ValueError(
            f"symlink target escapes archive root for {item.archive_name}: {target!r}"
        )
    return target


def create_archive(output: Path, entries: Sequence[ArchiveEntry], epoch: int) -> None:
    if not entries:
        raise ValueError("at least one archive entry is required")

    expanded_with_roots = sorted(
        (
            (item, entry.archive_name)
            for entry in entries
            for item in _walk(entry)
        ),
        key=lambda value: value[0].archive_name.as_posix(),
    )
    expanded = [item for item, _archive_root in expanded_with_roots]
    seen: set[str] = set()
    symlink_targets: dict[PurePosixPath, str] = {}
    for item, archive_root in expanded_with_roots:
        folded_name = item.archive_name.as_posix().casefold()
        if folded_name in seen:
            raise ValueError(f"duplicate archive path: {item.archive_name}")
        seen.add(folded_name)
        if item.source.is_symlink():
            symlink_targets[item.archive_name] = _validated_symlink_target(
                item,
                archive_root,
            )

    symlink_names = {
        archive_name.as_posix().casefold() for archive_name in symlink_targets
    }
    for item in expanded:
        if any(
            parent != PurePosixPath(".")
            and parent.as_posix().casefold() in symlink_names
            for parent in item.archive_name.parents
        ):
            raise ValueError(
                f"archive path has a symlink ancestor: {item.archive_name}"
            )

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for item in expanded:
                source = item.source
                target = symlink_targets.get(item.archive_name)
                if target is not None:
                    if not source.is_symlink() or os.readlink(source) != target:
                        raise ValueError(
                            f"symlink changed while packaging: {item.archive_name}"
                        )
                elif source.is_symlink():
                    raise ValueError(
                        f"source became a symlink while packaging: {item.archive_name}"
                    )
                is_directory = target is None and source.is_dir()
                archive_name = item.archive_name.as_posix() + ("/" if is_directory else "")
                info = zipfile.ZipInfo(archive_name, _zip_datetime(epoch))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = _mode_for(source) << 16
                if is_directory:
                    info.external_attr |= 0x10
                    archive.writestr(info, b"")
                elif target is not None:
                    archive.writestr(info, target.encode("utf8"))
                else:
                    with source.open("rb") as input_file, archive.open(info, "w") as output_file:
                        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--epoch",
        type=int,
        help="normalized ZIP timestamp (defaults to SOURCE_DATE_EPOCH)",
    )
    parser.add_argument(
        "--entry",
        action="append",
        required=True,
        type=parse_entry,
        help="input and archive path as SOURCE=ARCHIVE_PATH; repeat as needed",
    )
    return parser


def main() -> None:
    parser = _argument_parser()
    arguments = parser.parse_args()
    if arguments.epoch is None:
        try:
            arguments.epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        except ValueError:
            parser.error("SOURCE_DATE_EPOCH must be an integer")
    create_archive(arguments.output, arguments.entry, arguments.epoch)


if __name__ == "__main__":
    main()
