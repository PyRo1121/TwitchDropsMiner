# STATUS — ui-audit

Complete. Output: `docs/ui-review/01-UI-CURRENT-STATE.md` (8 sections + exec summary).

Audited the full existing UI: `gui.py` (2,971L) read end-to-end, plus `main.py`, `settings.py`, `translate.py`, `constants.py`, `cache.py`, relevant `twitch.py`/`utils.py`, and the README screenshots. No source modified.

Key findings: single-threaded Tk+asyncio shared loop (no mainloop/.after; `_poll` dooneevent drain), only OS thread is the pystray tray. 4 tabs (Main/Inventory/Settings/Help), tray, login, priority/exclude lists, live progress, websocket status, autostart all three OSes. Documented freezes (Wayland fix 3aec843), Linux wheel-broken, unbounded console, non-scaling fixed geometry, accessibility gaps (color-only states, emoji glyphs, no keyboard target), stylistic token sprawl. Produced a concrete preservation contract (checkbox list) and a cut list (debug harness, ttk-workaround primitives, imperative theming).
