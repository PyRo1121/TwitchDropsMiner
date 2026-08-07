# BRIEF-05 — Async & Threading Model Audit

**Output file: `docs/ui-review/05-ASYNC-THREADING.md`** (overwrite).

## Mission

Document exactly how **concurrency works today** in TwitchDropsMiner and what constraints the rewrite must honor so the new UI never freezes, races, or deadlocks. Background: recent commit `3aec843 "Fix UI freeze during maintenance reloads on Wayland/Linux (#1114)"` — the team has fought these bugs before.

## Scope

- `gui.py` — read fully, focus on:
  - How the Tk mainloop and the asyncio event loop coexist (`.after()` polling? `asyncio.run_coroutine_threadsafe`? threads?). Find the actual mechanism with line refs.
  - Worker threads (websocket reader, login, reload), queue usage (`queue.Queue`? `asyncio.Queue`?), and UI marshaling.
  - Places where the UI blocks on network I/O or `run_sync`-style calls (freeze candidates).
- `websocket.py` — sharding model, per-shard tasks, event fan-out.
- `twitch.py` — session/connection lifecycle, reconnect logic, rate limiting, how long operations run.
- `channel.py` — switching logic timing, race conditions between websocket events and channel state.
- `main.py` — startup order, task group / exception handling, shutdown.

## Deliverables

1. **Concurrency architecture diagram** (text/ASCII) — threads, loops, queues, and every cross-boundary handoff, with file:line refs.
2. **Event flow catalog** — every event type (stream up/down, viewer counts, drop progress, campaign changes, maintenance) → producer → path → UI consumer.
3. **Freeze/race audit** — concrete list of sites where UI responsiveness is at risk, with reasoning; note which are fixed by 3aec843 and which remain.
4. **Rewrite constraints** — explicit "the new UI must/must-not" rules (e.g. "never touch asyncio from UI thread without `run_coroutine_threadsafe`", "websocket shards must stay out of UI thread", "long reloads must be cancellable").
5. **Testing gaps** — what concurrency behavior has no test coverage and should be pinned before the rewrite.

## Rules

- Quote `file:line` for every claim.
- Do NOT modify source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-05.md` and stop.
