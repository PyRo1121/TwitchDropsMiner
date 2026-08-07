# 01 — UI Current-State Audit (TwitchDropsMiner)

**Agent:** ui-audit · **Brief:** `docs/ui-review/BRIEF-01-ui-audit.md`
**Scope:** existing Tkinter UI at HEAD (`a3fda85`). ~8,171 LOC / 14 Python modules; `gui.py` = 2,971 lines.
All code claims are cited `file:line`. No source was modified.

---

## 0. Executive summary

TwitchDropsMiner's entire UI is a **single-file, single-threaded Tkinter application** (`gui.py`, 2,971 lines) glued to an **asyncio backend** (`twitch.py`) that shares the same event loop. There is **no `mainloop()` and no `.after()`** anywhere: the Tk event loop is driven by a cooperative `await asyncio.sleep(0.05)` poll task (`gui.py:2404-2429`), so Tk and asyncio live on one thread and all background→UI delivery happens as plain coroutine calls. The only real OS thread is the **pystray** tray icon (`gui.py:1154`), bridged via `loop.call_soon_threadsafe`.

The app is a functional, mature, actively-maintained miner; the UI is dense, label-heavy, and built purely from stock ttk/tk widgets with a hand-rolled dark palette applied via `ttk.Style`/`option_add`. The README screenshots (image IDs `164298155`, i.e. April 2022) show the **old light theme**, long before the dark-mode/`clam` palette existed, so they materially under-represent the current look.

---

## 1. Screen / component inventory

### 1.1 Top-level composition (`GUIManager.__init__`, `gui.py:2208-2334`)

- `Tk` root frame with 8px padding + a `ttk.Notebook` (4 tabs) + the tray "Minimize to Tray" button overlaid at the top-right (`gui.py:2265-2266`, `TrayIcon` grid at `gui.py:1131-1133`).
- `root.minsize()` is clamped to the widget-requested size after `update_idletasks` (`gui.py:2290`), i.e. the layout can only grow.
- ESC anywhere unfocuses tree/listbox selection (`gui.py:2449-2453`, `unfocus`).
- Window title from `constants.py:WINDOW_TITLE` = `f"Twitch Drops Miner v{__version__} (by DevilXD)"`.
- Windows-specific Win32 `WM_CLOSE`/`WM_QUERYENDSESSION` handling via a subclassed `wnd_proc` + `ShutdownBlockReasonCreate` (`gui.py:2342-2360`, `gui.py:2309-2324`); other platforms use `WM_DELETE_WINDOW`/`WM_DESTROY_WINDOW` (`gui.py:2325-2329`).

### 1.2 Tab 0 — Main (`gui.py:2269-2278`)

