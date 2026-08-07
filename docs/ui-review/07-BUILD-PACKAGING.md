# 07 — Build & Packaging Audit

**Agent:** build-packaging (BRIEF-07)
**Repo:** `/home/pyro1121/Documents/TwitchDropsMiner`
**Stack today:** Python 3.10+ · Tkinter GUI · pystray tray · PyInstaller (one-file) · AppImage (Python-venv recipe)
**Goal:** Map the build/packaging/distribution pipeline, then predict concretely what the BRIEF-04 UI-stack change does to it.

> **Assumption note.** BRIEF-04's output was being produced in parallel and is not yet on disk at review time. Given BRIEF-04's own stated constraints (reuse the ~6k-LOC Python backend; the Farmer look is "Wails/React web-tech UI"), the two realistic picks are **pywebview** (primary line of fit — thin Python wrapper, web frontend) and **PySide6/Qt** (runner-up). This report analyzes the packaging impact for those two, and separately scores the "go-native Go/Rust" alternatives (Wails v3, Tauri v2) that would require a backend port. If BRIEF-04 lands elsewhere, §5's mapping table still lets any stack be read off.

---

## 1. Pipeline map — every script's job, inputs, outputs, targets

| Artifact | Script / file | Role | Targets | Output |
| --- | --- | --- | --- | --- |
| Dev env | `setup_env.sh` / `setup_env.bat` | Create `python3 -m venv env/`, pip-install `wheel` + `requirements.txt` | dev hosts (Win/Linux) | `./env` venv |
| Dev run | `run_dev.bat` | Prompt console vs GUI, launch `env\scripts\python(w).exe main.py` | Windows devs | running app |
| Build | `build.sh` / `build.bat` | Convenience wrapper: verify `env/`, auto-install `pyinstaller`, run `pyinstaller build.spec` | Win/Linux devs | `dist/` |
| Pack | `pack.bat` | Local 7-Zip fold: `dist\*.exe` + `manual.txt` → `Twitch Drops Miner/` → `Twitch Drops Miner.zip` | Windows devs | zip (manual release) |
| Spec | `build.spec` | PyInstaller recipe — data/binaries/hiddenimports, onefile/codesign/BUNDLE | all platforms | `dist/` |
| AppImage | `appimage/AppImageBuilder.yml` + `appimage/pickaxe.png` | appimage-builder recipe; Python installed into AppDir, not frozen | Linux x86_64 + aarch64 | `Twitch.Drops.Miner-{ARCH}.AppImage` |
| CI | `.github/workflows/ci.yml` | Full matrix + auto dev-build release | see below | Release artifacts + `dev-build` prerelease |

### CI build matrix (from `.github/workflows/ci.yml`)

| Job | Runner | PyInstaller | AppImage | Artifact zip |
| --- | --- | --- | --- | --- |
| `validate` | ubuntu-latest | — | — | JSON-validates every `lang/*.json` |
| `windows` | windows-latest | ✅ | — | `Twitch.Drops.Miner.Windows.zip` |
| `macos` | macos-latest | ✅ (BUNDLE .app) | — | `Twitch.Drops.Miner.MacOS.zip` |
| `linux-pyinstaller` | ubuntu-22.04 / ubuntu-22.04-arm (matrix x86_64, aarch64) | ✅ | — | `...Linux.PyInstaller-{arch}.zip` |
| `linux-appimage` | ubuntu-22.04 / ubuntu-22.04-arm (matrix x86_64, aarch64) | — | ✅ | `...Linux.AppImage-{arch}.zip` |
| `update_releases_page` | ubuntu-latest | — | — | Downloads all artifacts → deletes & recreates `dev-build` prerelease |

Key CI facts to carry forward:

- **One matrix entry per OS; five total build outputs** (Win, macOS, Linux×2 formats ×2 arches). Python pinned `3.10` (`ci.yml` `PYTHON_VERSION: "3.10"`).
- CI appends the git short SHA to `version.py` on each job before building: Windows PowerShell `-Replace '^__version__...'` (`ci.yml` ~54), sed equivalents for macOS/Linux (`ci.yml` ~103, ~171).
- Dev-build release is a **prerelease** (`prerelease: true`, tag `dev-build`), deleted then recreated via `ncipollo/release-action@v1` (`ci.yml` `update_releases_page`). No stable tag workflow with semantic signing/releases — everything rolls through `dev-build`.
- macOS job builds an **unsigned** `.app` and zips it; there is **no codesigning or notarization step** anywhere (`ci.yml` macos job). This is a live distribution gap (see §4.4).
- Linux PyInstaller job patches `LD_LIBRARY_PATH` with a **freshly built `libXft 2.3.9`** and runs under `xvfb-run --auto-servernum` because Ubuntu 22.04 ships libXft 2.3.4 which crashes with emoji fonts on some Xorg setups (`ci.yml` "Build a recent version of libXft" + `build.spec` excluded `librsvg`). The same libXft workaround appears in the AppImage recipe.

---

## 2. PyInstaller details (`build.spec`)

### Critical flags (top-of-file "Simple configuration", `build.spec` ~35-39)

```python
upx: bool = False      # UPX compression → AV-flag risk, kept off
console: bool = False  # windowed (no console) — GUI app
one_dir: bool = False  # one-file (single EXE) is the default
optimize: int | None = None
app_name: str = "Twitch Drops Miner (by DevilXD)"
```

- **one-file** is the shipped mode (`one_dir=False` → everything packed into one executable; `COLLECT`/`.app` only emitted for macOS BUNDLE). Windows practice is double-click-a-single-`.exe`.
- Explicitly these are build-time Python constants, not CLI flags — CI runs `pyinstaller build.spec` with no overrides, so all jobs inherit the file's defaults. The macOS `--noconfirm` flag (`ci.yml` macos) only skips the overwrite prompt.

### Data bundling — everything the app must ship (`build.spec` "to_add" loop ~42-58)

- **5 icons as `.ico`** → `./icons`: `pickaxe`, `active`, `idle`, `error`, `maint` (`build.spec` 43-47). Loaded at runtime with `Image.open(resource_path("icons/*.ico"))` in the tray (`gui.py` 1088-1092) and `set_root_icon(root, resource_path("icons/pickaxe.ico"))` (`main.py` ~66, `gui.py` 2217).
- **Every `lang/*.json` except `English.json`** → `./lang` (`build.spec` 53-56, `DEFAULT_LANG = "English"` from `constants.py:125`). English is the code-level fallback, so there is intentionally no English file to ship — consistent with `.gitignore` (`/lang/English.json`) and `translate.py` (`DEFAULT_LANG` removed from the installable list, `translate.py` 466-468). `LANG_PATH` resolves through `_resource_path("lang")` (`constants.py:101`).
- **`seleniumwire/ca.crt` + `ca.key`** are listed but `required=False` (`build.spec` 50-51) — vestigial for the current aiohttp-based client, silently skipped when absent. (`seleniumwire` is not even in `requirements.txt`; this is dead spec content.)

### Hidden imports & hooks (`build.spec` 74-86)

- `PIL._tkinter_finder` — required because Tkinter embeds a Tcl/Tk that must find the Pillow `_imagingtk`/Tkinter bridge after freeze.
- A block of `setuptools._distutils.*` — used by some vendored dependency at runtime; pinned so PyInstaller doesn't miss them.
- aiohttp / websockets: **no custom hook needed** — PyInstaller ships built-in hooks for `aiohttp` and `websockets`, and the spec relies on them implicitly (no `hiddenimports` entry). This is worth re-verifying after any backend change, aiohttp is imported dynamically.

### Linux tray support (`build.spec` 88-110)

- On Linux only, bundles the **AppIndicator** integration needed by pystray's backend:
  - `girepository-1.0/AyatanaAppIndicator3-0.1.typelib` → `gi_typelibs` (datas)
  - `libayatana-appindicator3.so.1` → `.` (binaries)
  - hiddenimports `gi.repository.Gtk`, `gi.repository.GObject`, `hooksconfig.gi` with `languages: ["en_US"]` and empty icons/themes.
