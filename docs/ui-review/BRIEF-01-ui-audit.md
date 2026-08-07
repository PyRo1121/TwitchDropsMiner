# BRIEF-01 — UI Current-State Audit

**Output file: `docs/ui-review/01-UI-CURRENT-STATE.md`** (overwrite; be exhaustive, this is the primary input to the rewrite).

## Mission

Produce a complete, authoritative map of the **existing UI** of TwitchDropsMiner so the rewrite knows exactly what exists, what must be preserved, and what is broken.

## Scope

- `gui.py` (2,971 lines) — read it **fully**. Enumerate:
  - Every window / tab / frame / panel and its purpose.
  - Every widget type, data source, and callback that populates it.
  - All user flows: login, priority game list, channel list, channel switching, drop progress display, inventory, settings, reload, maintenance handling, tray icon interaction, manual channel switch.
  - The Tk mainloop ↔ asyncio loop integration (how background work reaches the UI; look for `.after()`, queues, threads, `asyncio.run` / loop management).
  - UI-facing constants and colors/geometry in `constants.py` (font sizes, colors, sizes).
- `main.py` — how the app boots, wires gui ↔ backend, handles args (e.g. `--minimize-to-tray`).
- `settings.py` — the settings schema the UI edits.
- `translate.py` + `lang/*.json` — how UI strings are produced (the UI's text layer).
- The three README screenshots (README.md "Pictures") — view them (fetch_content on the raw image URLs) and describe the current visual style honestly.

## Deliverables (sections in your report)

1. **Screen/component inventory** — table: UI region → purpose → key widgets → data source → notes.
2. **Flow maps** — login, mining lifecycle (start/switch/claim/stop), settings persistence, tray.
3. **Event/threading model as it exists today** — diagram in text; where the UI blocks, where background threads post to the UI.
4. **Pain points & bugs** — from code reading: layout, freeze risks (e.g. Wayland maintenance reload freeze fixed in commit 3aec843), scalability, string handling, accessibility (screen-reader, high-DPI, keyboard nav), look-and-feel.
5. **Preservation contract** — every feature the new UI MUST keep, listed as concrete checkboxes.
6. **Suggested cut list** — features that can die in the rewrite (e.g. dead code paths in gui.py).

## Rules

- Quote `file:line` for every claim about code.
- Do NOT modify source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-01.md` and stop.
