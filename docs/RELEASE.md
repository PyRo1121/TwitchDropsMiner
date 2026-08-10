# Release engineering and signing readiness

This document describes the development-release pipeline on `master`. It is a
security boundary: changes to workflows, lock files, the PyInstaller spec, or the
AppImage recipe require review as release changes.

## What the pipeline guarantees

Each native build uses CPython 3.10 and preserves the existing matrix:

- Windows x86-64;
- macOS x86-64 and arm64;
- Linux PyInstaller x86-64 and aarch64; and
- Linux AppImage x86-64 and aarch64.

Release filenames have the form
`Twitch.Drops.Miner-<version>.<short-commit>-<platform>-<architecture>.zip`.
The commit-addressed release tag remains `dev-build-<full-commit>`.

The build sets `PYTHONHASHSEED=0` and sets `SOURCE_DATE_EPOCH` to the source
commit timestamp. A single Python archiver then sorts entries, normalizes ZIP
timestamps and permissions, and preserves macOS bundle symlinks. This makes the
archive layer repeatable for identical payloads. PyInstaller's documentation
specifically requires a fixed `PYTHONHASHSEED` for bit-for-bit repeatability and
uses `SOURCE_DATE_EPOCH` for the Windows PE timestamp.

This is **not** a claim that native payloads are bit-for-bit reproducible across
runs. GitHub-hosted runner images and AppImage Ubuntu package resolution are not
content-locked, and Apple or Microsoft signing timestamps would intentionally
vary. Reproducible native payloads require controlled runner images, pinned OS
package snapshots for both Ubuntu archive architectures, and two-build
comparison jobs. Do not describe the current build as reproducible beyond the
normalized archive layer.

## Python dependency integrity

Release installs use pip's all-or-nothing hash-checking mode:

```text
python -m pip install --require-hashes --only-binary=:all: -r requirements-bootstrap.txt
python -m pip install --require-hashes --only-binary=:all: -r requirements-build.txt
```

`requirements-release.txt` is a release-only, hash-locked mirror of the runtime
pins in `requirements.txt`, plus explicitly pinned Python 3.10-only transitive
dependencies; a regression test requires every runtime package/version to stay
aligned and constrains the allowed extra set. It allows only the wheels selected
for the five native OS/architecture targets. The AppImage builder graph is
separately locked in `requirements-appimage.txt`, and its source archive is locked in
`requirements-appimage-source.txt`. Its two source-only dependencies and the
commit-pinned appimage-builder archive have reviewed SHA-256 hashes. The
upstream archive currently omits a package marker needed to include its AppRun 3
helper namespace in built wheels, so CI adds an empty `__init__.py` after hash
verification and installs the patched tree with indexes and dependencies
disabled. The installed CLI is executed as a regression check. Other release
installs use wheels only.

When updating a lock:

1. change the exact version deliberately;
2. download the wheel or source archive for every affected matrix target from
   the publisher's canonical index;
3. calculate each local digest with `python -m pip hash <file>`;
4. replace, rather than append to, the old artifact allowlist;
5. run `python -m unittest -v tests.test_build_configuration` and perform a
   clean `--require-hashes` install on Python 3.10; and
6. review package provenance and the lock diff before merging.

Pip documents that hashes supplied only by a remote index do not establish
trust. Hashes become a useful tamper check here only after they are recorded and
reviewed in Git.

## Checksums, SBOM, and provenance

The privileged release job runs only for this repository's `master` branch. It:

1. downloads the uniquely named native archives;
2. expands them beside the reviewed lock files for Syft discovery;
3. emits `Twitch.Drops.Miner.spdx.json` in SPDX JSON format;
4. emits and immediately verifies a sorted `SHA256SUMS` manifest;
5. creates GitHub/Sigstore build-provenance and SBOM attestations for every ZIP;
   and
6. creates a new immutable, commit-addressed prerelease without deleting an old
   release.

