from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")
PIN = re.compile(r"([A-Za-z0-9_.-]+)==([^;\s]+)")


def _logical_requirements(name: str) -> list[str]:
    logical: list[str] = []
    pending = ""
    for raw_line in (ROOT / name).read_text(encoding="utf8").splitlines():
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
        raise AssertionError(f"{name} ends with an incomplete requirement")
    return logical


def _pins(name: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for requirement in _logical_requirements(name):
        if requirement.startswith("-r "):
            continue
        match = PIN.match(requirement)
        if match:
            package, version = match.groups()
            pins[re.sub(r"[-_.]+", "-", package).lower()] = version
    return pins


class BuildConfigurationTests(unittest.TestCase):
    def test_release_dependency_files_are_hash_locked(self) -> None:
        for name in (
            "requirements-bootstrap.txt",
            "requirements-build.txt",
            "requirements-appimage.txt",
            "requirements-appimage-source.txt",
            "requirements-release.txt",
        ):
            with self.subTest(file=name):
                requirements = _logical_requirements(name)
                self.assertTrue(requirements)
                for requirement in requirements:
                    if requirement.startswith("-r "):
                        continue
                    pin = requirement.split(" --hash=", 1)[0].strip()
                    exact_version = re.fullmatch(
                        r"[A-Za-z0-9_.-]+==[^=<>!~\s;]+(?:;\s*.+)?",
                        pin,
                    )
                    pinned_archive = re.fullmatch(
                        r"appimage-builder\s+@\s+https://github\.com/"
                        r"AppImageCrafters/appimage-builder/archive/[0-9a-f]{40}\.tar\.gz",
                        pin,
                    )
                    self.assertTrue(
                        exact_version or pinned_archive,
                        f"{name} contains an unlocked requirement: {requirement}",
                    )
                    self.assertGreaterEqual(
                        len(HASH.findall(requirement)),
                        1,
                        f"{name} contains an unhashed requirement: {requirement}",
                    )
                    self.assertNotIn("git+", requirement)

        self.assertIn("-r requirements-release.txt", _logical_requirements("requirements-build.txt"))
        runtime_pins = _pins("requirements.txt")
        release_pins = _pins("requirements-release.txt")
        self.assertEqual(runtime_pins, {name: release_pins[name] for name in runtime_pins})
        self.assertEqual(
            set(release_pins) - set(runtime_pins),
            {"async-timeout", "tomli"},
        )
        self.assertEqual(release_pins["tomli"], "2.4.1")
        release_lock = (ROOT / "requirements-release.txt").read_text(
            encoding="utf8"
        )
        self.assertIn('tomli==2.4.1; python_version < "3.11"', release_lock)
        self.assertIn(
            "sha256:0d85819802132122da43cb86656f8d1f8c6587d54ae7dcaf30e90533028b49fe",
            release_lock,
        )

    def test_ci_uses_hash_locks_and_real_frozen_self_tests(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf8")

        self.assertIn("concurrency:", workflow)
        self.assertGreaterEqual(workflow.count("requirements-build.txt"), 3)
        self.assertGreaterEqual(workflow.count("requirements-bootstrap.txt"), 4)
        self.assertIn("requirements-appimage.txt", workflow)
        self.assertGreaterEqual(workflow.count("--require-hashes"), 8)
        self.assertGreaterEqual(workflow.count("--only-binary=:all:"), 7)
        self.assertGreaterEqual(workflow.count("--self-test"), 4)
        self.assertEqual(workflow.count("build_tools/package_release.py"), 4)
        self.assertIn("pyright@1.1.409", workflow)
        self.assertIn("python -m venv .release-lock-check", workflow)
        self.assertIn("--dry-run --ignore-installed", workflow)
        self.assertIn("Resolve release lock metadata in clean CPython 3.10", workflow)
        self.assertIn("python -m venv .venv", workflow)
        self.assertIn('${PWD}/.venv/bin', workflow)
        self.assertIn('PYTHONHASHSEED: "0"', workflow)
        self.assertIn("SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)", workflow)
        self.assertEqual(workflow.count("git rev-parse --short=12 HEAD"), 4)
        pyright_config = (ROOT / "pyrightconfig.json").read_text(encoding="utf8")
        self.assertIn('"pythonVersion": "3.10"', pyright_config)
        self.assertIn("tests.test_translation_schema", workflow)
        self.assertIn("github.repository == 'PyRo1121/TwitchDropsMiner'", workflow)
        self.assertIn("github.ref == 'refs/heads/master'", workflow)
        self.assertIn('gh release create "dev-build-${GITHUB_SHA}"', workflow)
        self.assertIn("publish_private_runtime:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("inputs.publish_private_runtime == true", workflow)
        self.assertIn(
            "vars.ENABLE_PRIVATE_RUNTIME_PUBLICATION == 'true'",
            workflow,
        )
        self.assertIn("name: private-runtime-publication", workflow)
        self.assertIn("APPROVE_PRIVATE_RUNTIME_PUBLICATION", workflow)
        self.assertNotIn("github.event_name != 'pull_request'", workflow)
        self.assertEqual(workflow.count("gh release create"), 1)
        self.assertEqual(workflow.count("contents: write"), 1)
        publication = workflow.split("\n  publish_release:\n", 1)[1]
        self.assertIn("workflow_dispatch", publication)
        self.assertIn("private-runtime-publication", publication)
        self.assertIn("gh release create", publication)
        self.assertIn("SHA256SUMS", workflow)
        self.assertNotIn("gh release delete", workflow)
        self.assertNotIn("Compress-Archive", workflow)
        self.assertNotIn("7z a", workflow)
        self.assertNotIn("Install UPX", workflow)
        self.assertIn("macos-15-intel", workflow)
        self.assertIn("-macOS-${{matrix.arch}}", workflow)
        self.assertIn("-Windows-x86_64", workflow)
        self.assertNotIn("runs-on: macos-latest", workflow)

    def test_release_has_sbom_provenance_and_least_privilege(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf8")
        codeql = (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf8")
        dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf8")

        self.assertIn("attestations: write", workflow)
        self.assertIn("artifact-metadata: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertEqual(
            workflow.count(
                "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6"
            ),
            8,
        )
        self.assertEqual(
            workflow.count(
                "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610"
            ),
            4,
        )
        self.assertEqual(workflow.count("sbom-path:"), 7)
        self.assertEqual(workflow.count("subject-path:"), 8)
        self.assertEqual(workflow.count("path: sbom-input"), 4)
        self.assertEqual(
            workflow.count(
                "output-file: ${{steps.vars.outputs.artifact_name}}.spdx.json"
            ),
            4,
        )
        self.assertNotIn("Twitch.Drops.Miner.spdx.json", workflow)
        self.assertNotIn("cp requirements*.txt", workflow)
        self.assertNotIn("sbom-input/locks", workflow)
        self.assertIn("Copy-Item requirements-release.txt", workflow)
        self.assertEqual(
            workflow.count(
                "cp requirements-release.txt sbom-input/runtime/requirements.txt"
            ),
            3,
        )
        self.assertIn("Verify one-to-one archive and runtime SBOM mapping", workflow)
        self.assertIn("test \"${#archives[@]}\" -eq 7", workflow)
        self.assertIn("subject-path: artifacts/*.zip", workflow)
        for subject in (
            "windows",
            "macos_x86_64",
            "macos_arm64",
            "pyinstaller_x86_64",
            "pyinstaller_aarch64",
            "appimage_x86_64",
            "appimage_aarch64",
        ):
            self.assertEqual(
                workflow.count(
                    f"sbom-path: ${{{{steps.subjects.outputs.{subject}_sbom}}}}"
                ),
                1,
            )
            self.assertEqual(
                workflow.count(
                    f"subject-path: ${{{{steps.subjects.outputs.{subject}_archive}}}}"
                ),
                1,
            )
        self.assertIn("merge-multiple: true", workflow)
        self.assertIn("! -name SHA256SUMS", workflow)
        self.assertEqual(workflow.count("retention-days: 7"), 5)
        self.assertEqual(workflow.count("compression-level: 0"), 5)
        self.assertEqual(
            (workflow + codeql).count("actions/checkout@"),
            (workflow + codeql).count("persist-credentials: false"),
        )
        self.assertIn("package-ecosystem: pip", dependabot)
        self.assertIn("package-ecosystem: github-actions", dependabot)
        self.assertEqual(dependabot.count("default-days: 7"), 2)
        self.assertIn("security/advisories/new", security)
        self.assertIn("gh attestation verify", security)

        expected_actions = {
            "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
            "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
            "anchore/sbom-action": "e22c389904149dbc22b58101806040fa8d37a610",
            "github/codeql-action/init": "5595ccaf912efad79be6eef63a5619ff05969be3",
            "github/codeql-action/analyze": "5595ccaf912efad79be6eef63a5619ff05969be3",
        }
        action_references = re.findall(
            r"uses:\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@([0-9a-f]{40})(?:\s+#\s+[^\n]+)?",
            workflow + codeql,
        )
        self.assertTrue(action_references)
        self.assertEqual((workflow + codeql).count("uses:"), len(action_references))
        for action, revision in action_references:
            self.assertEqual(expected_actions[action], revision)
        self.assertEqual(codeql.count("github/codeql-action/"), 2)
        self.assertIn("security-events: write", codeql)
        self.assertIn("build-mode: none", codeql)

    def test_local_scripts_use_hash_locks_and_normalized_packaging(self) -> None:
        build_sh = (ROOT / "build.sh").read_text(encoding="utf8")
        setup_sh = (ROOT / "setup_env.sh").read_text(encoding="utf8")
        build_bat = (ROOT / "build.bat").read_text(encoding="utf8")
        setup_bat = (ROOT / "setup_env.bat").read_text(encoding="utf8")
        pack_bat = (ROOT / "pack.bat").read_text(encoding="utf8")
        run_dev_bat = (ROOT / "run_dev.bat").read_text(encoding="utf8")

        self.assertIn('cd "$script_dir"', build_sh)
        self.assertIn("--clean --noconfirm", build_sh)
        self.assertIn('venv_dir="$script_dir/.venv"', build_sh)
        self.assertIn("PYTHONHASHSEED", build_sh)
        self.assertIn("SOURCE_DATE_EPOCH", build_sh)
        self.assertIn('venv_dir="$script_dir/.venv"', setup_sh)
        self.assertIn("sys.version_info[:2] != (3, 10)", setup_sh)
        self.assertIn("--require-hashes --only-binary=:all:", setup_sh)
        self.assertIn(".venv", build_bat)
        self.assertIn("pushd", build_bat.lower())
        self.assertIn("--clean --noconfirm", build_bat)
        self.assertIn("SOURCE_DATE_EPOCH", build_bat)
        self.assertIn("sys.version_info[:2] != (3, 10)", setup_bat)
        self.assertIn("--require-hashes", setup_bat)
        self.assertIn("build_tools\\package_release.py", pack_bat)
        self.assertNotIn("7z.exe", pack_bat.lower())
        self.assertIn(".venv", run_dev_bat)
        self.assertNotIn("\\env\\", run_dev_bat)

    def test_appimage_runtime_and_trust_settings_are_explicit(self) -> None:
        recipe = (ROOT / "appimage/AppImageBuilder.yml").read_text(encoding="utf8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf8")
        runtime = (ROOT / "requirements.txt").read_text(encoding="utf8")

        self.assertIn("requirements-bootstrap.txt", recipe)
        self.assertIn("requirements-release.txt", recipe)
        self.assertEqual(recipe.count("--require-hashes"), 2)
        self.assertIn("python3.10", recipe)
        self.assertIn("python3.10/site-packages", recipe)
        self.assertIn("pip install --require-hashes --only-binary=:all: --ignore-installed", recipe)
        self.assertIn("libxcb-cursor0", recipe)
        self.assertIn("{{APT_REPOSITORY}}", recipe)
        self.assertIn("jammy main universe", recipe)
        self.assertIn("sign-key: None", recipe)
        self.assertNotIn("update-information: guess", recipe)
        self.assertIn("{{APP_VERSION}}-Linux-AppImage-{{ARCH}}.AppImage", recipe)
        self.assertIn("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_APPIMAGE_BUILDER", workflow)
        self.assertIn("requirements-appimage-source.txt", workflow)
        self.assertIn("--no-index --no-deps --no-build-isolation", workflow)
        self.assertIn("apprun_3/helpers/__init__.py", workflow)
        self.assertIn("FUSE_PACKAGE:", workflow)
        self.assertIn('runtime_library_path="$(\n', workflow)
        self.assertIn('LD_LIBRARY_PATH="$runtime_library_path" ldd', workflow)
        self.assertEqual(workflow.count("runner: ubuntu-24.04-arm"), 2)
        self.assertIn("PySide6==6.11.1", runtime)
        self.assertNotIn("pip install --upgrade", recipe)

    def test_release_documentation_is_honest_about_unsigned_artifacts(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf8")
        release = (ROOT / "docs/RELEASE.md").read_text(encoding="utf8")
        self.assertIn("xattr -cr 'Twitch Drops Miner (by DevilXD).app'", readme)
        self.assertIn("not Authenticode-signed", release)
        self.assertIn("not notarized", release)
        self.assertIn("not carry an embedded PGP signature", release)
        self.assertIn("--require-hashes", release)
        self.assertIn("Public binary publication is **disabled by default**", release)
        self.assertIn("private-runtime-publication", release)
        self.assertIn("official-API-only", release)
        self.assertIn("Public binary releases", readme)


if __name__ == "__main__":
    unittest.main()
