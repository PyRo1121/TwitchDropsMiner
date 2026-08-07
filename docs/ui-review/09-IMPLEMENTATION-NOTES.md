# 09 — Qt UI Implementation Notes (Phase 1–2 of the Redesign)

**Date:** 2026-08-06 · **Author:** orchestrator + review swarm
**Preceded by:** [00-UI-REDESIGN-MASTER-PLAN.md](00-UI-REDESIGN-MASTER-PLAN.md) and reports [01–08].

This document records what has actually been **built** so far on the path to
"redo the entire UI" — the working PySide6 UI that reuses the backend untouched — plus how to run it,
what was verified, and what remains.

---

## 1. What was built

A new **PySide6 (Qt 6) UI** with a distinct "Drop Deck" control-room aesthetic, implemented as
a drop-in replacement for the Tkinter `GUIManager`, injected through a
non-breaking `gui_factory` seam.

### New files

| File | Purpose |
| --- | --- |
| `gui_qt/__init__.py` | package; exports `QtGUIManager` |
| `gui_qt/theme.py` | Drop Deck design tokens + QSS dark/light theming |
| `gui_qt/widgets.py` | reusable widgets (Card, StatusDot, Badge, Progress, ProgressRing, StatTile, IconButton, EmptyState) |
| `gui_qt/subs.py` | backend-facing sub-objects: `QtStatusBar`, `QtWebsocketStatus`, `QtLoginForm`, `QtCampaignProgress`, `QtChannelList`, `QtConsole` |
| `gui_qt/tray.py` | `QSystemTrayIcon` adapter with generated state icons |
| `gui_qt/image_cache.py` | Async CDN image cache with Qt pixmap conversion |
| `gui_qt/autostart.py` | Linux, Windows, and macOS autostart integration |
| `gui_qt/manager.py` | `QtGUIManager` (QMainWindow shell: sidebar + stacked pages), `QtSettings`, `QtHelp`, inventory adapter |
| `qt_main.py` | qasync launcher (Qt equivalent of `main.py`) |
| `render_preview.py` | dev tool — renders the UI from mock data to `/tmp/tdm_shots/*.png` for iterating on the look without a live Twitch session |
| `tests/test_qt_ui.py` | offline regression coverage for Qt protocol wiring, locales/RTL, cache hash validation, and Linux autostart |

### Minimal backend integration changes

- `twitch.py` — added the `gui_factory` seam and made `QtGUIManager` the
  production default without changing Twitch transport, authentication, GQL,
  rate limiting, or token behavior.
- `channel.py` and `websocket.py` — kept valid payload behavior unchanged while
  adding structural typing and defensive handling for malformed input.
- `main.py` — now provides the primary Qt launcher shim; `qt_main.py` owns the
  qasync lifecycle and CLI handling.
- `settings.py` — replaced the `main.ParsedArgs` type annotation with a
  structural `CliArgs` Protocol (type-only; enables both launchers to type-check).
- `requirements.txt` — added `PySide6`, `qasync`, and the MIT-licensed
  `QtAwesome` icon set; removed obsolete Tk tray dependencies.
- `build.spec`, CI, and the AppImage recipe — updated for the Qt runtime,
  resources, plugins, and system dependencies.
- `.gitignore` — added `/.venv`; `pyrightconfig.json` — points pyright at the venv.

---

## 2. Architecture

```
                    ┌────────────────────── TwitchDropsMiner ──────────────────────┐
  Twitch backend    │   Qt 6 (PySide6) QMainWindow                                 │
  (twitch.py, ws,   │    sidebar │ QStackedWidget pages                            │
   channel, inv) ─► │    Overview / Channels / Drops / Settings / Help             │
   calls gui.X()    │    (diagnostic event log via Help or Ctrl+K)                 │
        ───────────►│    QSS dark theme (gui_qt/theme.py)                          │
   loop = qasync.QEventLoop   (Qt event loop IS the asyncio loop)                  │
   tray = QSystemTrayIcon (native)                                                 │
                    └────────────────────────────────────────────────────────────┘
```

- The backend calls the same methods as before (`status.update`, `channels.display`,
  `progress.display`, `inv.add_campaign`, `tray.*`, `login.*`, …). A
  **protocol-conformance test** confirms every one is present on `QtGUIManager`.
- No threading, no queues — same single cooperative thread as today. `qasync`
  makes Qt the asyncio loop, so the fragile Tk `_poll()` 50 ms drain is gone.

---

## 3. How to run it

```bash
# one-time setup (Python 3.11 recommended for wheel availability)
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# launch the Qt UI (requires a Twitch account + network to mine)
python main.py [--tray] [-v] [--log] [--dump]

# direct equivalent entry point
python qt_main.py [--tray] [-v] [--log] [--dump]

# preview the UI locally from mock data (no login needed)
python render_preview.py        # writes PNGs to /tmp/tdm_shots/
```

`gui.py` remains only as historical legacy source; it is excluded from the Qt
frozen build.

---

## 4. Verification performed

- **Backend boundary audit:** the diff changes GUI composition, typing, and
  malformed-input handling only; valid Twitch endpoints, persisted-query hashes,
  headers, OAuth/device login, token handling, rate limits, and request semantics
  were not changed.
- **Static checks:** direct venv `compileall`, primary LSP diagnostics, and
  `git diff --check` pass for the edited Python/UI files.