Actions are pinned to full 40-character commit IDs. The release job alone gets
`contents: write`, `id-token: write`, `attestations: write`, and
`artifact-metadata: write`; ordinary validation and native builds retain
`contents: read` only. Checkout credentials are never persisted.

Consumers should download a ZIP, `SHA256SUMS`, and the SPDX document from the
same release, then run:

```bash
sha256sum --check SHA256SUMS
gh attestation verify <artifact.zip> --repo PyRo1121/TwitchDropsMiner
```

A checksum detects a mismatch but does not identify the publisher on its own.
The GitHub attestation binds the artifact digest to this repository and workflow.

## Credential-dependent blockers

No signing credential is present in this repository or required by pull-request
CI. The following items must remain documented as blockers until maintainers
provide hardware-backed or environment-protected release credentials and add a
separate, reviewed signing stage.

### Windows Authenticode

Windows executables are currently **not Authenticode-signed**. CI reports the
`Get-AuthenticodeSignature` result before packaging so that the status is
visible. Production signing requires a trusted code-signing certificate and its
private key (preferably via a managed/HSM-backed signer), plus an RFC 3161
timestamp service. Microsoft requires explicit digest algorithms; the signing
stage must use SHA-256 for both:

```powershell
signtool sign /fd SHA256 /tr <RFC3161-URL> /td SHA256 <executable.exe>
signtool verify /pa /v <executable.exe>
```

Signing and verification must occur before the normalized ZIP is created. Do
not add a base64 PFX or its password as a repository variable or file.

### macOS Developer ID and notarization

The macOS bundles receive PyInstaller's ad-hoc signatures and CI verifies their
nested code with `codesign --deep --strict`, but they are **not notarized** and
do not carry a trusted Developer ID signature. Distribution readiness requires:

- a `Developer ID Application` certificate and private key;
- the Apple team identifier;
- Hardened Runtime signing with only reviewed entitlements;
- App Store Connect/notary credentials stored in a protected signing keychain;
- `xcrun notarytool submit ... --wait` and log review; and
- `xcrun stapler staple` followed by `stapler validate` and `spctl` assessment.

The order is build, sign nested code and the app, package for notarization,
submit, staple the accepted ticket to the app, verify, then create the release
ZIP. Apple's legacy `altool` is not acceptable. The current ad-hoc output must
not be represented as notarized.

### AppImage signing and OS packages

AppImages currently do **not carry an embedded PGP signature**. The recipe also
omits update information so an unsigned in-place update channel is not
advertised. Release SHA-256 manifests and GitHub attestations protect downloaded
ZIPs, but they are not an AppImage-native signature.

Embedded signing requires an offline-managed OpenPGP release key and an
independently distributed public key. AppImage documentation warns that
`--appimage-signature` only prints an embedded signature; it does not validate
it. Verification must use an external verifier with an explicitly trusted
public key before execution.

The AppImage Python toolchain is hash locked, but Ubuntu Jammy packages are still
resolved from signed, moving archive pockets. Ubuntu's snapshot service can pin
archive state, but a snapshot must be proven to work for both the amd64 archive
and the arm64 ports archive in appimage-builder before it is enabled. A fixed
snapshot also needs a scheduled security-refresh policy. This is an external
reproducibility blocker, not a reason to remove the ARM matrix.

## Primary references

- GitHub secure use and action pinning:
  <https://docs.github.com/en/actions/reference/security/secure-use>
- GitHub artifact attestations:
  <https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations>
- `actions/attest` modes and permissions:
  <https://github.com/actions/attest>
- pip secure installs:
  <https://pip.pypa.io/en/stable/topics/secure-installs/>
- PyInstaller reproducible builds:
  <https://pyinstaller.org/en/stable/advanced-topics.html#creating-a-reproducible-build>
- Microsoft SignTool:
  <https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool>
- Apple notarization:
  <https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution>
- AppImage signatures:
  <https://docs.appimage.org/packaging-guide/optional/signatures.html>
- Ubuntu snapshot service:
  <https://documentation.ubuntu.com/server/how-to/software/snapshot-service/>
