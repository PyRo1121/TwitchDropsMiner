from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BuildConfigurationTests(unittest.TestCase):
    def test_dependency_files_are_exact_locks(self) -> None:
        for name in (
            "requirements.txt",
            "requirements-build.txt",
            "requirements-appimage.txt",
        ):
            with self.subTest(file=name):
                for raw_line in (ROOT / name).read_text(encoding="utf8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or line.startswith("-r "):
                        continue
                    requirement = line.split(";", 1)[0].strip()
                    exact_version = re.fullmatch(r"[^=<>!~\s]+==[^=<>!~\s]+", requirement)
                    pinned_vcs = re.fullmatch(
                        r"[^@\s]+\s+@\s+git\+https://[^\s]+@[0-9a-f]{40}",
                        requirement,
                    )
                    self.assertTrue(
                        exact_version or pinned_vcs,
                        f"{name} contains an unlocked requirement: {line}",
                    )

    def test_ci_uses_locks_and_real_frozen_self_tests(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf8")

        self.assertIn("concurrency:", workflow)
        self.assertGreaterEqual(workflow.count("requirements-build.txt"), 3)
        self.assertIn("requirements-appimage.txt", workflow)
        self.assertGreaterEqual(workflow.count("--self-test"), 4)
        self.assertIn("pyright@1.1.409", workflow)
        self.assertIn("python -m venv .venv", workflow)
        self.assertIn('${PWD}/.venv/bin', workflow)
        pyright_config = (ROOT / "pyrightconfig.json").read_text(encoding="utf8")
        self.assertIn('"pythonVersion": "3.10"', pyright_config)
        self.assertIn("tests.test_translation_schema", workflow)
        self.assertIn("github.repository == 'PyRo1121/TwitchDropsMiner'", workflow)
        self.assertIn("github.ref == 'refs/heads/master'", workflow)
        self.assertIn('gh release create "dev-build-${GITHUB_SHA}"', workflow)
        self.assertIn("SHA256SUMS", workflow)
        self.assertNotIn("gh release delete", workflow)
        self.assertNotIn("--appimage-extract-and-run", workflow)
        self.assertIn('"$image" --self-test', workflow)
        self.assertIn("macos-15-intel", workflow)
        self.assertIn("Twitch.Drops.Miner.MacOS-${{matrix.arch}}", workflow)
        self.assertNotIn("runs-on: macos-latest", workflow)

    def test_release_has_sbom_provenance_and_security_policy(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf8")
        dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf8")

        self.assertIn("attestations: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610", workflow)
        self.assertIn(
            "actions/attest-build-provenance@96278af6caaf10aea03fd8d33a09a777ca52d62f",
            workflow,
        )
        self.assertIn("subject-path: |", workflow)
        self.assertIn("! -name SHA256SUMS", workflow)
        self.assertIn("Twitch.Drops.Miner.spdx.json", workflow)
        self.assertIn("setuptools==83.0.0", workflow)
        self.assertIn(
            "setuptools==83.0.0",
            (ROOT / "requirements-build.txt").read_text(encoding="utf8"),
        )
        self.assertEqual(
            workflow.count("actions/checkout@"),
            workflow.count("persist-credentials: false"),
        )
        self.assertEqual(
            workflow.count("actions/checkout@"),
            workflow.count("actions/setup-python@"),
        )
        self.assertIn("package-ecosystem: pip", dependabot)
        self.assertIn("package-ecosystem: github-actions", dependabot)
        self.assertEqual(dependabot.count("default-days: 7"), 2)
        self.assertIn("security/advisories/new", security)
        self.assertIn("gh attestation verify", security)
        self.assertIn(
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            workflow,
        )
        self.assertIn(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            workflow,
        )
        self.assertIn(
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            workflow,
        )

        codeql = (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf8")
        self.assertIn(
            "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
            workflow + codeql,
        )
        self.assertEqual(codeql.count("github/codeql-action/"), 2)
        self.assertEqual(
            codeql.count("@c4dd10e44af883a891fe31ced449bcb4a6728b9b"),
            2,
        )
        self.assertIn("security-events: write", codeql)
        self.assertIn("build-mode: none", codeql)
        self.assertIn("persist-credentials: false", codeql)

    def test_local_scripts_anchor_outputs_and_share_dot_venv(self) -> None:
        build_sh = (ROOT / "build.sh").read_text(encoding="utf8")
        setup_sh = (ROOT / "setup_env.sh").read_text(encoding="utf8")
        build_bat = (ROOT / "build.bat").read_text(encoding="utf8")
        setup_bat = (ROOT / "setup_env.bat").read_text(encoding="utf8")
        pack_bat = (ROOT / "pack.bat").read_text(encoding="utf8")
        run_dev_bat = (ROOT / "run_dev.bat").read_text(encoding="utf8")

        self.assertIn('cd "$script_dir"', build_sh)
        self.assertIn("--clean --noconfirm", build_sh)
        self.assertIn('venv_dir="$script_dir/.venv"', build_sh)
        self.assertIn('venv_dir="$script_dir/.venv"', setup_sh)
        self.assertIn(".venv", build_bat)
        self.assertIn("pushd", build_bat.lower())
        self.assertIn("--clean --noconfirm", build_bat)
        self.assertIn(".venv", setup_bat)
        self.assertIn("where 7z.exe", pack_bat.lower())
        self.assertIn("%~dp0", pack_bat)
        self.assertIn(".venv", run_dev_bat)
        self.assertNotIn("\\env\\", run_dev_bat)

    def test_appimage_runtime_and_bootstrap_versions_are_explicit(self) -> None:
        recipe = (ROOT / "appimage/AppImageBuilder.yml").read_text(encoding="utf8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf8")
        runtime = (ROOT / "requirements.txt").read_text(encoding="utf8")

        self.assertIn("pip==26.2.1", recipe)
        self.assertIn("wheel==0.47.0", recipe)
        self.assertIn("python3.10", recipe)
        self.assertIn("python3.10/site-packages", recipe)
        self.assertIn("pip install --ignore-installed --prefix=/usr", recipe)
        self.assertIn("libxcb-cursor0", recipe)
        self.assertIn("{{APT_REPOSITORY}}", recipe)
        self.assertIn("jammy main universe", recipe)
        self.assertNotIn("[arch=amd64]", recipe)
        self.assertIn("APT_REPOSITORY:", workflow)
        self.assertIn("FUSE_PACKAGE:", workflow)
        self.assertIn("dependencies=\"$(ldd \"$plugin\")\"", workflow)
        self.assertEqual(workflow.count("runner: ubuntu-24.04-arm"), 2)
        self.assertNotIn("runner: ubuntu-22.04-arm", workflow)
        self.assertIn("PySide6==6.11.1", runtime)
        self.assertNotIn("PySide6==6.8.0.2", runtime)
        readme = (ROOT / "README.md").read_text(encoding="utf8")
        self.assertIn("ARM64 artifacts require `glibc>=2.39`", readme)
        self.assertNotIn("pip install --upgrade", recipe)

    def test_documented_macos_command_quotes_the_application_path(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf8")
        self.assertIn(
            "xattr -cr 'Twitch Drops Miner (by DevilXD).app'",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