- **Runtime render (offscreen):** `QtGUIManager` constructs and renders all 7
  user-facing surfaces (including the login state) to PNG with the Drop Deck deep-navy palette, compact navigation rail, command
  palette affordance, bento overview, progress ring, and icon-led surfaces.
- **Protocol conformance:** the backend-driven GUI members, direct
  `help._invalidate_button.config(state=…)` poke, partial WebSocket updates,
  login-button interaction, channel-switch callback, and Linux autostart
  round-trip were exercised successfully.
- **Regression tests:** seven offline `unittest` cases cover protocol wiring,
  persistent tray startup, async close interruption, all-locale construction
  with Arabic RTL, malformed WebSocket input, cache hash validation, and Linux
  autostart round-tripping.
- **PyInstaller:** the Qt spec builds and the local frozen `--version` smoke
  test now pass. On Linux, the spec patches only a temporary copy when a
  Python distribution marks `libpython`'s GNU_STACK executable; obsolete
  Tk/pystray modules and the unused Qt TIFF plugin are excluded.
- **Locale smoke:** existing translator-backed Qt labels were constructed for
  all 21 locale files, including Arabic right-to-left layout direction; full
  locale visual coverage remains open.
- **AppImage:** the recipe parses as valid YAML and includes the Qt/XCB/TIFF
  runtime dependencies. An actual appimage-builder run was not performed.

> **pyright gate note:** pi-lens's lens runner invokes pyright with system
> Python (no venv), so it reports `reportMissingImports` false-positives for
> `PySide6`/`qasync`/`truststore`. Direct pyright (venv config) and runtime are
> clean. This is an environment limitation, not a code defect.

---

## 5. Visual direction and research decisions

The second visual pass was informed by current layout and accessibility
research, then deliberately moved away from the common dark SaaS vocabulary:

- The local design audit and the official Material 3 canonical-layout guidance
  both reinforce a grid and content hierarchy; they do not require a floating
  card for every datum. Fluent 2 navigation guidance reinforces short labels,
  persistent selection, and shallow navigation. The implementation keeps the
  useful shell ideas but does not copy either system.
- Government design-system layout/type guidance and WCAG contrast guidance led
  to a continuous canvas, ruled sections, readable body text, and hierarchy by
  type/spacing rather than shadow/elevation.
- `QtAwesome 1.4.2` supplies Phosphor icons with an MIT wrapper and works with
  PySide6. The tempting QFluentWidgets package was not adopted: its dual
  GPLv3/commercial license and stock Fluent vocabulary would add distribution
  and visual constraints that do not fit this MIT project.
- The result is an original editorial instrument: warm ink instead of navy,
  acid-lime signal color instead of purple, serif display headings with mono
  metadata, square controls, thin rules, a single focal hero, flat channel/drop
  lists, and no gradients. The transport event log is deliberately separated
  from the user-facing Overview.

## 6. What is implemented vs. still open

Implemented (working vertical slice):

- Shell: focused sidebar nav (Overview/Channels/Drops/Settings/Help), with the
  diagnostic event log still reachable from Help or Ctrl+K.
  - stacked pages + status dot + `QSystemTrayIcon` tray (5 state icons,
    Show/Quit menu, live-progress tooltip, notifications).
- **Overview**: user-facing run status, login panel, campaign/drop hero with
  progress ring and countdown, plus quick steering actions. Transport diagnostics
  and raw logs are intentionally not shown on the overview.
- **Channels**: live/offline/pending channel rows with game context, selection,
  watching highlight, and the existing channel-switch state callback.
- **Drops**: campaign cards, per-drop progress, CDN thumbnails,
  cache reuse, filters, refresh, and claim-state updates.
- **Settings**: language, dark mode, tray/autostart/notifications toggles,
  priority/exclude editors, proxy validation, advanced toggles, and Reload;
  writes go through the existing `Settings` object.
- **Help**: links/about surface plus the existing invalidate-token flow.
- **Login**: username/password/2FA validation and async device-code interaction.
- **qasync launcher**: Qt event loop, single-instance lock, CLI flags, signal
  handlers, logging-to-activity, and shutdown save ordering.
- **Packaging/autostart**: PyInstaller Qt resources/plugins, CI smoke preview,
  AppImage Qt dependencies, and Linux/Windows/macOS autostart writers.

Not yet done / needs live or platform validation:

- **A real Twitch session:** login/mining/WebSocket/drop-claim paths must be
  exercised against a live account; static or mock UI checks are not evidence
  of live Twitch correctness.
- Full Windows/macOS frozen builds, an actual AppImage-builder run, and macOS
  signing/notarization were not performed in this environment; native CI smoke
  steps are now configured for those artifacts.
- Many new presentation strings remain English-first; existing translator-backed
  labels, the restart-required language setting, and Arabic right-to-left layout
  are wired, but full Qt locale re-rendering/RTL verification is still open.
- Live end-to-end tests and a 24/7 soak test remain open; the offline Qt
  regression suite is now present and runs in CI validation.

---

## 7. Suggested next steps (in order)

1. Validate a real session: login, drop-watching, channel switching, claiming,
   tray behavior, and a full 24/7 soak.
2. Run the AppImage recipe and Windows/macOS frozen builds on their native CI
   runners; add macOS signing/notarization before release.
3. Complete locale coverage and visual RTL/locale smoke coverage for every Qt page.
4. Expand regression coverage to cache persistence and backend callback
   concurrency behavior.
5. Revisit deletion of the historical Tk source only after downstream users no
   longer need it; the production launcher is already Qt.
