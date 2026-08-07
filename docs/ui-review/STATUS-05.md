# STATUS — 05 (async-threading)

Completed. Wrote `docs/ui-review/05-ASYNC-THREADING.md` (report) per BRIEF-05. Audited
`gui.py`, `websocket.py`, `twitch.py`, `channel.py`, `main.py` at commit `3aec843`.

Findings: the app is single-threaded asyncio — one OS thread (pystray tray, `gui.py:1154`), no
queues, no threading; Tk is run by a `doonevent(DONT_WAIT)` drain loop inside `_poll()`
(`gui.py:2395-2430`) instead of `mainloop()`. `3aec843` fixed the Wayland freeze via that
non-blocking drain + `XMODIFIERS=@im=none`. Remaining freeze risks: CPU-bound image decode on
the loop (`cache.py:117-161`). No data races (single thread) but ordering races R1–R6 noted.
Report includes: ASCII architecture diagram (10 handoff edges), 6 event-flow catalogs, freeze
audit (fixed vs remaining), 7 rewrite MUST/MUST-NOT constraints, 8 testing gaps (repo has zero
tests). No source modified.
