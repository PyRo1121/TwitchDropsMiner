# Twitch Drops Miner

**Twitch Drops Miner V2** is a maintained fork of *Twitch Drops Miner* (upstream by [@DevilXD](https://github.com/DevilXD)) rewritten around a modern Qt dashboard. It lets you AFK mine timed Twitch drops without having to worry about switching channels when the one you were watching goes offline, claiming the drops, or even receiving the stream data itself. This helps you save on bandwidth and hassle.

- **Maintainers:** [@PyRo1121](https://github.com/PyRo1121) (fork) · upstream by [@DevilXD](https://github.com/DevilXD)
- **Source:** [github.com/PyRo1121/TwitchDropsMiner](https://github.com/PyRo1121/TwitchDropsMiner)

## Play

Ready to start mining? The quickest path to playing is cloning this repo and running from source:

```bash
git clone https://github.com/PyRo1121/TwitchDropsMiner.git
cd TwitchDropsMiner
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py --tray
```

Then, inside the app:

1. Authorize the app with Twitch's device-code login panel.
2. On the **Settings** tab, add the games you want to farm to the **Priority List**.
3. Press **Reload** — the miner finds eligible live channels and starts mining automatically.

See **Usage** below for details. Prebuilt releases (when published) will live on the fork's [Releases](https://github.com/PyRo1121/TwitchDropsMiner/releases) page.

## How It Works

About once per minute, the application sends the same lightweight viewer-presence event that Twitch's player uses, without downloading stream video or sound. The supported default is one watch target. An opt-in experimental setting can try two independent targets when they are eligible for different games and Drop IDs, but Twitch may credit only one. A sharded websocket keeps channel state and viewer-drop progress synchronized.

## Features

- Stream-less drop mining - save on bandwidth.
- Game priority and exclusion lists, allowing you to focus on mining what you want, in the order you want, and ignore what you don't want.
- Sharded websocket connection, allowing for tracking up to `199` channels at the same time.
- Automatic drop campaigns discovery based on linked accounts (requires you to do [account linking](https://www.twitch.tv/drops/campaigns) yourself though).
- Stream tags and drop campaign validation, to ensure you won't end up mining a stream that can't earn you the drop.
- One reliable watch target by default, with an explicitly experimental dual-target option constrained to different eligible games and Drop IDs.
- Automatic channel stream switching, when a watched channel goes offline, as well as when a channel streaming a higher priority game goes online.
- Login session is saved in a cookies file, so you don't need to login every time.
- Mining is automatically started as new campaigns appear, and stopped when the last available drops have been mined.
- The Qt dashboard surfaces the active game's artwork, campaign/drop progress, current channel, session metrics, and a plain-language explanation when the miner is idle.
- Optional Steam enrichment provides current player count, US store pricing when available, and direct Steam/SteamDB links; it is cached and never affects Twitch channel selection.

## Usage

- Download and unzip [the latest release](https://github.com/PyRo1121/TwitchDropsMiner/releases) - it's recommended to keep it in the folder it comes in.
- Run it and authorize the miner from Twitch's device-authorization page; the app never collects account credentials.
- After a successful login, the app should fetch a list of all available campaigns and games you can mine drops for - you can then select and add games of choice to the Priority List available on the Settings tab, and then press on the `Reload` button to start processing. It will fetch a list of all applicable streams it can watch, and start mining right away. You can also manually switch to a different channel as needed.
- If you wish to keep the miner occupied with mining anything it can, beyond what you've selected via the Priority List, you can use the Priority Mode setting to specify the mining order for the rest of the games.
- Make sure to link your Twitch account to game accounts on the [campaigns page](https://www.twitch.tv/drops/campaigns), to enable more games to be mined.

## Pictures

![Main](https://user-images.githubusercontent.com/4180725/164298155-c0880ad7-6423-4419-8d73-f3c053730a1b.png)
![Inventory](https://user-images.githubusercontent.com/4180725/164298315-81cae0d2-24a4-4822-a056-154fd763c284.png)
![Settings](https://user-images.githubusercontent.com/4180725/164298391-b13ad40d-3881-436c-8d4c-34e2bbe33a78.png)

## Notes

### Concurrent viewing warning

Due to how Twitch handles drop progression, watching a stream in the browser or elsewhere on the same account while the miner is active can cause conflicting progress and leave the current Drop stuck. Avoid concurrent viewing on the managed account during mining.

### Credential safety

Persistent web-session cookies remain in `cookies.jar`. OAuth refresh tokens, when Twitch returns one, are stored in the current user's native credential vault: Windows Credential Manager, macOS Keychain, or a Freedesktop Secret Service provider on Linux. Existing `oauth.json` credentials are migrated with a verified, versioned transaction. Credential reads, writes, migrations, and logout deletion are serialized across threads and application processes; rollback occurs only while the value written by that transaction is still present.

If no native provider has ever been provisioned (for example, a first run in a Linux desktop session without Secret Service), the miner retains a provenance-marked, versioned, mode-0600 `oauth.json` fallback instead of discarding the credential. After native storage has been provisioned, an outage fails closed and never creates a new plaintext fallback. A differing fallback and vault value is preserved as an explicit conflict rather than choosing and deleting one. Vault access or integrity failures do not silently downgrade to plaintext storage, and records written by an unknown future version are left untouched and rejected.

Logout first writes a secret-free `oauth.json.state` tombstone, then confirms deletion from both storage locations. Incomplete cleanup is reported and retried before any future load; only a newly-authorized device session may retire a completed logout tombstone. Keep `cookies.jar` and any fallback `oauth.json` safe: either can contain authorization material that gives access without the account password. Mutable application data is stored per user in `%LOCALAPPDATA%\\TwitchDropsMiner` on Windows, `${XDG_DATA_HOME:-~/.local/share}/TwitchDropsMiner` on Linux, and `~/Library/Application Support/TwitchDropsMiner` on macOS.

Login uses Twitch's device-authorization page. The miner never asks for or stores your Twitch username, password, or two-factor authentication code, and credential values are never written to application logs.

### Progress semantics

The time-remaining timer is a presentation estimate for the primary target. Authoritative progress comes from Twitch's PubSub drop events or the assigned target's `CurrentDrop` response; the miner does not fabricate progress when Twitch does not acknowledge it. With two targets, the secondary target is reconciled independently and does not replace the primary hero display.

The source code requires Python 3.10 or higher to run.

## Twitch compatibility and policy

This project is an independent, unofficial client and is not affiliated with or endorsed by Twitch. Viewer-presence, campaign inventory, progress, and claim behavior rely on Twitch web interfaces that are not part of Twitch's documented public developer API. Twitch can change or withdraw those interfaces without notice, and a release may stop working even when its own code has not changed. Users are responsible for deciding whether their use complies with Twitch's current terms and local requirements.

The project deliberately excludes multi-account farming, hosted operation, password/manual-token authentication, channel-points or chat automation, and request-volume-increasing features. Progress shown as authoritative comes only from Twitch responses; the application does not report estimated progress as confirmed credit.

## Binary publication hold and verification

Public binary releases of the current private-interface runtime are on hold. The native matrix still creates short-lived GitHub Actions artifacts for internal validation, including one same-basename SPDX SBOM per ZIP and a verified checksum manifest, but ordinary pushes and pull requests do not create or update a GitHub Release. Publication requires a manual request plus explicit repository and protected-environment approval that defaults off; it must not be enabled without a documented Twitch policy exception or an official-API-only runtime.

If an exceptional release is approved, verify its checksum before running it. With the GitHub CLI installed, verify repository/workflow provenance with:

```bash
gh attestation verify <downloaded-artifact> --repo PyRo1121/TwitchDropsMiner
```

Current Windows, macOS, and AppImage builds do not carry platform-native trusted publisher signatures; checksum and GitHub-attestation verification is therefore especially important. See [the release engineering guide](docs/RELEASE.md) for the publication gate, exact validation guarantees, and signing blockers.

Security vulnerabilities should be submitted through the [private vulnerability-reporting form](https://github.com/PyRo1121/TwitchDropsMiner/security/advisories/new), not a public issue. See [SECURITY.md](SECURITY.md) for the response policy.

## Notes about the Windows build

- To achieve a portable-executable format, the application is packaged with PyInstaller into an `EXE`. Some antivirus engines may flag PyInstaller executables, but a warning must not be assumed to be a false positive. The current executable is not Authenticode-signed: verify its release checksum and GitHub attestation, and run from reviewed source instead if its origin cannot be established.
- The executable uses the `%TEMP%` directory for temporary bundled resources. Persistent settings, credentials, logs, history, and caches are stored in `%LOCALAPPDATA%\\TwitchDropsMiner`.
- The autostart feature is implemented as a registry entry to the current user's (`HKCU`) autostart key. It is only altered when toggling the respective option. If you relocate the app to a different directory, the autostart feature will stop working, until you toggle the option off and back on again

## Notes about the Linux build

- The Linux app is built and distributed using two distinct portable-executable formats: [AppImage](https://appimage.org/) and [PyInstaller](https://pyinstaller.org/).
- There are no major differences between the two formats, but if you're looking for a recommendation, use the AppImage.
- The x86-64 Linux artifacts require `glibc>=2.35`. The ARM64 artifacts require `glibc>=2.39` because current supported Qt for Python ARM wheels use that baseline. Both require a working display server.
- Every feature of the app is expected to work on Linux just as well as it does on Windows. If you find something that's broken, please [open a new issue](https://github.com/PyRo1121/TwitchDropsMiner/issues/new) on the fork.
- The Qt build uses `QSystemTrayIcon` for native system tray and notification support; it no longer depends on GTK/AppIndicator tray packages.
- As an alternative to the native Linux app, you can run the Windows app via [Wine](https://www.winehq.org/) instead. It works really well!

## Notes about the macOS build

- The macOS version is packaged using PyInstaller into a standalone `.app` bundle, distributed as a ZIP archive.
- Since this application has no trusted Developer ID signature and is not notarized, **macOS Gatekeeper will block it** on the first run (saying it "The application is damaged and can't be opened"). Verify the release checksum and GitHub attestation before choosing to remove quarantine metadata.
  - **To fix this**: Either open the Terminal in the folder the app is in (or navigating with `cd path/to/folder`) and enter `xattr -cr 'Twitch Drops Miner (by DevilXD).app'` or just type `xattr -cr` (make sure to put a space at the end), drag and drop the `Twitch Drops Miner (by DevilXD).app` file into the terminal window (this will auto-fill the path) and enter
- Persistent files such as `cookies.jar`, `settings.json`, the `cache` folder, and any vault-unavailable `oauth.json` fallback are stored in `~/Library/Application Support/TwitchDropsMiner`, outside the read-only application bundle. OAuth refresh tokens normally reside in the user's macOS Keychain.

## Advanced Usage

If you'd be interested in running the latest master from source or building your own executable, see the wiki page explaining how to do so: <https://github.com/DevilXD/TwitchDropsMiner/wiki/Setting-up-the-environment,-building-and-running>

## Support

If you'd encounter any issues with the miner:

- Please see the [troubleshooting page](https://github.com/DevilXD/TwitchDropsMiner/wiki/Troubleshooting) from the upstream project for some common issues and their explanation.
- Please [search the issues page](https://github.com/PyRo1121/TwitchDropsMiner/issues?q=sort%3Aupdated-desc%20is%3Aissue) to see if your issue on the fork hasn't been reported yet.
- Upstream-specific issues belong on [DevilXD's repository](https://github.com/DevilXD/TwitchDropsMiner/issues); issues with this fork belong on [PyRo1121/TwitchDropsMiner](https://github.com/PyRo1121/TwitchDropsMiner/issues).

If you find the application useful, a ⭐ on the fork is appreciated!

[![Buy me a coffee](https://i.imgur.com/cL95gzE.png)](https://www.buymeacoffee.com/DevilXD)
[![Support me on Patreon](https://i.imgur.com/Mdkb9jq.png)](https://www.patreon.com/bePatron?u=26937862)

## Project goals

Twitch Drops Miner (TDM for short) has been designed with a couple of simple goals in mind. These are, specifically:

- Twitch Drops oriented - it's in the name. That's what I made it for.
- Easy to use for an average person. Includes a nice looking GUI and is packaged as a ready-to-go executable, without requiring an existing Python installation to work.
- Intended as a helper tool that starts together with your PC, runs in the background through out the day, and then closes together with your PC shutting down at the end of the day. If it can run continuously for 24 hours at minimum, and not run into any errors, I'd call that good enough already.
- Requiring a minimum amount of attention during operation - check it once or twice through out the day to see if everything's fine with it.
- Underlying service friendly - the amount of interactions done with the Twitch site is kept to the minimum required for reliable operation, at a level achievable by a diligent site user.

TDM is not intended for/as:

- Mining channel points - again, it's about the drops: only.
- Mining anything else besides Twitch drops - no, I won't be adding support for a random 3rd party site that also happens to rely on watching Twitch streams.
- Unattended operation: worst case scenario, it'll stop working and you'll hopefully notice that at some point. Hopefully.
- 100% uptime application, due to the underlying nature of it, expect fatal errors to happen every so often.
- Being hosted on a remote server as a 24/7 miner.
- Being used with more than one managed account.
- Mining campaigns the managed account isn't linked to.

This means that features such as:

- It being possible to run it without a GUI, or with only a console attached.
- Any form of automatic restart when an error happens.
- Docker or any other form of remote deployment.
- Using it with more than one managed account.
- Making it possible to mine campaigns that the managed account isn't linked to.
- Anything that increases the site processing load caused by the application.
- Any form of additional notifications system (email, webhook, etc.), beyond what's already implemented.

..., are most likely not going to be a feature, ever. You're welcome to search through the existing issues to comment on your point of view on the relevant matters, where applicable. Otherwise, most of the new issues that go against these goals will be closed and the user will be pointed to this paragraph.

For more context about these goals, please check out these issues: [#161](https://github.com/DevilXD/TwitchDropsMiner/issues/161), [#105](https://github.com/DevilXD/TwitchDropsMiner/issues/105), [#84](https://github.com/DevilXD/TwitchDropsMiner/issues/84)

## Credits

<!---
Note: The translations credits are sorted alphabetically, based on their English language name.
When adding a new entry, please ensure to insert it in the correct place in the second section.
Non-translations related credits should be added to the first section instead.

Note: When adding a new credits line below, please add two trailing spaces at the end
of the previous line, if they aren't already there. Doing so ensures proper markdown
rendering on Github. In short: Each credits line should end with two trailing spaces,
placed past the period character at the end.

• Last line can have the two trailing spaces omitted.
• Please ensure your editor won't trim the trailing spaces upon saving the file.
• Please ensure to leave a single empty new line at the end of the file.
-->

@guihkx - For the CI script, CI maintenance, and everything related to Linux builds.
@kWAYTV - For the implementation of the dark mode theme.
@crocchetto - For the macOS port.

@Bamboozul - For the entirety of the Arabic (العربية) translation.
@Suz1e - For the entirety of the Chinese (简体中文) translation and revisions.
@wwj010, @zhangminghao1989, @Self4215 - For the Chinese (简体中文) translation corrections and revisions.
@Ricky103403 - For the entirety of the Traditional Chinese (繁體中文) translation.
@LusTerCsI - For the Traditional Chinese (繁體中文) translation corrections and revisions.
@nwvh - For the entirety of the Czech (Čeština) translation.
@Kjerne - For the entirety of the Danish (Dansk) translation.
@lmdpocus - For the entirety of the Dutch (Nederlandse) translation.
@Rensoraa - For the Traditional Dutch (Nederlandse) translation corrections and revisions.
@roobini-gamer - For the entirety of the French (Français) translation.
@Calvineries - For the French (Français) translation revisions.
@ThisIsCyreX - For the entirety of the German (Deutsch) translation.
@Nagyhoho1234 - For the entirety of the Hungarian (Magyar) translation.
@Eriza-Z - For the entirety of the Indonesian translation.
@casungo - For the entirety of the Italian (Italiano) translation.
@ShimadaNanaki - For the entirety of the Japanese (日本語) translation.
@biroman -  For the entirety of the Norwegian (Norsk) translation.
@Patriot99 - For the Polish (Polski) translation and revisions (co-authored with @DevilXD).
@zarigata - For the entirety of the Portuguese (Português) translation.
@Sergo1217 - For the entirety of the Russian (Русский) translation.
@kilroy98, @flamesv - For the Russian (Русский) translation corrections and revisions.
@Shofuu - For the entirety of the Spanish (Español) translation and revisions.
@Forero-0 - For the Spanish (Español) translation revisions.
@alikdb - For the entirety of the Turkish (Türkçe) translation.
@DogancanYr, @Elderly-Emre, @Hweord - For the Turkish (Türkçe) translation corrections and revisions.
@Nollasko - For the entirety of the Ukrainian (Українська) translation and revisions.
@kilroy98 - For the Ukrainian (Українська) translation corrections and revisions.
