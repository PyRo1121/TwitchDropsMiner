from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from build_tools.resolve_runtime_manifest import resolve_lock, write_manifest
from build_tools.validate_spdx_runtime import (
    SpdxRuntimeValidationError,
    manifest_package_names,
    validate_spdx_runtime,
)


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-release.txt"
_TARGETS = {
    "linux": {
        "overrides": {
            "implementation_name": "cpython",
            "implementation_version": "3.10.20",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_system": "Linux",
            "python_full_version": "3.10.20",
            "python_version": "3.10",
            "sys_platform": "linux",
        },
        "count": 35,
        "required": {"keyring", "secretstorage", "cffi", "cryptography", "jeepney", "pycparser"},
        "forbidden": {"pywin32-ctypes"},
    },
    "windows": {
        "overrides": {
            "implementation_name": "cpython",
            "implementation_version": "3.10.20",
            "os_name": "nt",
            "platform_machine": "AMD64",
            "platform_python_implementation": "CPython",
            "platform_system": "Windows",
            "python_full_version": "3.10.20",
            "python_version": "3.10",
            "sys_platform": "win32",
        },
        "count": 31,
        "required": {"keyring", "pywin32-ctypes"},
        "forbidden": {"secretstorage", "cffi", "cryptography", "jeepney", "pycparser"},
    },
    "macos": {
        "overrides": {
            "implementation_name": "cpython",
            "implementation_version": "3.10.20",
            "os_name": "posix",
            "platform_machine": "arm64",
            "platform_python_implementation": "CPython",
            "platform_system": "Darwin",
            "python_full_version": "3.10.20",
            "python_version": "3.10",
            "sys_platform": "darwin",
        },
        "count": 30,
        "required": {"keyring"},
        "forbidden": {"secretstorage", "cffi", "cryptography", "jeepney", "pycparser", "pywin32-ctypes"},
    },
}
_BUILD_ONLY = {
    "altgraph",
    "macholib",
    "pefile",
    "pip",
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "setuptools",
    "setuptools-scm",
    "wheel",
}


def _write_spdx(path: Path, package_names: set[str]) -> None:
    document = {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "test-runtime",
        "packages": [
            {"SPDXID": f"SPDXRef-{index}", "name": name}
            for index, name in enumerate(sorted(package_names), start=1)
        ],
    }
    path.write_text(json.dumps(document), encoding="utf8")


class RuntimeSbomToolTests(unittest.TestCase):
    def _resolved_manifest(
        self,
        directory: Path,
        platform: str,
    ) -> tuple[Path, set[str]]:
        manifest = directory / f"requirements-{platform}.txt"
        write_manifest(LOCK, manifest, _TARGETS[platform]["overrides"])
        return manifest, manifest_package_names(manifest)

    def test_resolver_evaluates_markers_and_keeps_only_runtime_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for platform, case in _TARGETS.items():
                with self.subTest(platform=platform):
                    manifest, names = self._resolved_manifest(root, platform)
                    text = manifest.read_text(encoding="utf8")
                    self.assertEqual(len(names), case["count"])
                    self.assertTrue(case["required"].issubset(names))
                    self.assertTrue(case["forbidden"].isdisjoint(names))
                    self.assertTrue(_BUILD_ONLY.isdisjoint(names))
                    self.assertNotIn("sys_platform ==", text)
                    self.assertNotIn("python_version <", text)
                    self.assertEqual(text.count("=="), len(names))
                    _environment, active = resolve_lock(LOCK, case["overrides"])
                    expected_hashes = [
                        digest
                        for requirement in active
                        for digest in requirement.hashes
                    ]
                    actual_hashes = re.findall(
                        r"--hash=sha256:([0-9a-f]{64})",
                        text,
                    )
                    self.assertCountEqual(actual_hashes, expected_hashes)

    def test_validator_accepts_each_target_resolved_spdx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for platform in _TARGETS:
                with self.subTest(platform=platform):
                    manifest, names = self._resolved_manifest(root, platform)
                    spdx = root / f"{platform}.spdx.json"
                    _write_spdx(spdx, names)
                    manifest_names, spdx_names = validate_spdx_runtime(
                        spdx,
                        manifest,
                        platform,
                    )
                    self.assertEqual(manifest_names, names)
                    self.assertEqual(spdx_names, names)

    def test_validator_rejects_inactive_platform_graphs_in_actual_spdx(self) -> None:
        inactive_cases = {
            "linux": ("pywin32-ctypes",),
            "windows": ("secretstorage", "cryptography"),
            "macos": ("secretstorage", "pywin32-ctypes"),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for platform, inactive_packages in inactive_cases.items():
                manifest, names = self._resolved_manifest(root, platform)
                for inactive in inactive_packages:
                    with self.subTest(platform=platform, inactive=inactive):
                        spdx = root / f"{platform}-{inactive}.spdx.json"
                        _write_spdx(spdx, names | {inactive})
                        with self.assertRaisesRegex(
                            SpdxRuntimeValidationError,
                            "inactive platform keyring packages",
                        ):
                            validate_spdx_runtime(spdx, manifest, platform)

    def test_validator_rejects_missing_active_keyring_graph(self) -> None:
        missing_cases = {
            "linux": "secretstorage",
            "windows": "pywin32-ctypes",
            "macos": "keyring",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for platform, missing in missing_cases.items():
                with self.subTest(platform=platform, missing=missing):
                    manifest, names = self._resolved_manifest(root, platform)
                    spdx = root / f"{platform}-missing.spdx.json"
                    _write_spdx(spdx, names - {missing})
                    with self.assertRaisesRegex(
                        SpdxRuntimeValidationError,
                        "missing target runtime packages",
                    ):
                        validate_spdx_runtime(spdx, manifest, platform)

    def test_validator_rejects_build_packages_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest, names = self._resolved_manifest(root, "linux")
            with manifest.open("a", encoding="utf8") as output:
                output.write(
                    "pyinstaller==6.21.0 \\\n"
                    "    --hash=sha256:"
                    f"{'0' * 64}\n"
                )
            spdx = root / "build-package.spdx.json"
            _write_spdx(spdx, names | {"pyinstaller"})
            with self.assertRaisesRegex(
                SpdxRuntimeValidationError,
                "bootstrap/build packages",
            ):
                validate_spdx_runtime(spdx, manifest, "linux")


if __name__ == "__main__":
    unittest.main()