| Region | Class / lines | Purpose | Key widgets | Data source / callback |
| --- | --- | --- | --- | --- |
| Status bar | `StatusBar` `437-448` | one-line app/state message ("Idle", "Switching the channel…", "Fetching inventory…", "Exiting…") | `ttk.LabelFrame` + `ttk.Label` | `gui.status.update(...)` called from `twitch.py` state machine (`twitch.py:640,695,735,844,869,882…`) |
| Websocket status | `WebsocketStatus` `456-517` | per-connection status + topic counts for up to `MAX_WEBSOCKETS`(=8) connections | monospace `ttk.Label`s (`MS.TLabel`), one line per ws id | `gui.websockets.update(idx,status,topics)` (websocket layer) |
| Login form | `LoginForm` `527-613` | username/password/2FA login | 3 `PlaceholderEntry` (username, password masked `•`, optional 2FA), status label showing "Status: / User ID:", `ttk.Button` | async `ask_login` / `ask_enter_code` awaited by backend (`twitch.py` auth); results via `asyncio.Event` `_confirm` (`gui.py:552-569`) |
| Campaign progress | `CampaignProgress` `636-798` | live progress of currently-mined campaign + drop; two progress bars, % and HH:MM:SS countdown via a `_timer_loop` coroutine counting seconds | `ttk.Progressbar`(determinate, `length=420`), `StringVar`/`DoubleVar` sets, `MouseOverLabel` | `gui.display_drop()` (`2479`); vars driven by `TimedDrop`/`DropsCampaign` `progress`/`remaining_minutes` |
| Console output | `ConsoleOutput` `801-844` | monotony log of every status/print line | `tk.Text` (wrap=none, disabled, H+V scrollbars) | `gui.print()`; `logging.Handler` `_TKOutputHandler` `74-80` routes `logging`→`print`; timestamps each line `strftime("%X")` |
| Channel list | `ChannelList` `852-1078` | table of channels with online/offline/pending, game, drops-enabled, viewer count, ACL-based flags; **Switch** button | `ttk.Treeview` with dynamic columns, per-column auto width, "watching" row highlight (`gray70`), `ttk.Button` | `gui.channels.display/set_watching/clear/remove` (`twitch.py` channel fetch + watch loop); Switch → `twitch.state_change(State.CHANNEL_SWITCH)` (`gui.py:874`); manual switch reads `get_selection` (`twitch.py:848`) |
| — layout | — | Main tab grid: status(0,0) / ws(1,0) / login(1,1) / progress(0-1,2) / channels(2,1span2) / output(0-2,3, expandable) | grid geometry | `ConsoleOutput` row carries the lone `weight=1` expander (`gui.py:814-816`) |

### 1.3 Tab 1 — Inventory (`InventoryOverview` `1235-1545`)

- **Filter strip** (`gui.py:1264-1321`): five `ttk.Checkbutton` filters — Not linked / Upcoming / Expired / Excluded / Finished — plus a **Refresh** `ttk.Button`. Filter defaults: not_linked = `priority_mode is PRIORITY_ONLY`, upcoming=1, others=0 (`gui.py:1255-1264`).
- **Scroll canvas** (`gui.py:1322-1362`): a `tk.Canvas` + inner frame + both scrollbars; mouse-wheel bound only while the pointer is over the canvas (`gui.py:1344-1346`), shift-wheel = horizontal scroll (`gui.py:1383-1390`).
- **Campaign cards** (`add_campaign` `1392-1494`): one `ttk.Frame` per campaign (`relief="ridge"`) containing — title, colored status (`green`/`goldenrod`/`red`, `get_status` `1357-1367`), a `MouseOverLabel` swapping **Ends/Starts** on hover (`1452-1462`), a `LinkLabel` for the campaign link page + linked/not-linked state, "Allowed Channels" list (first N + "and X more…", `1470-1495`), the Twitch **box-art image** (108×144) fetched async via `ImageCache` (`1476-1482`), and one drop sub-card per drop, each with **benefit reward images** (80×80) and a progress caption (`update_progress` `1502-1539`).
- Data source: `ImageCache` (`cache.py`) for all images (disk-cached, hashed LRU, asyncio lock); campaigns from backend `fetch_inventory` → `gui.inv.add_campaign` tasks (`twitch.py:1508`); progress updates pushed via `gui.inv.update_drop` (`twitch.py` watch loop / `_update_drop`).

### 1.4 Tab 2 — Settings (`SettingsPanel` `1572-2087`)

