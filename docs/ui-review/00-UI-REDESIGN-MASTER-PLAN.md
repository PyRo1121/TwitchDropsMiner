# TwitchDropsMiner — Full UI-Redesign Master Plan

**Synthesized by:** orchestrator (agent `pi`, workspace `wB`) from an 8-agent parallel review swarm (workspace `wC`, 8 panes).
**Date:** 2026-08-06 (review run against HEAD `a3fda85`)
**Status:** DECISION-MAKING COMPLETE → ready for Phase 1 implementation.
**Source reports:** see [00-INDEX](00-INDEX.md); each numbered report is linked below.

> Everything in this repo review is **citation-based**: every external technology/UX claim in the source reports carries a URL + access date (2025/2026 sources preferred), and every code claim carries a `file:line` reference. This master plan consolidates them; go to the linked report for full citations.

---

## 0. Executive summary

| Question | Answer |
| --- | --- |
| What is the app? | Python 3 + aiohttp/asyncio + **Tkinter** desktop AFK miner for Twitch drops (~8,171 LOC / 14 modules; `gui.py` = 2,971 lines, one shared Tk+asyncio event loop, pystray tray, 20 locales). |
| What does the user want? | **Redo the entire UI**, adopting the look of **Farmer** (Go + Wails v2 + React 19 + Vite 7 dark-theme dashboard). See [03-FARMER-UI-REFERENCE](03-FARMER-UI-REFERENCE.md) and [08-UX-DESIGN-TOKENS](08-UX-DESIGN-TOKENS.md). |
| **Recommended stack** | **PySide6 (Qt 6) QWidgets + QSS dark theme + `qasync` (Qt loop = asyncio loop) + native `QSystemTrayIcon`.** Runner-up: pywebview. Wails/Tauri explicitly ruled out. See [04-STACK-RECOMMENDATION](04-STACK-RECOMMENDATION.md). |
| Backend plan | **Keep the ~6k-LOC Python backend entirely untouched.** Define a `GUIProtocol` interface from today's `self.gui.*` call sites; the new Qt `GUIManager` implements it. Only `gui.py` is retired. See [02-BACKEND-SURFACE](02-BACKEND-SURFACE.md). |
| Concurrency crux | Replace the fragile Tk `_poll()` 50 ms drain hack with Qt-as-asyncio (no threads, no queues). Single cooperative thread preserved. See [05-ASYNC-THREADING](05-ASYNC-THREADING.md). |
| Design system | Adopt Farmer's 12-token dark-navy palette (`--bg #090d14`, accent `#9b7cff`), extend with status/motion/spacing scales, WCAG-violation fixes. See [08-UX-DESIGN-TOKENS](08-UX-DESIGN-TOKENS.md). |
| Packaging | PyInstaller pipeline stays; add Qt hooks/platform plugins, ship `lang/*.json`; fix macOS notarization gap. See [07-BUILD-PACKAGING](07-BUILD-PACKAGING.md). |
| i18n & settings | Keep `translate.py` + 20 JSON files and the `settings.json` schema verbatim. Optionally add true runtime language switching (a gap today). See [06-I18N-SETTINGS](06-I18N-SETTINGS.md). |

---

## 1. Why this stack (the decision)

Full decision matrix + citations are in [04-STACK-RECOMMENDATION](04-STACK-RECOMMENDATION.md). Bottom line:

- **PySide6/Qt 6 preserves the single biggest asset — the Python backend.** Wails (Go) and Tauri (Rust) both force a rewrite/proxy of ~6k LOC and add a second runtime; rejected.
- PySide6 is actively maintained in 2026 (6.9.x, Python 3.13/3.14) [PySide6 release notes](https://doc.qt.io/qtforpython-6/release_notes/pyside6_release_notes.html), LGPLv3 via dynamic linking fits the MIT app [Qt LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations).
- `qasync` (or Qt's bundled `QtAsyncio`) makes the Qt event loop BE the asyncio loop — the inverse of today's `_poll()` hack (`gui.py:2404`), killing the freeze-prone single-loop drain without adding threads [qasync](https://pypi.org/project/qasync/).
- Native `QSystemTrayIcon` **replaces pystray + PyGObject + pywin32** tray plumbing — drops three runtime deps.
- Packaging reuses the existing PyInstaller pipeline (`build.spec`); official Qt-for-Python PyInstaller guide exists [Qt PyInstaller](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html).
- The web-grade "Farmer look" is achievable in QWidgets+QSS (rounded corners, gradients, token sheets) **without dragging in Chromium/QWebEngineView** [PythonGUIs FAQ](https://www.pythonguis.com/faq/html-css-and-js-in-a-desktop-app-qt-webengine-vs-electron-vs/).

**Runner-up pywebview** (in-process Python backend, real HTML/CSS/JS = closest literal match to Farmer's frontend) but rejected as primary for three divergent platform webviews (WebView2/WKWebView/WebKitGTK), no built-in tray, and weaker 24/7-daemon reliability [pywebview web engine](https://pywebview.flowrl.com/guide/web_engine).

**Fallback if Qt is refused:** CustomTkinter (v6.0.0, 2026-06) is the low-risk refresh that keeps Tkinter, but it is a refresh, not the requested rewrite [CustomTkinter](https://github.com/tomschimansky/customtkinter).

---

## 2. Architecture of the rewrite

```
                    ┌─────────────────────── TwitchDropsMiner process ───────────────────────┐
  Twitch API/WS      │   Python 3 + asyncio                    Qt 6 (PySide6)                │
  (twitch.py,        │                                        ┌──────────────────────────┐    │
   websocket.py,     │   Twitch client ──gui──▶  GUIProtocol  │  Qt Application (QWidgets)│    │
   channel/inventory)│        │ (unchanged calls:             │   Dashboard / Channels /  │    │
                    │        │  status, tray, channels,       │   Drops / Inventory /     │    │
                    │        │  progress, inv, print, save…)  │   Settings / Activity /   │    │
                    │        ▼                                │   QSS dark theme          │    │
                    │   loop = qasync.QEventLoop (Qt IS the asyncio loop)                   │
                    │   backend coroutines + Qt slots share one cooperative main thread     │
                    │   tray = QSystemTrayIcon (native)                                    │
                    └──────────────────────────────────────────────────────────────────────┘
```

### 2.1 The `GUIProtocol` (the binding contract — replaces Farmer's Wails bindings)

Today the backend calls the GUI **directly** through object attributes: `self.gui.status.update(...)`, `self.gui.tray.change_icon(...)`, `self.gui.channels.*`, `self.gui.progress.*`, `self.gui.inv.*`, `self.gui.print(...)`, `self.gui.save(...)`, `self.gui.websockets.update(...)`, `self.gui.login.ask_login(...)`, `self.gui.set_games(...)`, `self.gui.display_drop(...)` (call sites enumerated in [02-BACKEND-SURFACE](02-BACKEND-SURFACE.md), §0/§3, with `twitch.py:line` refs).

**Task:** define a `typing.Protocol` (or abstract base) capturing exactly these members. The new Qt `GUIManager` implements it. **This is the acceptance checklist** — every method in the protocol is a feature the new UI must surface. Because UI and backend share one process/one language, there is **no binding layer** (simpler than Wails' generated TS proxies) [Wails how-does-it-work](https://wails.io/docs/howdoesitwork/).

> ⚠️ **#1 integration risk:** the backend and `gui.py` have a **bidirectional import cycle** (`twitch.py:19,448`) and ~20 direct synchronous UI callouts inside backend hot paths. The new `GUIManager` should break the cycle by implementing the protocol (backend importable without the UI layer). See [02-BACKEND-SURFACE](02-BACKEND-SURFACE.md) §5.

### 2.2 Event-loop swap (the crux)

- **Today:** Tk events drained by `_poll()` → `root.dooneevent(DONT_WAIT)` + `await asyncio.sleep(0.05)` (`gui.py:2404-2429`); Wayland freeze fixed by `XMODIFIERS="@im=none"` (`main.py:45-47`) [fixed in `3aec843`].
- **Rewrite:** `qasync.QEventLoop()` makes Qt the asyncio loop. Backend coroutines (aiohttp, websockets) and Qt slots tick cooperatively on one thread. No polling, no `threading`/`QThread`/queues. Keep single-threaded cooperative model exactly as today so **zero data races** (there are none today — report [05] documents this).
- **Async UI⇄backend calls** (`wait_for_login_press`, `ask_login`, `coro_unless_closed` at `gui.py:2378`) map 1:1 to asyncio `Future`s; `coro_unless_closed` survives unchanged.
- **Any real thread** (e.g. future native tray work) must round-trip via `loop.call_soon_threadsafe` exactly as today (`gui.py:1143`).

### 2.3 Concurrency MUST / MUST-NOT (from [05-ASYNC-THREADING])

1. MUST keep backend and UI on **one cooperative thread** (hard rule; removes an entire class of races).
2. MUST NOT perform network I/O or long pure-Python work on the UI/gui critical path in one coroutine that also does heavy layout (today's `add_campaign` does serialized aiohttp image fetch + layout in one pass — a freeze spot). Split expensive pure work off the UI path with `create_task` as done at `twitch.py:1438,1508` and / or offload CPU-bound work (today's image decode at `cache.py:117-161`) to an executor.
3. MUST keep maintenance reloads **non-blocking** (the exact `3aec843` bug class).
4. MUST add the repo's first tests — pin the concurrency contract before retiring `gui.py` (repo has **zero tests** today; report [05] §5 lists 8 testing gaps).
5. MUST NOT ship a Rust/Go rewrite via Wails/Tauri just to get the look (kills backend reuse).

---

## 3. The target design system

Full token set, rationale, WCAG values, and reconciled Farmer↔TDM tables are in [08-UX-DESIGN-TOKENS](08-UX-DESIGN-TOKENS.md). Extract — copy Farmer verbatim where noted ([03] lists exact `App.css:line` for every value).

### 3.1 Canonical palette (adopt Farmer's `:root` verbatim)

`--bg #090d14` · `--surface #101722` · `--surface-2 #141d2a` · `--surface-3 #192333` · `--border rgba(255,255,255,.075)` · `--text #f3f6fb` · `--muted #8995a8` · `--subtle #5f6b7e` · **`--accent #9b7cff`** · `--accent-2 #6e4bff` · `--green #55d6a8` · `--amber #f1b86a` ([03] §2.1).

**Extended status tokens (new for TDM, all pass WCAG 2.2 4.5:1 on `--bg`):** `--error #e86f6f` (≈7.6:1) · `--idle #8995a8` (≈6:1) · `--offline #5f6b7e` (≈3.9:1, tertiary tier) · `--info #7fb4e8` (≈7.1:1). **Verify with a contrast tool at implementation time** ([08] §1.3 → [WCAG 2.2](https://www.w3.org/TR/WCAG22/)).

### 3.2 Key decisions

- **Accent = periwinkle `#9b7cff`** (Farmer) for app-wide consistency; **Twitch Purple `#9146FF` reserved** for the Twitch-connected/account badge and the primary "Go live / Watch" CTA as brand signal ([08] §1.8; [Twitch Beyond Purple](https://blog.twitch.tv/en/2019/12/03/beyond-purple/)). Do not reproduce Twitch logo marks ([Twitch trademark](https://legal.twitch.com/en/legal/trademark/)).
- **Typography:** adopt Farmer's `Inter, ui-sans-serif, …` stack; BUT **floor readable text at 12px** (Farmer's 7–9px micro text is below legibility floor; use `rem` so OS scaling works) ([08] §1.4; [Apple HIG Typography](https://developer.apple.com/design/human-interface-guidelines/typography)).
- **Spacing:** 4px base / 8px rhythm scale `--space-1..12` ([08] §1.5).
- **Radii/shadows/motion:** adopt Farmer's `8/10-14/16-17/999px` radii, card/modal/accent shadows, `0.16s ease` transitions, `1.6s` pulse, and **`prefers-reduced-motion: reduce`** policy (WCAG 2.2 SC 2.3.3) ([08] §1.6).
- **Tray-first "AFK 24/7" mindset:** default resting state = system tray; window is a passive live-status surface; toasts (non-blocking) for routine news; modals only for blocking decisions; stable layout (no scroll-jump) ([08] §4.4).

### 3.3 Information architecture: Farmer nav → TDM pages

| Farmer nav | TDM page | Replaces TDM tab |
| --- | --- | --- |
| Overview | **Dashboard** (hero: current campaign + drop progress; active-watch bar; metrics) | Main |
| Channels | **Channels** (river of `ChannelRow`) | Main (channels region) |
| Streaks & Weekly | **Weekly** (per-campaign timers; **new** surface) | — |
| Campaigns | **Drops/Campaigns** (scan-summary + `CampaignCard` grid; **new** discovery UI) | Inventory (campaigns) + Main (progress) |
| Collection | **Inventory** (`OpportunityRow` list + filters) | Inventory |
| Activity | **Activity** (structured feed + collapsible raw log) | Help (log) |
| Settings | **Settings** (General / Account / Priority&Exclude / Advanced) | Settings |
| (Help) | **Help/About** folded into top-bar action or footer | Help |

Sidebar (224px, `aria-current`) + single scroll `.main`; TopBar = eyebrow + `h1` + health dot + **account button** (login lives here as the account modal, Farmer pattern) ([03] §5, [08] §2.1).

### 3.4 Component inventory (framework-mapped)

Sidebar · TopBar · HealthDot/StatusDot (never color-only) · StatusBadge pills · ChannelRow+ChannelAvatar · CampaignCard+DropProgressBar (`<progress aria-valuenow>`) · RewardRow/OpportunityRow · Button primaries (gradient+glow), secondaries, ghosts, danger · MonitorBar · ScanSummary/MetricCard · EmptyState (Carbon-style) · Loading/Skeleton · AccountModal (connect / pending device-code / connected; focus trap) · generic Modal · Toast · SettingsSection · SelectCombobox · Switch toggle · ActivityRow · SidebarFooter. Full specs in [08] §3 and [03] §3. Preserve Farmer's patterns: **seatbelt stale-response guard** (`useRef` request-ID), button-label mutation ("Syncing…"), `role="alert"`/`role="status"` banners, auto-resuming pending-action after re-auth ([03] §4, to be ported to Qt signals/state).

---

## 4. Current-state pain points the rewrite fixes

From [01-UI-CURRENT-STATE](01-UI-CURRENT-STATE.md) §4 — all of these are addressed by the target system:

- Fixed non-scaling geometry / magic-number pixel layout / no design tokens → token QSS + responsive layout.
- Non-scrollable Settings panel; fragile manual canvas in Inventory → virtualized/scrollable modern lists.
- Freeze-prone single-thread Tk drain → Qt-as-asyncio.
- Unbounded console growth → bounded structured log.
- **Linux scroll-wheel broken in Inventory** (only `MouseWheel` bound; `Button-4/5` missing) → use a proper scrollable widget.
- **Accessibility:** color-only states, emoji-as-UI (⇈➖❌✔🎁), no keyboard targets, low-contrast raw `green/goldenrod/red` strings, high-DPI gaps → a11y-first a11y components, WCAG 2.2 compliance, real `<progress>`/roles (Qt widgets fix most of this by being native).
- Hard-coded untranslated strings (Help tab, `gui.py:2103-2150`; "Language … requires restart" `gui.py:1650`) → route through `translate.py`.

**Preservation contract (must-keep checklist)** is enumerated in [01] §5 — every item (4 tabs→pages, login+validation, live progress bars, channel table+switch, priority list, exclude list, priority mode, inventory filters+refresh, console/timestamps, settings incl. autostart all 3 OSes + dark mode + tray-notifications + proxy + advanced toggles + reload, tray 5-state icon + tooltip + notifications + minimize, Help + invalidate token, websocket status panel, CLI flags, single-instance, shutdown-safe close) is a checkbox for Phase 0/5.

**Suggested cut list** in [01] §6 (e.g. `_TKOutputHandler`, the custom ttk-workaround primitives, the `1<<1` drain hack, the `gui.py` GUI-debug harness ~240 lines, DWM title-bar hack).

---

## 5. i18n & settings (from [06])

- **Keep `translate.py` untouched**: `_(ns, key)` dotted lookup, `merge_json` back-fill from English default, named `str.format` variables, **20 locale JSON files** (`lang/`). See [06-I18N-SETTINGS](06-I18N-SETTINGS.md) for the full key namespace and table.
- **Gap to fix:** `set_language` is only called at startup (`main.py:141`); the Settings selector is **restart-only in practice** (`gui.py:1632-1638`), though the translator already supports in-process switching. The rewrite should wire live language switching (and set `QLocale`/layout direction for **Arabic RTL** + verify CJK font coverage).
- **Settings schema:** keep `settings.json` field-for-field (verified in [06] §3 and [02] state model). Add schema **versioning** going forward (gap today). Preserve `Settings._altered`/`force` save semantics and the delayed save-at-exit behavior.
- **New UI strings** must be added to the English default (`translate.py` default dict + regenerated `lang/*.json`) so all locales stay canonical.

---

## 6. Build & packaging plan (from [07])

- **Pipeline today:** PyInstaller one-file windowed (`build.spec`), `build.sh/.bat` + `pack.bat` + `setup_env.*`, AppImage (Python-into-`AppDir`, not frozen), CI matrix = Win/macOS/Linux PyInstaller ×2 arch + Linux AppImage ×2 arch → `dev-build` prerelease. Tray via pystray executor thread, 5 `.ico` states, AppIndicator bundling on Linux, macOS special-cased.
- **For PySide6/Qt (recommended):** update `build.spec` with Qt platform plugins + QSS `datas` + keep `lang/*.json` and icons; consider `--onedir` for debuggability first, add a **frozen-build smoke test per OS** in CI [Qt PyInstaller](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html) · [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html). Artifact grows (~100–250 MB if WebEngine is pulled in — **avoid QWebEngineView**; keep it QWidgets and it stays lean).
- Remove `pystray`, `PyGObject` (Linux), and (Win) `pywin32` tray deps from `requirements.txt`.
- **⚠️ Pre-existing gap to fix:** macOS codesign/notarization is **not set up** — required before shipping a signed Qt binary to macOS.

---

## 7. Phased roadmap

1. **Phase 0 — freeze the `GUIProtocol`** (no code). Enumerate every `self.gui.*` member from call sites; cross-check against [01] §5 preservation checklist. This is the acceptance contract. *(½ day)*
2. **Phase 1 — spike the shell.** Minimal PySide6 + `qasync` app implementing **Login + StatusBar + Tray** on the protocol; prove the Qt-as-asyncio loop works end-to-end with the real aiohttp/websocket stack; reuse `translate.py` + `settings.py` unchanged. `gui.py` untouched. *(1–2 days)*
3. **Phase 2 — port screens** in dependency order: **Dashboard → Channels → Drops/Campaigns → Inventory → Settings → Activity → Help**. One shared QSS token file (per [08]). Keep `dark_mode` toggle (now semantic-token swap). *(majority of effort)*
4. **Phase 3 — swap tray:** `QSystemTrayIcon` + `QMenu`, port 5 icon states + live-progress tooltip + notification gate; drop pystray deps. *(½ day)*
5. **Phase 4 — retire `gui.py` + Tk:** delete `gui.py` and `_poll()`; switch `main.py` composition root to Qt GUIManager; drop tkinter imports; update `build.spec`; re-run CI + AppImage; wire macOS notarization.
6. **Phase 5 — parity sign-off:** walk the Phase-0 protocol checklist; verify all 20 locales render (Arabic RTL + CJK); soak-test the 24/7 AFK loop; add tests (repo has zero today) — concurrency contract first ([05]).

---

## 8. Risk register (top items)

| # | Risk | Likelihood/Impact | Mitigation |
| --- | --- | --- | --- |
| 1 | Qt+PyInstaller packaging drift (plugins/QSS/datas missing, size) | Med/High | Official PyInstaller guide; `--onedir` first; per-OS frozen smoke test [07] |
| 2 | asyncio/Qt loop integration bugs (loop ownership, signal thread safety) | Med/High | `qasync.QEventLoop` as **the** loop from startup; single cooperative thread; `call_soon_threadsafe` for real threads [05] |
| 3 | LGPL compliance slip | Low/Med | Ship PySide6 as separate shared libs; include LGPLv3 notice [04] |
| 4 | i18n/RTL & locale regressions (Arabic RTL, CJK) | Med/Med | Keep keys; set `QLocale`/layout direction; per-locale smoke [06] |
| 5 | Feature-parity drift (countdown timer, ws-shard status, autostart, tray notif) | Med/High | Phase-0 protocol = binding checklist; parity tests [01][02] |
| 6 | macOS notarization (pre-existing) | Med/Med | Add signing/notarization pipeline in Phase 4 [07] |
| 7 | UI freeze on CPU-bound work / maintenance reload | Med/High | offload decode; non-blocking reload (the `3aec843` class) [05] |

---

## 9. Where the detail lives

- **01** Current-state audit + preservation contract + cut list — `docs/ui-review/01-UI-CURRENT-STATE.md`
- **02** Backend service surface + `GUIProtocol` mapping + health — `docs/ui-review/02-BACKEND-SURFACE.md`
- **03** Farmer UI translation spec (tokens, components, interaction patterns, screen map) — `docs/ui-review/03-FARMER-UI-REFERENCE.md`
- **04** Stack decision matrix + integration + migration + risk — `docs/ui-review/04-STACK-RECOMMENDATION.md`
- **05** Concurrency/threading model + constraints + testing gaps — `docs/ui-review/05-ASYNC-THREADING.md`
- **06** i18n mechanism + settings schema + hard-coded-string audit — `docs/ui-review/06-I18N-SETTINGS.md`
- **07** Build/packaging pipeline + UI-stack impact — `docs/ui-review/07-BUILD-PACKAGING.md`
- **08** Design tokens + wireframes + component inventory + a11y checklist — `docs/ui-review/08-UX-DESIGN-TOKENS.md`

**Next useful action:** approve the PySide6/Qt + `qasync` recommendation, then begin **Phase 0** (enumerate the `GUIProtocol` from `gui.py`/`twitch.py` call sites). This is documentation-only; **no source code was modified** (git shows only `docs/` untracked).
