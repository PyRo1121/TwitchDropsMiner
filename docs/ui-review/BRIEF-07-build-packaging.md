# BRIEF-07 — Build & Packaging Audit

**Output file: `docs/ui-review/07-BUILD-PACKAGING.md`** (overwrite).

## Mission

Document the **build, packaging, and distribution pipeline** and predict what a UI-stack change (per BRIEF-04's recommendation) does to it. Packaging is a hard requirement: the app ships to Windows/Linux/macOS as a desktop app with tray support.

## Scope (read fully, quote file:line)

- `build.spec` — PyInstaller spec: hidden imports, datas (icons, lang), binaries, onefile/onedir, windowed mode.
- `build.sh`, `build.bat`, `pack.bat`, `run_dev.bat`, `setup_env.sh`, `setup_env.bat` — CI/dev tooling, environment setup (GTK deps? PyGObject on Linux).
- `appimage/` — AppImage config (linuxdeploy?), what's bundled.
- `.github/` — CI workflows (release builds for which OS/arch).
- `requirements.txt` — dependency pins, platform markers.
- `icons/` — icon set (active/error/idle/maint/pickaxe .ico + presumably png), tray icons.
- `version.py`, `main.py` argument handling — version display, single-instance?

## Deliverables

1. **Pipeline map** — every script's job, inputs/outputs, target platforms, current build matrix.
2. **PyInstaller details** — critical flags, data bundling (lang JSON must ship), hooks needed for aiohttp/websockets, size of current artifact if determinable.
3. **Tray integration** — how pystray is wired, per-platform (Windows/macOS/Linux) gotchas, what happens on headless.
4. **UI-stack change impact** — for the BRIEF-04 recommendation (and runner-up): new build steps, new dependencies, artifact size, per-platform considerations (e.g. Qt WebEngine vs system webview, GTK needs, codesigning/notarization on macOS), CI changes. Be concrete.
5. **Release checklist** — steps a maintainer runs to ship today; what changes after the rewrite.

## Rules

- Quote `file:line`/`file:section`; cite external claims (PyInstaller/Qt/Tauri docs, 2026) with URLs.
- Do NOT modify source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-07.md` and stop.