- **General** LabelFrame (`gui.py:1697-1780`): language `SelectCombobox` ("requires restart", sets `settings.language`), autostart checkbox (+ `update_autostart`), autostart-into-tray checkbox (non-macOS), tray-notifications checkbox (non-macOS), dark-mode checkbox (`update_dark_mode` → `apply_theme`), priority-mode `SelectCombobox` (3 modes from `PRIORITY_MODES`, `gui.py:1577-1586`), proxy `PlaceholderEntry` with focusout URL validation (`proxy_validate` `1548-1557`, prefill `http://`).
- **Advanced** LabelFrame (`gui.py:1780-1816`): red "Warning!" banner + goldenrod warning text; "enable_badges_emotes" and "available_drops_check" toggles.
- **Priority** LabelFrame (`gui.py:1816-1882`): a `PlaceholderCombobox` game picker (options fed from `set_games`), `PaddedListbox` of ordered priority games, plus arrow buttons ⇈/↑/↓/⇊ to reorder and ➕/❌ to add/remove; writes to `settings.priority` + `settings.alter()` (`priority_add/move/delete` `1975-2037`).
- **Exclude** LabelFrame (`gui.py:1882-1917`): same combobox+listbox pattern, alphabetical, writes `settings.exclude` (`exclude_add/delete` `2046-2087`).
- **Reload** strip (`gui.py:1917-1927`): "Most changes require a reload…" + **Reload** button → `twitch.state_change(State.INVENTORY_FETCH)`.

### 1.5 Tab 3 — Help (`HelpTab` `2090-2200`)

- About (author, repo, donate links), Useful links (Twitch inventory/campaigns), "How It Works" and "Getting Started" static text (`wraplength=800`, `gui.py:2091`), and an **Invalidate** token button (`gui.py:2173-2200`) → revokes OAuth token and restarts the state machine (`State.RESTART`).
- Several labels are **hard-coded English** ("About", "Application created by:", "Repository:", "Donate:", the donate sentence) and **not translated** (`gui.py:2103-2144`) unlike the rest.

### 1.6 Tray (`TrayIcon` `1081-1205`)

- pystray `Icon` with 5 icon states: `pickaxe/active/idle/error/maint` (`gui.py:1088-1094`, files under `icons/`).
- Runs in a separate OS thread via `loop.run_in_executor(None, self.icon.run)` (`gui.py:1154`); menu callbacks ("Show", "Quit") bridge back with `loop.call_soon_threadsafe` (`gui.py:1152`).
- Multi-line tooltip title showing game/reward/progress, computed with a shorten algorithm (`get_title` `1112-1135`).
- Desktop notifications `notify()` gated on `settings.tray_notifications` (`gui.py:1179-1194`), auto-removed after `duration`.
- `minimize()` withdraws the root win; `restore()` deiconifies; macOS hides the tray button and tray minimize (`gui.py:1131`, `gui.py:1164-1177`).

### 1.7 Custom widget primitives (reused, `gui.py:74-429`)

`PlaceholderEntry`/`PlaceholderCombobox`, `PaddedListbox` (listbox in a themed frame for padding), `MouseOverLabel` (hover alt-text swap), `LinkLabel` (webopen on click), `SelectMenu`/`SelectCombobox` (option menus).

---

## 2. Flow maps

### 2.1 Boot / wiring (`main.py:1-208`)

1. `freeze_support()`; validate Python ≥3.10; set `XMODIFIERS="@im=none"` on Linux (`main.py:45-47`) to avoid XWayland/Mutter lockups.
2. Create a **hidden dummy** `tk.Tk()` to render argparse errors in a messagebox, parse args (`main.py:96-162`): `--version`, `-v`, `--tray`, `--log`, `--dump`, hidden `--debug-ws`/`--debug-gql`. Destroy dummy window.
3. `Settings(args)` (`main.py:163-172`), error → messagebox, exit 4.
4. `asyncio.run(main())`; single-instance via `lock_file(LOCK_PATH)` (`main.py:199-207`, exit 3 if held).
5. Inside async `main()`: set language, configure logging, build `Twitch(settings)`, install SIGINT/SIGTERM handlers (Linux), `await client.run()`, handle `CaptchaRequired`/fatal errors (prevent close to show traceback), then `shutdown()`, `wait_until_closed()`, final `save(force=True)`, `gui.stop()`+`close_window()`, `sys.exit`.
6. **`--tray`** flag + `settings.autostart_tray` both feed the "autostart into tray" checkbox; at startup if `settings.tray` and not macOS, the window starts hidden by triggering tray minimize via `_root.after_idle(self.tray.minimize)` (`gui.py:2331-2334`).

