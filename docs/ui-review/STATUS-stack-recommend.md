# STATUS — Stack Recommendation (2026-08-05)

Completed BRIEF-04 stack recommendation. Output: `docs/ui-review/04-STACK-RECOMMENDATION.md` (7 sections, ~2.3k words, all claims cited to 2025/2026 sources accessed 2026-08-05).

**Recommendation: PySide6 / Qt 6 (QWidgets + QSS + QSystemTrayIcon) with a qasync bridge — primary; pywebview — runner-up.** Wails/Tauri rejected (would port/proxy the 6k-LOC Python backend), Flet/NiceGUI/Reflex rejected (no tray/daemon fit), CustomTkinter is a low-risk fallback.

Key insight: the current GUI is asyncio-driven inside a Tk `_poll()` 50 ms drain (gui.py:2404); Qt+`qasync` makes the Qt event loop the asyncio loop — a cleaner inversion with zero backend change via a frozen `GUIProtocol`. Delivered decision matrix, architecture sketch, 6-phase migration path, and 5-item risk register. No source modified.
