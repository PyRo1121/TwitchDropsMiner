#!/usr/bin/env python3
"""Validate a Syft SPDX JSON document against its resolved runtime manifest."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from packaging.utils import canonicalize_name


_COMMON_KEYRING = frozenset(
    canonicalize_name(name)
    for name in (
        "backports.tarfile",
        "importlib_metadata",
        "jaraco.classes",
        "jaraco.context",
        "jaraco.functools",
        "keyring",
        "more-itertools",
        "zipp",
    )
)
_LINUX_KEYRING = frozenset(
    canonicalize_name(name)
    for name in (
        "cffi",
        "cryptography",
        "jeepney",
        "pycparser",
        "SecretStorage",
    )
)
_WINDOWS_KEYRING = frozenset({canonicalize_name("pywin32-ctypes")})
_BUILD_ONLY = frozenset(
    canonicalize_name(name)
    for name in (
        "altgraph",
        "macholib",
        "pefile",
        "pip",
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "setuptools",
        "setuptools-scm",
        "wheel",
    )
)
_PIN = re.compile(r"([A-Za-z0-9_.-]+)==[^;\s]+")


class SpdxRuntimeValidationError(ValueError):
    pass


def current_platform() -> str:
    if sys.platform == "linux":
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    raise SpdxRuntimeValidationError(f"unsupported release platform: {sys.platform}")


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
        raise SpdxRuntimeValidationError(
            f"{path} ends with an incomplete requirement"
        )
    return logical


def manifest_package_names(path: Path) -> set[str]:
    names: set[str] = set()
    for logical in _logical_requirements(path):
        requirement_text = logical.split(" --hash=", 1)[0].strip()
        match = _PIN.fullmatch(requirement_text)
        if match is None:
            raise SpdxRuntimeValidationError(
                f"resolved manifest contains a non-exact or marked entry: {requirement_text}"
            )
        if "--hash=sha256:" not in logical:
            raise SpdxRuntimeValidationError(
                f"resolved manifest contains an unhashed entry: {requirement_text}"
            )
        name = canonicalize_name(match.group(1))
        if name in names:
            raise SpdxRuntimeValidationError(
                f"resolved manifest contains duplicate package: {match.group(1)}"
            )
        names.add(name)
    if not names:
        raise SpdxRuntimeValidationError(f"resolved manifest is empty: {path}")
    return names


def spdx_package_names(document: Mapping[str, Any]) -> set[str]:
    if not str(document.get("spdxVersion", "")).startswith("SPDX-2."):
        raise SpdxRuntimeValidationError("SBOM is not an SPDX 2.x JSON document")
    packages = document.get("packages")
    if not isinstance(packages, list):
        raise SpdxRuntimeValidationError("SPDX document has no packages array")
    names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise SpdxRuntimeValidationError("SPDX packages array contains a non-object")
        name = package.get("name")
        if isinstance(name, str) and name:
            names.add(canonicalize_name(name))
    if not names:
        raise SpdxRuntimeValidationError("SPDX document contains no named packages")
    return names


def platform_policy(platform: str) -> tuple[frozenset[str], frozenset[str]]:
    if platform == "linux":
        return _COMMON_KEYRING | _LINUX_KEYRING, _WINDOWS_KEYRING
    if platform == "windows":
        return _COMMON_KEYRING | _WINDOWS_KEYRING, _LINUX_KEYRING
    if platform == "macos":
        return _COMMON_KEYRING, _LINUX_KEYRING | _WINDOWS_KEYRING
    raise SpdxRuntimeValidationError(f"unsupported release platform: {platform}")


def _display(names: Iterable[str]) -> str:
    return ", ".join(sorted(names))


def validate_spdx_runtime(
    spdx_path: Path,
    manifest_path: Path,
    platform: str | None = None,
) -> tuple[set[str], set[str]]:
    target_platform = current_platform() if platform is None else platform
    required_keyring, forbidden_keyring = platform_policy(target_platform)
    manifest_names = manifest_package_names(manifest_path)

    unexpected_build = manifest_names & _BUILD_ONLY
    if unexpected_build:
        raise SpdxRuntimeValidationError(
            "resolved runtime manifest contains bootstrap/build packages: "
            f"{_display(unexpected_build)}"
        )
    missing_manifest_keyring = required_keyring - manifest_names
    if missing_manifest_keyring:
        raise SpdxRuntimeValidationError(
            "resolved runtime manifest is missing active keyring packages: "
            f"{_display(missing_manifest_keyring)}"
        )
    inactive_manifest_keyring = forbidden_keyring & manifest_names
    if inactive_manifest_keyring:
        raise SpdxRuntimeValidationError(
            "resolved runtime manifest contains inactive keyring packages: "
            f"{_display(inactive_manifest_keyring)}"
        )

    try:
        loaded: Any = json.loads(spdx_path.read_text(encoding="utf8"))
    except json.JSONDecodeError as error:
        raise SpdxRuntimeValidationError(f"invalid SPDX JSON: {error}") from error
    if not isinstance(loaded, dict):
        raise SpdxRuntimeValidationError("SPDX JSON root is not an object")
    document = loaded
    spdx_names = spdx_package_names(document)

    missing_runtime = manifest_names - spdx_names
    if missing_runtime:
        raise SpdxRuntimeValidationError(
            f"SPDX is missing target runtime packages: {_display(missing_runtime)}"
        )
    inactive_spdx_keyring = forbidden_keyring & spdx_names
    if inactive_spdx_keyring:
        raise SpdxRuntimeValidationError(
            "SPDX contains inactive platform keyring packages: "
            f"{_display(inactive_spdx_keyring)}"
        )
    missing_spdx_keyring = required_keyring - spdx_names
    if missing_spdx_keyring:
        raise SpdxRuntimeValidationError(
            "SPDX is missing active keyring packages: "
            f"{_display(missing_spdx_keyring)}"
        )
    return manifest_names, spdx_names


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spdx", required=True, type=Path, help="Syft SPDX JSON output")
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="target-resolved runtime requirements used as Syft input",
    )
    return parser


def main() -> None:
    arguments = _argument_parser().parse_args()
    try:
        manifest_names, spdx_names = validate_spdx_runtime(
            arguments.spdx,
            arguments.manifest,
        )
    except (OSError, SpdxRuntimeValidationError) as error:
        raise SystemExit(f"SPDX runtime validation failed: {error}") from error
    print(
        f"Validated {len(manifest_names)} target runtime packages "
        f"against {len(spdx_names)} SPDX packages"
    )


if __name__ == "__main__":
    main()