### 2.2 Login flow

1. `Twitch._run()` → `get_auth()`; if no stored token, backend calls `gui.login.ask_login()` (async, `gui.py:571-596`) — sets "Login required", ensures window visible via `grab_attention()`.
2. User fills username/password/optional 2FA, presses **Login** (defers to `twitch.state_change`-style; actually `self._button.command = self._confirm.set`, `gui.py:551`). `wait_for_login_press()` clears the asyncio event and awaits it (`gui.py:563-569`); button disables during wait.
3. Input validation at `gui.py:576-590`: username 3–25 chars, ascii+underscore; password ≥8; optional 2FA ≥6; invalid → clear that field and re-prompt in a loop.
4. Credentials passed to backend auth (form-based login). If 2FA/Chrome device flow needed, `ask_enter_code()` shows the code, `webopen(page_url)` after a 4s pause (`gui.py:598-606`).
5. On success `login.update("Logged in", user_id)` (`gui.py:608-613`); user_id shown in the status label.

### 2.3 Mining lifecycle (state machine, `twitch.py:617-1040`)

`State` enum (`constants.py:224-233`): `IDLE → INVENTORY_FETCH → GAMES_UPDATE → CHANNELS_FETCH → CHANNEL_SWITCH → CHANNELS_CLEANUP → RESTART → EXIT`.

1. **INVENTORY_FETCH**: `fetch_inventory()` (claims claimable drops, `twitch.py:1490-1530`), `gui.set_games(...)`, save state.
2. **GAMES_UPDATE**: claim drops from expired/active campaigns (`twitch.py:655-658`), sort campaigns per priority mode (`ENDING_SOONEST`/`LOW_AVBL_FIRST`/`PRIORITY_ONLY`).
3. **CHANNELS_FETCH**: gather candidate channels (`gui.channels.clear()` + `display(...,add=True)`), set up websocket topics.
4. **CHANNEL_SWITCH**: pick best channel (priority/ACL logic `should_switch` `twitch.py:1033-1062`), `watch(channel)` → `gui.channels.set_watching`, `gui.tray.change_icon("active")`, `gui.status.update`, `gui.display_drop`.
5. **Watch loops** (`_watch_channel_loop`, `twitch.py`): each assigned target sends a `WATCH_INTERVAL` (59s) payload; `CurrentDrop` is used as an authoritative reconciliation source, with no synthetic progress fallback. Up to two targets are assigned only when games and Drop IDs are distinct; `gui.progress` remains the primary-target countdown.
6. **Maintenance** (`_maintenance_task` `twitch.py:985-1025`): hourly reload (`INVENTORY_FETCH`) and periodic channel cleanup (`CHANNELS_CLEANUP`).
7. **EXIT**: tray icon → "error" if not user-requested, `grab_attention` (bell + restore) to alert the user, then clean shutdown and settings-json save.

### 2.4 Settings persistence

- `settings.py` loads/saves `settings.json`; `Settings.__getattr__/__setattr__` proxy to `_settings` dict and set `_altered=True` on any write (`settings.py:53-88`).
- Most UI writes go through a checkbox `command=lambda: setattr(self._settings, "x", bool(...))` but the panel **only ever reads=** from settings-at-construction except a few live `setattr`s (language, dark_mode via `update_dark_mode`, proxy via `proxy_validate`, priority/exclude via `alter()`).
- **Save timing:** `Settings.save()` only writes if `_altered or force`. On normal exit `client.gui.save(force=True)` + `settings.save(force=True)` run **after** `wait_until_closed()` deliberately (`main.py:116` comment) so last-minute edits persist. There is no auto-save-on-change and no debounce.
- Autostart is written to the OS registry (Windows `HKCU/.../Run`, `SettingsPanel.AUTOSTART_KEY` `gui.py:1573`), a `~/.config/autostart/*.desktop` file (Linux), or a macOS LaunchAgent plist, with per-OS queries/builders (`gui.py:1860-1958`).

