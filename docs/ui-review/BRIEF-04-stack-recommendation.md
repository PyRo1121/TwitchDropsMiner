# BRIEF-04 — UI Stack Recommendation (2026, citation-based)

**Output file: `docs/ui-review/04-STACK-RECOMMENDATION.md`** (overwrite).

## Mission

Recommend the **best UI stack for the TwitchDropsMiner rewrite** and defend it with **cited 2026-standard sources**. This is the decision document the whole rewrite hinges on.

## Constraints (from the codebase — verify yourself)

- Existing backend: **Python 3** + aiohttp + asyncio (Twitch API client, websocket sharding, caching). Non-trivial (~6k LOC backend). Reusing it saves enormous effort.
- Existing app is Tkinter; features: system tray (pystray), login via browser (twitch.tv auth), i18n (14 languages), PyInstaller packaging (build.spec), Windows + Linux + macOS targets, low bandwidth usage, "run 24/7 AFK" nature.
- Reference app (Farmer) uses **Wails v2 (Go) + React 19 + Vite 7** and is the look the user wants.

## Research task (citation-based)

Compare, for THIS app, with 2025/2026 sources (official docs, release notes, benchmarks, migration guides, active maintenance evidence):

1. **PySide6 / Qt 6** (Python; native widgets or QML) — async integration with asyncio, tray support, packaging, licensing.
2. **Wails v2/v3** (Go + web frontend) — would require either porting the Python backend to Go or proxying; evaluate honestly.
3. **Tauri v2** (Rust + web frontend) — same Python-backend problem.
4. **pywebview** (Python + HTML/JS) — thin wrapper, keeps Python backend.
5. **Flet / NiceGUI / Reflex** (Python web-framework UIs) — fit for a long-running tray daemon app?
6. Also assess: keeping Tkinter + theming, or **Tkinter → CustomTkinter**.

Scoring criteria: effort to reuse Python backend, 2026 ecosystem health (maintenance, docs), desktop UX quality (dark theme, high-DPI, responsiveness), tray + background-daemon friendliness, packaging impact (from BRIEF-07's domain), i18n support, learning/upkeep.

## Deliverables

1. **Decision matrix** — stack × criteria, scored, with citations per row.
2. **Recommendation** — one primary pick + one runner-up; 3–5 sentence justification.
3. **Integration architecture sketch** — how the chosen UI layer talks to the existing Python backend (in-process bridge, async bridge, localhost service?). Reference Farmer's binding pattern.
4. **Migration path** — staged plan: what to build first, how the backend stays untouched, how to retire gui.py.
5. **Risk register** — top 5 risks of the chosen stack with mitigations.

## Rules

- **Every external claim MUST have a citation** with URL + accessed date; prefer official docs and 2026-dated sources. Inline links like `[title](url)`.
- Do NOT modify source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-04.md` and stop.
