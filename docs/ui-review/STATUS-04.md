# STATUS-04 — Stack Recommendation (2026-08-05)

Review complete. Verified the backend (Python 3 + aiohttp + asyncio, tightly coupled to the GUI via a `self.gui.*` object interface) and confirmed the GUI is already driven by asyncio inside a Tk `_poll()` 50 ms drain hook (gui.py:2404).

**Recommendation: PySide6 / Qt 6 (QWidgets + QSS dark theme + QSystemTrayIcon), bridged to asyncio via qasync (or QtAsyncio).** Runner-up: pywebview. Rejected honestly: Wails/Tauri (would force porting/proxying the 6k-LOC Python backend), Flet/NiceGUI/Reflex (no first-class tray/daemon), and CustomTkinter (a refresh, not the requested rewrite).

All claims cited to 2025/2026 sources (accessed 2026-08-05). Delivered: decision matrix (8 stacks × 7 criteria), recommendation, in-process async-bridge architecture sketch (Qt loop IS the asyncio loop, replacing the Tk _poll hack), 6-phase migration path that leaves the backend untouched and retires gui.py last, and a 5-item risk register. No source code modified.
