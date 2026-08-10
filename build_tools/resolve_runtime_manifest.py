#!/usr/bin/env python3
"""Emit the hash-locked runtime requirements active on the current platform."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, cast

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement


_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")
_SUPPORTED_PLATFORMS = {"darwin", "linux", "win32"}


@dataclass(frozen=True)
class LockedRequirement:
    requirement: Requirement
    version: str
    hashes: tuple[str, ...]


def _logical_requirements(path: Path) -> list[str]:
    logical: list[str] = []
    pending = ""
    for raw_line in path.read_text(encoding="utf8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    if pending:
        raise ValueError(f"{path} ends with an incomplete requirement")
    return logical


def parse_lock(path: Path) -> list[LockedRequirement]:
    locked: list[LockedRequirement] = []
    seen: set[str] = set()
    for logical in _logical_requirements(path):
        if logical.startswith("-"):
            raise ValueError(f"runtime lock cannot include another file: {logical}")
        requirement_text = logical.split(" --hash=", 1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as error:
            raise ValueError(f"invalid locked requirement: {requirement_text}") from error
        if requirement.url is not None or requirement.extras:
            raise ValueError(f"runtime lock entry must be a plain exact pin: {requirement}")
        specifiers = list(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ValueError(f"runtime lock entry is not exactly pinned: {requirement}")
        hashes = tuple(_HASH.findall(f"{logical} "))
        if not hashes:
            raise ValueError(f"runtime lock entry has no SHA-256 hash: {requirement}")
        normalized_name = re.sub(r"[-_.]+", "-", requirement.name).lower()
        if normalized_name in seen:
            raise ValueError(f"runtime lock contains duplicate package: {requirement.name}")
        seen.add(normalized_name)
        locked.append(LockedRequirement(requirement, specifiers[0].version, hashes))
    if not locked:
        raise ValueError(f"runtime lock is empty: {path}")
    return locked


def release_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment: dict[str, str] = {
        key: cast(str, value) for key, value in default_environment().items()
    }
    environment["extra"] = ""
    if overrides is not None:
        for key, value in overrides.items():
            environment[key] = value
    if environment["implementation_name"] != "cpython":
        raise ValueError("release runtime manifests require CPython")
    if environment["python_version"] != "3.10":
        raise ValueError("release runtime manifests require CPython 3.10")
    if environment["sys_platform"] not in _SUPPORTED_PLATFORMS:
        raise ValueError(
            f"unsupported release platform: {environment['sys_platform']}"
        )
    return environment


def resolve_lock(
    path: Path,
    environment: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[LockedRequirement]]:
    resolved_environment = release_environment(environment)
    active = [
        locked
        for locked in parse_lock(path)
        if locked.requirement.marker is None
        or locked.requirement.marker.evaluate(resolved_environment)
    ]
    return resolved_environment, active


def render_manifest(
    environment: Mapping[str, str],
    requirements: Sequence[LockedRequirement],
) -> str:
    lines = [
        "# Generated from requirements-release.txt; do not edit.",
        "# PEP 508 environment: "
        f"sys_platform={environment['sys_platform']}, "
        f"python_full_version={environment['python_full_version']}, "
        f"platform_machine={environment['platform_machine']}",
        "",
    ]
    for locked in requirements:
        lines.append(f"{locked.requirement.name}=={locked.version} \\")
        for index, digest in enumerate(locked.hashes):
            continuation = " \\" if index + 1 < len(locked.hashes) else ""
            lines.append(f"    --hash=sha256:{digest}{continuation}")
    return "\n".join(lines) + "\n"


def write_manifest(
    lock_path: Path,
    output_path: Path,
    environment: Mapping[str, str] | None = None,
) -> list[LockedRequirement]:
    resolved_environment, active = resolve_lock(lock_path, environment)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        dir=output_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf8", newline="\n") as output:
            output.write(render_manifest(resolved_environment, active))
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return active


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="union runtime lock")
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="target-resolved requirements manifest",
    )
    return parser


def main() -> None:
    arguments = _argument_parser().parse_args()
    try:
        active = write_manifest(arguments.input, arguments.output)
    except (OSError, ValueError) as error:
        raise SystemExit(f"runtime manifest generation failed: {error}") from error
    print(f"Wrote {len(active)} active runtime packages to {arguments.output}")


if __name__ == "__main__":
    main()
