# BRIEF-02 — Backend Service Surface Audit

**Output file: `docs/ui-review/02-BACKEND-SURFACE.md`** (overwrite).

## Mission

Map the **non-UI backend** of TwitchDropsMiner so the rewrite team knows exactly which services/APIs the new UI must bind to. The backend is Python (aiohttp + asyncio) and is assumed to survive the UI rewrite; the UI is the consumer.

## Scope (read fully, quote file:line)

- `twitch.py` (1,642 lines) — the Twitch API client: classes, async methods, GQL queries/hashes, login flow (cookie jar), stream/drop/campaign data structures. List every public method/type the UI calls.
- `channel.py` (505) — channel model, lifecycle, switching logic.
- `websocket.py` (404) — sharded WS connection, events emitted, how events reach the UI.
- `inventory.py` (512) — inventory tracking, reward claiming.
- `cache.py` (142) — what is cached, formats, staleness.
- `registry.py` (112), `utils.py` (458), `exceptions.py` (94), `constants.py` (511) — supporting services the UI depends on.
- `main.py` (208) — composition root: what is constructed, what the UI receives.

## Deliverables

1. **Service map** — table: module → responsibility → key classes/functions (signatures) → consumed-by (gui.py:line refs where possible).
2. **UI consumption index** — grep `gui.py` for every `self.<backend>` / imported backend symbol; produce a "UI ⇄ backend" interface table: method → arguments → return shape → sync/async → callback/event.
3. **State model** — what state the app holds (channels, campaigns, drops, settings, inventory) and where (in-memory vs persisted).
4. **Backend health** — fragile/broken spots found during reading (broken hashes, dead code, error swallowing) — report only, don't fix.
5. **Stability risks for the rewrite** — e.g. tight coupling between gui.py and backend classes that a new UI layer would need to sever.

## Rules

- Quote `file:line` for every claim.
- Do NOT modify source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-02.md` and stop.
