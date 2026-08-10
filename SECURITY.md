# Security policy

## Supported versions

This project is under active development. Security fixes are applied to the latest commit on the `master` branch and to the newest published release when a release is available. Older development artifacts are immutable and are not patched in place.

## Report a vulnerability privately

Do **not** open a public issue for a suspected vulnerability or attach credentials, OAuth tokens, cookies, logs containing private data, or proof-of-concept exploits to an issue.

Use GitHub's private vulnerability-reporting form:

<https://github.com/PyRo1121/TwitchDropsMiner/security/advisories/new>

Include, when possible:

- the affected commit or release and operating system;
- the security impact and required attacker capabilities;
- minimal reproduction steps;
- whether OAuth tokens, cookies, or account data may have been exposed; and
- any suggested remediation or disclosure deadline.

You should receive an acknowledgement within three business days and a status update within seven business days. Please allow coordinated remediation before public disclosure.

## Scope

Security reports may include credential or cookie exposure, unsafe persistence or migration, arbitrary file access, code execution, untrusted image or network-data handling, release provenance, and authentication or authorization failures.

Twitch service outages, campaign eligibility disputes, unsupported Twitch API changes, and requests for additional automation are not security vulnerabilities. Report ordinary defects through the [issue tracker](https://github.com/PyRo1121/TwitchDropsMiner/issues/new/choose).

## Release verification

Public binary publication of the current private-interface runtime is on hold and disabled by default. Native archives, same-basename archive-specific SPDX SBOMs, and `SHA256SUMS` are retained only as short-lived workflow artifacts for internal validation. A GitHub Release requires a manual dispatch plus explicit repository and protected-environment approval; the gate must remain off without a documented Twitch policy exception or an official-API-only runtime.

If an exceptional publication is approved, verify its checksums and repository/workflow provenance before running a download:

```bash
sha256sum --check SHA256SUMS
gh attestation verify <downloaded-artifact> --repo PyRo1121/TwitchDropsMiner
```

Current Windows, macOS, and AppImage development artifacts do not have trusted platform-native publisher signatures. A matching checksum alone does not authenticate the publisher; use the GitHub attestation as the repository/workflow identity check. Publication controls, signing, and notarization blockers are tracked in [the release engineering guide](docs/RELEASE.md).