- Path resolution is Debian-specific: `Path(f"/usr/lib/{arch}-linux-gnu")` with a `/usr/lib64` fallback (`build.spec` 91-95). This is why the CI Linux job installs `gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1` (`ci.yml`).

### Size trimming

- **UPX deliberately off** (`build.spec:35`) — and PyInstaller notes UPX is not useful for Linux binaries anyway (only Windows) and can break rebuilt `.so` libs ([PyInstaller usage](https://pyinstaller.org/en/stable/usage.html)).
- Linux `excluded_binaries` globs strip `libicudata.so.*`, `libicuuc.so.*`, `librsvg-*.so.*` (`build.spec` 117-125) — dropping ICU (large) and rsvg which the GTK path pulls in.

### macOS bundle (`build.spec` 147-156)

- `BUNDLE(..., name='Twitch Drops Miner (by DevilXD).app', icon='icons/pickaxe.ico', bundle_identifier='com.twitchdrops.miner')` — the only place a proper `.app` is assembled. No `codesign_identity`, no entitlements, no notarization anywhere (see §4.4).

### Artifact size

Not determinable without a build (none present in repo; `dist/` and `build/` are gitignored). Based on equivalent Python 3.10 + aiohttp + Pillow + pystray + (Linux) PyGObject one-file builds, expect roughly **35–60 MB** on Windows and Linux; the macOS `.app` larger (BUNDLE + frameworks). **Estimate only** — flagged as such; the meaningful comparison is relative growth after any stack change (§4), not the absolute number.

---

## 3. Tray integration (`pystray`)

### Wiring

- `gui.py` `TrayIcon` (class ~`gui.py:1086`): a dict `self._icon_images` loads the five `.ico` states up front (`gui.py` 1088-1092), a `pystray.Menu` with Show (default) / Quit items (`gui.py` 1145-1149), and `self.icon.run()` is launched **in a thread-pool executor** so the tray keeps the process alive while Tk is hidden (`gui.py` 1153-1154: `loop.run_in_executor(None, self.icon.run)`, `run_detached()` commented out).
- State changes call `self.icon.icon = self._icon_images[state]` via `change_icon(state)` (`gui.py` 1200-1205) — used for active/idle/error/maint states, including the error-tray on fatal shutdown (`main.py` ~190).
- A `--tray` CLI flag (`main.py` parser) and settings `autostart_tray`/`tray_notifications` (settings UI `gui.py` 1652-1666) control start-minimized-to-tray; the autorun flag is re-emitted into the VENV-autostart command line (`gui.py` 1868-1869).

### Per-platform

- **Windows/Linux:** app can start hidden into the tray (`gui.py:2330` `if self._twitch.settings.tray and sys.platform != "darwin"`), so near-total AFK-run is supported.
- **macOS:** the tray is **excluded from start-hidden** (`sys.platform != "darwin"` guard at `gui.py:2330`). pystray on macOS requires an `.icns`/`NSStatusItem`, and the codebase ships only `.ico` — the spec's icon is `.ico` for the `.app` too, so macOS tray icons rely on PIL decoding the ico; behavior is explicitly special-cased to NOT auto-hide on macOS.
- **Linux backend:** pystray uses GTK/AppIndicator; the spec bundles the AppIndicator typelib + `libayatana-appindicator3` and `PyGObject<3.51; sys_platform == "linux"` (`requirements.txt`). This is the fragile seam — it depends on the exact `girepository` distro layout (§2).

### Headless

- pystray (AppIndicator/GTK) and Tk both need a display; the CI builds under `xvfb-run` (`ci.yml` linux). The app has **no true headless/daemon mode** — `main.py` always builds a `tk.Tk()` first (for the parser error messagebox, `main.py` ~63-71) before the real window. So "run 24/7 without a desktop" is not a real scenario; on a headless server the run simply fails. A UI rewrite should decide whether a real daemon mode is wanted (matters for how big the background-webview keeps the footprint).

### Single-instance

- Not tray-specific but packaging-relevant: `main.py` final block uses `lock_file(LOCK_PATH)` (`utils.py:73`, `constants.py:105` for `LOCK_PATH`) and exits **code 3** if the lock is held (`main.py` ~200-205). The one-file EXE writes `lock.file`, `log.txt`, `settings.json`, `cache/`, `cookies.jar` next to itself in `WORKING_DIR` (`constants.py` 94-109).

---

## 4. UI-stack change impact (per BRIEF-04)

Ground truth for all three options below: **back end is Python + aiohttp + asyncio and must be reused**; targets are Win/macOS/Linux desktop with tray; the reference look is Farmer's React/Vite UI. Everything else is packaging consequence.

### 4.1 Option A — pywebview (primary line of fit: Python wrapper + web frontend)

pywebview wraps the OS-native webview (Edge WebView2 / WKWebView / WebKitGTK), so **no browser engine ships** — the app shell stays a Python one-file EXE, preserving almost all of `build.spec` (§2).

New build steps / deps:

- Add `pywebview` (+ its platform backends: WebView2 on Windows, Cocoa/WebKit on macOS, GTK on Linux) to `requirements.txt`.
- Keep `build.spec` structure. **Critical new hook consideration:** PyInstaller will auto-collect *all* pywebview GUI backends it discovers, including ones not used (e.g. PyQt/PySide pulled in on Windows despite EdgeChromium) — must exclude unused ones (`--exclude-module PyQt6 --exclude-module PySide6`) or the artifact balloons ([pywebview freezing guide](https://pywebview.idepy.com/en/guide/freezing)).
- Static frontend (the React build output) must be bundled as **datas** (`--add-data "frontend:frontend"`) the same way `build.spec` already bundles `lang/` — no new mechanism, just more `to_add` entries.
- `--windowed`/`console=False` already set (`build.spec:36`), no change.

Impact:

- **Artifact size:** roughly flat vs today (a few MB for pywebview + the tiny JS bundle + WebView2 bootstrap). Linux still carries the `gi`/AppIndicator baggage since pywebview's GTK backend reuses WebKitGTK — expect the existing libXft workaround to persist, now needed by WebKitGTK too.
- **Tray:** pystray stays exactly as-is (it is UI-shell-independent); `TrayIcon` in `gui.py` survives the rewrite untouched. The Linux AppIndicator bundling in `build.spec` (§2) is unaffected and must stay.
- **Per-platform gotchas:** WebView2 on Windows requires the runtime (bootstrap) or embedding (~180 MB) ([Tauri's Windows installer docs quantify the same WebView2 options](https://v2.tauri.app/distribute/windows-installer/)); WKWebView on macOS is fine unsigned-ish but the notarization gap applies.
- **CI:** only the `windows`/`macos`/`linux-*` datas change. The single biggest *new* CI concern is a frontend build step (npm/Vite) feeding the Python pack → CI must install Node, run the Vite build, then PyInstaller. `linux-appimage` currently copies `*.py` into the AppDir and compiles them (`AppImageBuilder.yml` script) — with a frontend it must also copy `frontend/dist`. i18n: the `lang/*.json` flow is untouched.
- **macOS Logging/background:** WKWebView + pystray coexists fine; no new entitlement strictly required for local use, but see §4.4.

### 4.2 Option B — PySide6 / Qt (runner-up: native widgets or QML)

Impact is the **largest** of the Python-retaining options; this is the heavy one.

- **New dependency + one-file freeze of a large Qt.** Qt6 wheels are big; a frozen PySide6 app typically lands in the **150–250 MB** range (making Electron's 120–200 MB comparable). UPX won't help on Linux ([PyInstaller docs](https://pyinstaller.org/en/stable/usage.html)). The slim `excluded_binaries` ICU strip (`build.spec` 117-125) is exactly the kind of trick that breaks here — Qt *needs* ICU.
- **If Qt WebEngine is used** (to get the Farmer web-tech UI), it embeds Chromium: must bundle `QtWebEngineProcess`, `.pak` resources, ICU/V8 data, locales — PyInstaller's PySide6 WebEngine hooks handle it but only on `--onedir`, and AppImage size goes **70 MB+ → several hundred MB** (Qt's own guide: you cannot strip WebEngine resources, they are runtime requirements) ([Qt WebEngine deploy docs](https://doc.qt.io/QT-6/qtwebengine-deploying.html)). **Recommendation: avoid WebEngine entirely** — if the new UI is Qt-native widgets there's no browser and size stays in the 100-200 MB window.
- **Linux:** replacing `PyGObject` tray with Qt's `QSystemTrayIcon` would *remove* the fragile AppIndicator bundling in `build.spec` (§2) — that is the genuine upside. But if you keep a web frontend via WebEngine, the libXft/WebKit workarounds are traded for Chromium's own.
- **PyInstaller hooks:** modern PyInstaller has `PySide6` hooks; current versions avoid collecting all of QtQml when unused — still must be careful to import only needed Qt modules ([PyInstaller changelog](https://github.com/pyinstaller/pyinstaller/blob/develop/doc/CHANGES.rst)).
- **CI/build tooling:** `build.spec` needs a dedicated PySide hook block and excluded-module list; AppImage recipe gets bigger (WebEngine locale pruning only after testing supported locales). Everything else in §2 datas (icons, lang) stays mechanical.

### 4.3 Option C — Wails v3 / Tauri v2 (go-native, only if the Python backend is ported)

Scored for completeness; both **require porting the ~6k-LOC Python aiohttp backend to Go/Rust**, which defeats BRIEF-04's "reuse backend" constraint — so these are only realistic for a full-stack rewrite, not the UI change.

- **Wails:** v3 (procedural API, multi-window) is still pre-release; v2.12/2.13 is the stable line ([Wails v3 FAQ](https://v3.wails.io/faq/)) — packaging would target v2. Native WebView (no bundled Chromium), so sizes stay small (~5-20 MB). Ships its own tray API and its own installers per-OS (uses nsis on Windows, .app on macOS, AppImage on Linux) — **replaces** PyInstaller entirely, so `build.spec`, `appimage/`, and the whole `env` build flow go away. A multi-window/tray-capable v3 would also match the requirements.
- **Tauri v2:** Rust + OS webview; tiny baseline (600 KB advertised, real installers 3–8 MB macOS) ([Tauri](https://tauri.app/)) but AppImage bundling inflates 2–6 MB → **70+ MB** ([Tauri AppImage docs](https://v2.tauri.app/distribute/appimage/)). Windows relies on WebView2. Tauri brings **signed/notarized-savvy** wiring for macOS by default — unlike PyInstaller today.
- Either is the "correct 2026 stack for a from-scratch app with small bundles + native trays," but it is a **backend rewrite**, not a UI swap — out of scope of this brief's assumption and of similar effort magnitude.

### 4.4 Cross-cutting: codesigning / notarization (both A and B)

Today the macOS `.app` is **built unsigned and zipped** (`ci.yml` macos; no codesign step). PyInstaller ad-hoc signs by default, which is *not* valid for Developer ID distribution; proper macOS distribution needs an Apple **Developer ID Application** cert, hardened runtime, `codesign --verify --deep --strict`, then `notarytool` (altool died Nov 1, 2023) and stapling ([Apple notarization docs](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution), [PyInstaller feature notes](https://pyinstaller.org/en/v5.0/feature-notes.html)). PyInstaller's `--codesign-identity`/`--osx-entitlements-file` flags do this at build time. **A rewritten UI does not change this** — it is a pre-existing distribution gap that should be closed regardless of stack, made *easier* by Tauri (Option C) but requiring explicit work in A/B. Windows signing (Authenticode) is likewise absent today.

### 4.5 Size comparison for the decision

| Stack | ~Frozen size | Tray cost | Linux frag | Backend reuse | New CI steps |
| --- | --- | --- | --- | --- | --- |
| Today (Tkinter) | ~35–60 MB | pystray + AppIndicator | libXft workaround, gi bundling | — | none (Vite made) |
| A. pywebview + React | ~flat (+ few MB) | unchanged pystray | libXft → WebKitGTK | full | + Node/Vite build, exclude-unused-webview-backends |
| B. PySide6 (Qt widgets) | ~100–200 MB | native QSystemTrayIcon (removes AppIndicator hack) | Qt+ICU (no libXft webkit) | full | spec hook block; bigger AppImage |
| B'. PySide6 + WebEngine | 250+ MB | native | heaviest | full | WebEngine .pak/locales bundling; onedir |
| C. Tauri/Wails (full rewrite) | 3–20 MB (go) / 3–80 MB (tauri) | native | native | **none** (port backend) | replace PyInstaller with native bundler |

---

## 5. Release checklist

### How a maintainer ships *today*

1. **Dev-built Windows release:** run `setup_env.bat` (fresh `env`, `requirements.txt` incl. `pywin32`), `build.bat` (auto-installs PyInstaller + `pywin32_postinstall.py -install -silent`), then `pack.bat` (7-Zip `dist\*.exe` + `manual.txt` → zip). Requires `7z.exe` on PATH.
2. **Continuous dev builds (the real release channel):** push/merge to `master` (or `workflow_dispatch`) → CI matrix builds all 5 artifacts → `update_releases_page` deletes + recreates the **`dev-build` prerelease** with everything.
3. **Consumers** grab the platform zip from the `dev-build` release; Linux users pick PyInstaller or AppImage. macOS users currently get an **unsigned, unnotarized** `.app` zip.

### What changes after the rewrite (Option A — pywebview, the least-disruptive path)

1. **New frontend build feeds the Python pack.** Add a CI step (ubuntu for Linux jobs / run on each OS) running `npm ci && npm run build` (Vite) to produce `frontend/dist`; every packaging path (Windows/macOS PyInstaller `build.spec` datas, `linux-appimage` script copy step) must also copy that directory.
2. **`build.spec` gains:** `frontend/dist` in `to_add` (like `lang/`), `pywebview` hiddenimport/exclude rules, `--exclude-module PyQt6/PySide6` to stop accidental Qt collection.
3. **Linux AppImage recipe (`AppImageBuilder.yml`):** add `frontend/dist` to the `cp` step; keep `gir1.2-...appindicator` deps; WebKitGTK replaces nothing here since pywebview-GTK uses it — verify `libxft2` exclusion still holds.
4. **Tray:** `gui.py`'s `TrayIcon` (pystray) is reused unchanged; no spec change to the AppIndicator bundling.
5. **macOS:** independently, add codesign (`codesign_identity`, entitlements) + a `notarytool` step to the macOS job — this gap exists today and should be closed in the same release cycle, not because the UI changed.
6. **Version/release flow** (`version.py` SHA suffix, `dev-build` prerelease) is stack-agnostic and unchanged.

**Handoff to BRIEF-00 (synthesis):** the packaging cost of "keep Python + web-tech UI" (pywebview) is low and localized (a Vite build + datas + exclude rules); PySide6 costs size; Tauri/Wails costs a backend port. Recommend NOT bundling a chromium webview and NOT taking Wails/Tauri unless the backend is being rewritten.

---

## Sources

- [pywebview — Freezing guide](https://pywebview.idepy.com/en/guide/freezing) (incl. PyInstaller collecting unused Qt backends)
- [pywebview](https://pywebview.flowrl.com/)
- [PyInstaller manual](https://pyinstaller.org/en/stable/) (UPX not useful on non-Windows; onedir+PySide6 WebEngine hooks)
- [PyInstaller Common Issues / PySide6 WebEngine](https://github.com/pyinstaller/pyinstaller/blob/develop/doc/CHANGES.rst)
- [Qt — Deploying Qt WebEngine Applications](https://doc.qt.io/QT-6/qtwebengine-deploying.html) (WebEngine can't be stripped; locales)
- [Tauri 2 — Distribute](https://v2.tauri.app/distribute/) / [Windows installer (WebView2 sizes)](https://v2.tauri.app/distribute/windows-installer/) / [AppImage (2–6 MB → 70+ MB)](https://v2.tauri.app/distribute/appimage/)
- [Wails v3 FAQ (still prerelease; v2.x stable)](https://v3.wails.io/faq/)
- [Apple — Notarizing macOS software](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution); [PyInstaller codesign feature notes](https://pyinstaller.org/en/v5.0/feature-notes.html); [Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
- All repo citations are `file:line` as noted inline; external claims accessed Aug 2026.