### 2.5 Tray flow

Minimize button / startup `--tray` → `tray.minimize()` (starts pystray thread if not started, then `_root.withdraw()`). Tray menu "Show" → `restore()` (deiconify, hide icon). "Quit" → `manager.close()` → `State.EXIT`. Icon state reflects mining state; title shows current drop progress; notifications announced on claimed drops.

---

## 3. Event / threading model as it exists today

```
                        ┌──────────────────────────────────────────────┐
                        │              ONE THREAD (main)               │
                        │                                              │
   main.py  asyncio.run(main())                                       │
        │                                                            │
        ▼                                                            │
   asyncio loop ────────────────────────────────────────────────►   │
        │  Twitch backend (twitch.py: state machine, watch loop,     │
        │  websocket, aiohttp, image fetch)                          │
        │        │                                                   │
        │        │  calls gui.X(...) DIRECTLY (same-thread, no       │
        │        │  queue, no thread-safety needed)                  │
        ▼        ▼                                                   │
   GUIManager._poll()  (gui.py:2404):                                │
        while True:                                                  │
            while root.dooneevent(DONT_WAIT): pass   # drain Tk      │
            await asyncio.sleep(0.05)                # yield 50 ms   │
        └── because _poll is an asyncio task on the SAME loop,       │
            Tk events and backend coroutines interleave cooperatively│
                                                                    │
   SEPARATE OS THREAD #2: pystray tray icon (gui.py:1154)           │
        loop.run_in_executor(None, icon.run)                        │
        └── thread → main via loop.call_soon_threadsafe             │
            (Show/Quit menu bridge, gui.py:1152)                    │
                                                                    │
   Windows-only native hooks: win32 subclassed wnd_proc (WM_CLOSE,  │
   WM_QUERYENDSESSION, WM_DESTROY) (gui.py:2342-2360)               │
                                                                    │
   No mainloop(), no .after(), no threading beyond pystray.         │
   Async↔UI handoff points that ARE explicit awaits / events:      │
      • Login button: asyncio.Event (gui.py:552)                    │
      • close request: asyncio.Event _close_requested (gui.py:2300) │
      • inv.add_campaign: async (awaits ImageCache.get)             │
        └── image loads use asyncio.Lock + aiohttp (cache.py:68)    │
   Where UI can BLOCK: any long sync work in a coroutine that does  │
      heavy Tk layout (e.g. InventoryOverview.add_campaign does     │
      cached aiohttp + layout in one pass) or a Tcl native call     │
      (the reason _poll uses TKINTER_DONT_WAIT + XMODIFIERS=@im=none)
```

**Key correctness points**

- Because everything shares one loop, **there is no thread-safety problem at all** between backend and UI — the tradeoff is that a long/hanging coroutine (network, layout) blocks the whole UI = freeze risk. The `3aec843` fix specifically reworked `_poll` from `root.update()` to a non-blocking `dooneevent(DONT_WAIT)` drain plus `XMODIFIERS=@im=none` to stop Tcl/Tk blocking inside X11/Wayland IM during heavy redraws (`gui.py:2404-2429`, `main.py:45-47`).
- `coro_unless_closed()` races a user coroutine against the close event so a blocked login can't wedge shutdown (`gui.py:2378-2388`).
- Fragmenting inventory work: campaigns/available-drops are split into `asyncio.create_task` chunks (`twitch.py:1438,1508,1594,1618`) to keep the loop responsive.

---

## 4. Pain points & bugs (from code reading)

### Layout / geometry

