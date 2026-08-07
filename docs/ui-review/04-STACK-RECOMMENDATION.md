# BRIEF-04 — UI Stack Recommendation (2026)

**Author:** Agent `stack-recommend`
**Date:** 2026-08-05
**Scope:** Recommend the best UI stack for the TwitchDropsMiner rewrite and defend it with cited 2025/2026 sources.
**Output:** decision matrix, recommendation, integration architecture, migration path, risk register.

> **Rule applied:** every external claim below carries an inline citation `[title](url)`, all accessed **2026-08-05**. Sources are 2025/2026-dated official docs, release notes, and benchmarks wherever possible.

---

## 0. Verified constraints from the codebase (not assumed)

- Backend is Python 3 + aiohttp + asyncio, non-trivial and tightly coupled to the GUI: the `Twitch` client holds a `gui` reference and calls `self.gui.status.update(...)`, `self.gui.tray.change_icon(...)`, `self.gui.channels.*`, `self.gui.progress.*`, `self.gui.inv.*`, `self.gui.print(...)`, `self.gui.save(...)` etc. (`twitch.py:556–1490`, `main.py`).
- The GUI is already driven **by asyncio, not by a blocking mainloop**: `gui.py:2404` runs a `_poll()` coroutine that drains Tk events every 50&nbsp;ms via `root.dooneevent(DONT_WAIT)` purely "to avoid threads, thread safety, loop.call_soon_threadsafe, futures and all of that" (`gui.py:2409–2411`). This is the single most important architectural fact for stack choice.
- Async⇄UI bridges already exist: `coro_unless_closed()` (`gui.py:2378`), `loop.call_soon_threadsafe` for the tray thread (`gui.py:1143`), `run_in_executor` for pystray (`gui.py:1154`).
- Tray is **pystray** + PyGObject on Linux, icons in `icons/` (`gui.py:1081–1206`).
- i18n is a Python-side translation singleton `_(namespace, key)`: `translate.py`, **20 JSON files** in `lang/` (English + 19 locales, incl. Arabic and CJK). UI-independent.
- Packaging is **PyInstaller** (`build.spec`, one-file: `one_dir: bool = False`, windowed: `console: bool = False`), CI in `.github/workflows/ci.yml`, targets Windows/Linux/macOS, AppImage for Linux.
- License is **MIT** (`LICENSE`).
- Reference app **Farmer** = Wails v2 (Go) + React 19 + Vite 7 + TS (`frontend/package.json`); the user wants Farmer's **look**, not necessarily its Go stack.

---

## 1. Decision matrix

Scores: **1–10** (10 = best). Criteria weighted toward *effort to reuse the Python backend* because the brief makes that the hard constraint.

| Stack | Reuse Python backend | 2026 ecosystem health | Desktop UX (dark / HiDPI / responsive) | Tray + AFK daemon fit | Packaging impact | i18n | Learning / upkeep | **Weighted total** |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **PySide6 / Qt 6 (QWidgets+QSS) — PRIMARY** | **9** | **9** | **9** | **9** | **7** | **9** | **6** | **8.4** |
| **pywebview — RUNNER-UP** | **8.5** | **7** | **7** | **6** | **7** | **9** | **8** | **7.5** |
| CustomTkinter (keep Tkinter) | 9.5 | 6 | 6 | 8 | 9 | 9 | 8 | 7.6 |
| Wails v2/v3 (Go) | 2 | 6 | 8 | 7 | 5 | 6 | 5 | 5.6 |
| Tauri v2 (Rust) | 2 | 8 | 8.5 | 6.5 | 5 | 6 | 5 | 5.9 |
| Flet | 4 | 6 | 6 | 3 | 6 | 9 | 7 | 5.6 |
| NiceGUI | 4 | 6 | 6 | 3 | 5 | 9 | 7 | 5.4 |
| Reflex | 4 | 6 | 6 | 3 | 5 | 9 | 7 | 5.4 |

### Row-by-row citations

**PySide6 / Qt 6 (primary).**