1. **Fixed, cramped geometry.** Window cannot shrink below its required size; only the console row is `weight=1` expandable (`gui.py:814-816`), so most screens lock to a fixed pixel box (matches the 909×686 screenshots). No proportional columns except the channels table column 2 (`gui.py:857`). Progressbar hard-coded `length=420` (`gui.py:637`).
2. **Many magic numbers and pixel widths** scattered inline: `width=45` title `1468`, `108×144` boxart `1478`, `80×80` rewards `1486`, `width=37` proxy `1778`, `LABEL_SPACING=20` `1268`, arrow-button `weight_scale=5` `1830`. No design-token system; theming is imperative `configure` calls.
3. **Settings panel is not scrollable** — with long priority/exclude lists or on small/high-DPI displays the tab overflows with no scroll affordance (content centered in a fixed frame, `gui.py:1622-1624`).
4. Inventory uses a **canvas that must be manually re-parented / scrollregion-recomputed** (`_canvas_update` `1379`), a known-fragile Tk pattern; no virtual viewport, so hundreds of campaigns = hundreds of live widget objects.

### Freeze / responsiveness

5. **Single-threaded coupling** (Section 3): any hang in a backend coroutine (network retry during maintenance reload) freezes the UI. This is the exact class fixed by `3aec843` for Wayland, but the underlying model is inherently freeze-prone — e.g. `add_campaign` does serialized aiohttp image fetches + layout in one coroutine (`gui.py:1392-1494`).
2. `_poll` drains **all** pending Tk events in a tight loop before sleeping; a burst of `grid`-heavy updates can starve the 0.05s yield and spin CPU.
3. Wheeling only scrolls when the pointer is inside the inventory canvas, and **only the `MouseWheel` (Windows/macOS) binding is installed** — Linux X11 `Button-4/Button-5` wheel events are not handled (`gui.py:1344-1346,1383-1390`), so scroll-wheel in the inventory is broken on many Linux setups.

### Accessibility

8. **No keyboard navigation target** beyond default tab-focus; treeview/listbox rely on mouse selection; many interactive elements (`MouseOverLabel` alt-text, `LinkLabel`) are `takefocus=False` or have no accessible semantics.
2. **Hard-coded color strings** for semantic states (`"green"`, `"goldenrod"`, `"red"`, `"gray70"` watching row `gui.py:896,1371,1425,1567,1752`) are passed as `foreground` — poor contrast in dark mode and invisible to screen readers (no text alternative for ✔/❌/🎁 glyphs either, `gui.py:918,929,1418`).
3. **Emoji as UI:** button glyphs ⇈ ↑ ↓ ⇊ ➕ ❌ and table checkmarks ✔/❌/🎁/📋 render font/OS-dependently and have no labels/tooltips (`gui.py:877,918,929,1823-1900`, `translate.py:gui.inventory.status`).
4. **High-DPI:** no explicit DPI-scaling strategy; relies on Tk's default. Fonts resized only via `nametofont` copies at fixed sizes 10/16 (`gui.py:2544-2559`), not DPI-aware.
5. **Hard-coded untranslated strings** in the Help tab (`gui.py:2103-2150`) mixed with translated ones; also the "Language 🌐 (requires restart)" label is hard-coded English (`gui.py:1650`).

### String / styling
 1. Colors chosen at widget-build time are **not re-themable live** unless explicitly in `apply_theme`; several labels set `foreground="green"/"red"/"goldenrod"` at creation and are left that way (they survive because Tk keeps them, but toggling dark mode re-runs `apply_theme` which does *not* touch those leaf labels — inconsistent).
 2. The treeview "watching" highlight `gray70` (`gui.py:896`) is a light-mode color that is dark-mode-unfriendly.