- Ecosystem: PySide6 is the official Qt-for-Python binding, actively shipped — 6.8.1 added Python 3.13 and 6.9 supported it; the Qt team continues releasing through 6.9.x with Python 3.14 prep [PySide6 release notes](https://doc.qt.io/qtforpython-6/release_notes/pyside6_release_notes.html) · [Qt for Python 6.9 blog](https://www.qt.io/blog/qt-for-python-release-6.9) · [PySide6 on PyPI (LGPL-3.0/GPL-2/3)](https://pypi.org/project/PySide6/).
- Licensing: dual-licensed commercial/LGPLv3; an MIT app can stay closed-source with dynamic linking + LGPL notice [Qt – Obligations of the GPL and LGPL](https://www.qt.io/development/open-source-lgpl-obligations) · [Qt Licensing](https://doc.qt.io/qt-6/licensing.html).
- asyncio integration: `qasync` provides a PEP 3156 loop over Qt in the main thread, no `threading.Thread`/`QThread` needed [qasync (PyPI)](https://pypi.org/project/qasync/); Qt also ships `QtAsyncio` [QtAsyncio](https://doc.qt.io/qtforpython-6.8/PySide6/QtAsyncio/index.html).
- Packaging: PyInstaller supports PySide6; current versions ship Qt hooks/plugin runtime handling; `--onedir` is easier to debug than `--onefile` [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html) · [Qt for Python & PyInstaller](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html).
- Tray: native `QSystemTrayIcon` on Windows/macOS/Linux (StatusNotifierItem/XEmbed) with context menu + tooltip [QSystemTrayIcon](https://doc.qt.io/qt-6/qsystemtrayicon.html) · [System Tray Icon example](https://doc.qt.io/qt-6/qtwidgets-desktop-systray-example.html).
- Dark theme + HiDPI: QSS application-wide stylesheet + Qt 6 `PassThrough` DPI rounding policy for fractional 125/150% scaling [Qt Style Sheets](https://doc.qt.io/qt-6/stylesheet.html) · [QGuiApplication](https://doc.qt.io/qt-6/qguiapplication.html). Modern dark QSS templates exist (e.g. PyQtDarkTheme2).

**pywebview (runner-up).**

- Keeps Python backend in-process; renders HTML/CSS/JS in the platform webview: WebView2 (Win), WKWebView (macOS bundles), WebKitGTK/GTK (Linux) [pywebview intro](https://pywebview.flowrl.com/) · [Web engine](https://pywebview.flowrl.com/guide/web_engine).
- Active: v6.1 released Oct 2025; repo active into 2026 [pywebview releases](https://github.com/r0x0r/pywebview/releases).
- Packaging: build each target on its own OS; exclude unused GUI backends in `.spec`; WebView2 Runtime must be deployed/installed on Windows; macOS recommends py2app [PyInstaller](https://pyinstaller.org/en/stable/) · [pywebview freezing](https://pywebview.idepy.com/guide/freezing).
- Tray is **not** built-in — must pair with pystray; webview window/lifecycle adds process coordination → weaker daemon fit than Qt.

**CustomTkinter (keep Tkinter).**

- Active (quiet through 2025, then **v6.0.0 on 2026-06-24**); modern dark widgets, HiDPI, appearance modes on Tkinter [customtkinter PyPI](https://pypi.org/project/customtkinter/) · [CustomTkinter GitHub](https://github.com/tomschimansky/customtkinter).
- Zero migration risk, best backend reuse, but Tkinter's rendering/high-DPI/native-look ceilings persist and it does **not** achieve Farmer's web-grade look; it is a refresh, not the requested rewrite.

**Wails v2/v3 (Go).**

- The user's "look" comes from Farmer, but reusing it means porting ~6k LOC Python to Go or a managed localhost sidecar proxy with a Go bridge [Wails how-does-it-work](https://wails.io/docs/howdoesitwork/) · [Wails app dev / AssetsHandler](https://wails.io/docs/guides/application-development/). That violates the "reuse backend" constraint.
- v3 is beta/alpha in 2026 — not the stable recommendation [Wails v3 Beta](https://v3.wails.io/blog/wails-v3-beta/) · [Wails status](https://v3.wails.io/status/) · [wailsapp/wails releases](https://github.com/wailsapp/wails/releases).

**Tauri v2 (Rust).**

- Mature, frequent releases through 2.11.x in 2026 [tauri-apps/tauri releases](https://github.com/tauri-apps/tauri/releases) · [Tauri 2.0 stable](https://v2.tauri.app/blog/tauri-20/).
- Same blocker as Wails: core is Rust; Python backend would be a sidecar [Tauri process model](https://v2.tauri.app/concept/process-model/). Native WebView consistency is a real risk (WebView2/WKWebView/WebKitGTK).

**Flet / NiceGUI / Reflex.**

- None offers first-class cross-platform system tray; all are browser/server-centric. Flet is Flutter-rendered and its tray is an open feature request [Flet tray issue #954](https://github.com/flet-dev/flet/issues/954); NiceGUI runs a local FastAPI server + webview process in native mode [NiceGUI config/deployment](https://nicegui.io/documentation/section_configuration_deployment); Reflex is browser-frontend + server-side Python [Reflex how-it-works](https://reflex.dev/docs/advanced-onboarding/how-reflex-works/). All are poor fits for a long-running tray daemon.

---

## 2. Recommendation

**Primary pick: PySide6 (Qt 6) — QWidgets UI styled with an application-wide QSS dark theme, tray via `QSystemTrayIcon`, keeping the existing asyncio backend in-process and bridging it with `qasync` (or Qt's built-in `QtAsyncio`).**

Justification: it is the only 2026-maintained stack that (a) reuses the ~6k-LOC Python backend **in-process** with zero porting, (b) replaces the fragile Tk `_poll()` 50 ms drain hack with a proper Qt event loop that *is* the asyncio loop — the exact inverse-and-cleaner of the current design, keeping the same single-threaded cooperative model, (c) delivers first-class native desktop UX (application-wide dark QSS, Qt 6 fractional HiDPI scaling, native `QSystemTrayIcon`), (d) packages with the PyInstaller pipeline already in use (official Qt for Python + PyInstaller guidance), and (e) keeps the existing Python `translate.py` i18n untouched. It is LGPLv3 (dynamic linking) so it fits the MIT project with a small license-notice obligation [Qt licensing](https://www.qt.io/development/open-source-lgpl-obligations). The web-grade "Farmer look" is achievable in QWidgets+QSS (rounded corners, gradients, tokens) without dragging in Chromium via QWebEngineView, which community guidance explicitly favors for a lean desktop app [HTML/CSS/JS in a desktop app — Qt WebEngine vs widgets](https://www.pythonguis.com/faq/html-css-and-js-in-a-desktop-app-qt-webengine-vs-electron-vs/).

**Runner-up: pywebview** (Python backend in-process, HTML/CSS/JS gives the closest literal match to Farmer's frontend). Chosen as runner-up, not primary, because it delegates rendering to three divergent platform webviews (WebView2 / WKWebView / WebKitGTK), offers no built-in tray (relies on pystray), and its per-OS webview consistency is a real operational risk for a 24/7 AFK daemon — the exact reliability property this app needs [pywebview web engine](https://pywebview.flowrl.com/guide/web_engine).

**Explicit non-choices (honest):** Wails v2/v3 and Tauri v2 are rejected because both force a rewrite/proxy of the Python backend (Go/Rust core + localhost sidecar) — i.e., they discard the single largest asset in this rewrite and add a second runtime to package, secure, and keep alive. Flet/NiceGUI/Reflex are rejected for lacking first-class tray/daemon support. CustomTkinter is the low-risk fallback if the team refuses to leave Tkinter, but it cannot deliver the requested visual rewrite.

---

## 3. Integration architecture sketch

**Pattern: in-process async bridge over a frozen `GUIProtocol`, Qt event loop hosting asyncio.**

```
                    ┌─────────────────────── TwitchDropsMiner process ───────────────────────┐
  Twitch API/WS      │   Python 3 + asyncio                    Qt 6 (PySide6)                 │
  (twitch.py,        │                                        ┌──────────────────────────┐     │
   websocket.py,     │   Twitch client ──gui──▶  GUIProtocol  │  Qt Application (QWidgets)│     │
   channel/inventory)│        │ (unchanged calls:            │   Dashboard / Channels /  │     │
                    │        │  status, tray, channels,      │   Priority / Inventory /  │     │
                    │        │  progress, inv, print, save…) │   Settings / Help / Login │     │
                    │        ▼                                │   QSS dark theme          │     │
                    │   loop = qasync.QEventLoop (Qt IS the asyncio loop)                   │
                    │   backend coroutines + Qt slots share one cooperative main thread     │
                    │   tray = QSystemTrayIcon (native)                                    │
                    └──────────────────────────────────────────────────────────────────────┘
```

- **Keep the backend untouched.** The `Twitch` client already depends only on a GUI *object interface* (`self.gui.<member>`). Define and document that interface as a `GUIProtocol` (the members the backend actually calls — verified in §0). The new Qt backend (`GUIManager`) simply implements the same protocol; `gui.py` is the only file retired.
- **Event-loop swap is the crux.** Today Tk events are drained by `_poll()` inside the asyncio loop (`gui.py:2404`). With Qt, invert it: `qasync` makes the Qt event loop the `asyncio` loop, so backend coroutines (aiohttp, websockets) and Qt slots tick cooperatively on one thread — no 50 ms polling, no threads [qasync](https://pypi.org/project/qasync/). Alternatively use Qt's bundled `QtAsyncio` [QtAsyncio](https://doc.qt.io/qtforpython-6.8/PySide6/QtAsyncio/index.html).
- **Backend → UI updates** that today mutate Tk widgets become Qt signal emissions / queued `QMetaObject::invokeMethod`-style updates executed in the Qt thread (the same thread, so no marshalling needed for cooperative calls; keep expensive pure-Python work off the UI path as today).
- **Async UI → backend** calls (`wait_for_login_press`, `ask_login`, `coro_unless_closed`) map 1:1 to asyncio `Future`s awaited by the backend; `coro_unless_closed` survives unchanged because it is thread-agnostic (`gui.py:2378`).
- **Tray:** replace pystray+PyGObject with `QSystemTrayIcon` + `QMenu` (native Show/Quit), tooltip carries the live drop progress, `showMessage()` for notifications [QSystemTrayIcon](https://doc.qt.io/qt-6/qsystemtrayicon.html)]. This drops the `pystray`, `PyGObject`, and (Win) `pywin32` tray dependencies from `requirements.txt`.
- **Reference to Farmer's binding pattern:** Farmer exposes Go methods to React via generated Wails bindings; this Python rewrite needs no binding layer at all because UI and backend are the **same process and same language** — the `GUIProtocol` is the "binding contract," simpler than Wails' generated TS bindings [Wails how-does-it-work](https://wails.io/docs/howdoesitwork/).

---

## 4. Migration path (staged; backend stays untouched)

1. **Phase 0 — freeze the contract (no code change).** Enumerate the `GUIProtocol` from current `self.gui.*` call sites (`twitch.py`, `channel.py`, `websocket.py`, `inventory.py`); cross-check every member against BRIEF-01's feature inventory. This is the acceptance checklist for the rewrite.
2. **Phase 1 — spike the shell.** Stand up a minimal PySide6+`qasync` app that implements *Login*, *StatusBar*, and *Tray* on the protocol. Prove the event-loop swap works with the real aiohttp/websocket stack end-to-end and reuses `translate.py` + `settings.py` unchanged. `gui.py` is still present and untouched.
3. **Phase 2 — port screens.** Port in dependency order: **Dashboard** (status bar, websocket status, CampaignProgress countdown, ConsoleOutput), **Channels list**, **Priority/Exclude editor**, **Inventory overview**, **Settings panel** (incl. autostart query mirroring `gui.py:1860–1960`), **Help tab**. Style everything with one shared QSS token file (see BRIEF-08), keeping the existing `dark_mode` toggle.
4. **Phase 3 — swap tray.** Replace pystray with `QSystemTrayIcon`; remove `pystray`/`PyGObject` from `requirements.txt`; port icon states (`pickaxe/active/idle/error/maint`, `gui.py:1087`) and tooltip/notification logic.
5. **Phase 4 — retire `gui.py` + Tk.** Delete `gui.py` and the `_poll()` hack; switch `main.py` composition root to the Qt GUIManager under `qasync`; drop `tkinter` imports. **Update `build.spec`** (Qt platform plugins, QSS/datas, keep `lang/` JSON) per [Qt for Python & PyInstaller](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html) and `--onedir` guidance [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html); re-run CI matrix + AppImage; on macOS address notarization for Qt binaries (coordinate with BRIEF-07).
6. **Phase 5 — feature parity sign-off.** Walk the Phase-0 protocol checklist against the new UI; validate all **20 locale JSON files** render (esp. Arabic RTL + CJK); soak-test the 24/7 AFK path.

---

## 5. Risk register (primary stack: PySide6/Qt 6)

| # | Risk | Likelihood / Impact | Mitigation |
| --- | --- | --- | --- |
| 1 | **Qt + PyInstaller packaging drift** (Qt platform plugins not collected, QSS/datas missing, artifact growth) | Med / High | Follow official Qt-for-Python PyInstaller guide; build with `--onedir` first; add a frozen-build smoke test per OS in CI; use `qasync`/Qt hooks; verify `lang/*.json` + icons ship (per BRIEF-07). [Qt for Python & PyInstaller](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html) |
| 2 | **Asyncio/Qt event-loop integration bugs** (loop ownership, signal thread-safety, nested `asyncio.run`) | Med / High | Use `qasync.QEventLoop` as **the** loop from startup (documented pattern [qasync](https://pypi.org/project/qasync/)); keep the single cooperative thread; route any real thread (e.g. future native tray calls) through `loop.call_soon_threadsafe` as today (`gui.py:1143`). |
| 3 | **LGPL compliance slip** (static-linking/relink obligations, missing notice) | Low / Med | Ship PySide6 as separate shared libs, no binary-level relink locks, include the LGPLv3 notice + license text in the artifact; revisit if distribution channel changes [Qt obligations](https://www.qt.io/development/open-source-lgpl-obligations). |
| 4 | **i18n/RTL & locale regressions** in the new widget set (Arabic RTL, CJK, 20 locales) | Med / Med | Keep `translate.py` and keys unchanged; set `QGuiApplication.setLayoutDirection` / `QLocale` from `settings.language`; add a per-locale layout smoke check; verify the new font stack covers CJK. |
| 5 | **Feature-parity drift / dropped edge behavior** (drop countdown timer, websocket shard status, login forms, autostart, exclude/priority editor, tray notifications) | Med / High | Phase-0 protocol inventory is the binding checklist; port each screen with a parity test; keep existing settings schema; soak test the full 24/7 AFK loop before retiring `gui.py`. |

---

## 6. Bottom line

PySide6 (Qt 6) + `qasync`, QWidgets with a QSS dark theme and native `QSystemTrayIcon`, is the highest-value stack for this app: it reuses the Python backend in-process, replaces the Tk `_poll()` hack with a proper Qt-as-asyncio loop, delivers the dark/HiDPI/native desktop UX the rewrite is for, and factors cleanly into the existing PyInstaller pipeline. pywebview is the runner-up if the HTML/CSS look is non-negotiable, at the cost of platform-webview consistency and weaker tray/daemon fit. Wails and Tauri are honestly ruled out because they sacrifice the ~6k-LOC backend — the rewrite's biggest asset.