### Scalability / data
 1. `ChannelList` does **manual per-cell width measurement + redraw** on every update (`_adjust_width`, `_redraw` via `event_generate("<<ThemeChanged>>")`, `gui.py:963-975`); O(rows×cols) `font.measure` calls each refresh.
 2. `ConsoleOutput` appends unbounded history with **no line cap** (`gui.py:825-834`) — long mining sessions grow the text widget memory without bound.
 3. Inventory rebuilds **all campaign frames** on every refresh (`clear()` destroys all children then re-adds, `gui.py:1496-1500`) — no diff/patch; expensive with many campaigns.
 4. Integer overflow guard for ws topic counts uses `DIGITS = ceil(log10(WS_TOPICS_LIMIT))` (`gui.py:57`), fine but implicit.

### Platform quirks
 1. macOS disables the tray button and `--tray` minimize (`gui.py:1131,1165`), and uses an AppKit appearance swap rather than ttk theming (`gui.py:2530-2538`) — meaning macOS and the Linux/Windows `clam` theme diverge visually.
 2. Windows title-bar color via DWM hack `_set_title_bar_color` (`gui.py:2483-2503`, `DWMWA_CAPTION_COLOR=35`) is brittle and silently no-ops if DWM fails.

---

## 5. Preservation contract (every feature the rewrite MUST keep)

- [ ] 4-tab notebook: **Main / Inventory / Settings / Help** (`gui.py:2265-2283`).
- [ ] **Login flow** with username, masked password, optional 2FA; inline validation (username 3–25 ascii, password ≥8, 2FA ≥6); status + user-id display; device-code/webopen flow (`gui.py:527-613`).
- [ ] **Live campaign progress**: two bars (campaign + current drop), percent, HH:MM:SS countdown synchronized to backend (`CampaignProgress`).
- [ ] **Channel table**: channel/status/game/drops/viewers/ACL columns; ONLINE/OFFLINE/PENDING states; "watching" highlight; manual **Switch** button honoring the treeview selection (`gui.py:852-1078`).
- [ ] **Priority list** ordered game list with reorder (top/up/down/bottom) + add/remove, persisted to `settings.priority` (`gui.py:1975-2037`).
- [ ] **Exclude list** (unordered, alphabetical) persisted to `settings.exclude` (`gui.py:2046-2087`).
- [ ] **Priority mode** selector (Priority-only / Ending soonest / Low-availability first) persisted to `settings.priority_mode` (`gui.py:2039-2044`).
- [ ] **Inventory overview** with the 5 filters (Not linked / Upcoming / Expired / Excluded / Finished), Refresh, per-campaign box-art, per-drop reward images, per-drop progress captions, campaign LinkLabel with linked/not-linked state, hover Starts/Ends (`InventoryOverview`).
- [ ] **Console/output** log with timestamps and auto-scroll (`ConsoleOutput`).
- [ ] **Settings**: language selector, dark-mode toggle, proxy URL with validation, autostart (+into-tray), tray-notifications, advanced badges/emotes + available-drops-check toggles, **Reload** button (`SettingsPanel`).
- [ ] **Tray icon**: 5 mining-state icons, dynamic multi-line progress title, Show/Quit menu, desktop notifications gated by `settings.tray_notifications`, minimize-to-tray (`TrayIcon`).
- [ ] **Help tab**: About/links/how-it-works/getting-started + **Invalidate token** (logout) action (`HelpTab`).
- [ ] **Autostart persistence** on all three OSes (Windows registry, Linux `.desktop`, macOS LaunchAgent plist).
- [ ] **Dark/light theme** toggling that recolors all surfaces, text, selection, scrollbars, combobox dropdowns, treeview, progress bar, and (Windows) title bar (`apply_theme`).
- [ ] **Websocket status** panel (per-connection state + topic counts, up to 8).
- [ ] Single-instance lock, `--tray`, `--log`, `--dump`, `-v` verbosity CLI flags.
- [ ] Statusbar reflecting state-machine activity (idle/switching/fetching/exiting/terminated).
- [ ] Windows shutdown-safe close (`WM_QUERYENDSESSION` + `ShutdownBlockReason`) and prevent-close-to-show-traceback behavior.

---

## 6. Suggested cut list (safe to drop in the rewrite)

1. **The `_TKOutputHandler` logging→Text redirection and `ConsoleOutput`'s unbounded text** — replace with a bounded ring-buffer/structured log panel or external log viewer. (Feature can live on, but unbounded growth should die.)
2. **`PaddedListbox`, `MouseOverLabel`, `PlaceholderEntry`, `SelectMenu`, `SelectCombobox`, `_fixed_map` workarounds, `_disable_column_resize`** — all hacks compensating stock-ttk limitations; a React/Wails (or any modern) toolkit makes them obsolete (these live in `gui.py:63-429,2337-2349,948-967`).
3. **Imperative `ttk.Style`/`option_add` theming + Tk option-database hacks + `DWMWA_CAPTION_COLOR` DWM hack** (`apply_theme` `gui.py:2505-2708`) — one central token/stylesheet system replaces it.
4. **The `1 << 1` `TKINTER_DONT_WAIT` drain-loop `_poll` and `XMODIFIERS=@im=none` workaround** (`gui.py:2404-2429`, `main.py:45-47`) — a real UI thread or a framework with its own loop removes the need to hand-drive Tk from asyncio.
5. **`gui.py` `if __name__ == "__main__"` GUI-debug harness (`create_game/create_channel/create_drop/mock settings`)** — dev-only throwaway; the rewrite needs proper fixtures/component tests instead (`gui.py:2721-2958` — ~240 of the 2,971 lines).
6. **Dead/marginally-used constants** surfaced as UI strings in `translate.py` (e.g. unused `GUITray.notification_title`, unused websocket statuses like `disconnecting/reconnecting`) and unused `SelectMenu` (no production caller found) — confirm before dropping.
7. **The operational `log.txt` chatty DEBUG-to-console coupling** — move behind a log-level gate in the rewrite (the `minute_almost_done`/GQL-fallback machinery at `twitch.py:910-980` is backend logic, not UI, so it stays but must be decoupled from timers the UI thinks it owns).

---

## 7. Screenshots (README "Pictures") — honest visual description

The three images (`README.md:30-32`; GitHub user-images URLs with ID `164298155…`, i.e. April 2022) are all **909×686** and all show the **light default palette**:

- Dominant background ≈ `#f0f0f0` (the exact light `bg` in `apply_theme`, `gui.py:2540`), with white `#ffffff` surfaces/panels — matching light-mode `surface=#ffffff`.
- **Main** shows a white panel interior with grey LabelFrame borders, a native two-row progress bar, a Treeview channel list, and a dark-on-white console — the classic pre-theming stock-ttk look.
- **Inventory** is mostly light-grey with a single campaign card (the brownish `(72,27,15)` sample is campaign box-art), confirming the card layout.
- **Settings** is a large light-grey field (the 240,240,240 sample dominates) with sparse controls — reflecting the fixed non-scaling centering.

**Caveat:** these screenshots predate the dark-mode/`clam` theme, `TKINTER_DONT_WAIT`, autostart, invalidate-button, and several UI tweaks. They represent the **legacy light aesthetic only** and should not be treated as the current look. No current-version screenshots ship in the repo.

---

## 8. Sources & verification

- Code read in full: `gui.py` (2,971L), `main.py` (208L), `settings.py` (101L), `translate.py` (510L), `constants.py` (511L), `cache.py` (default_translation), plus targeted regions of `twitch.py`, `utils.py`.
- Commit `3aec843` (Wayland maintenance-reload freeze fix) inspected via `git show`.
- Screenshots fetched from README and analyzed (pixel color / brightness) — model can't render images, so description is programmatic + code-inferred.
- No source files modified.

**Remaining risk:** this is a timing/async audit from static reading; behavioral freeze claims (Section 4.5-4.7) are inferred from the single-loop model and the `3aec843` commit message, not reproduced at runtime.
