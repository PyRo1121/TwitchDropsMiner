# BRIEF-05 — Async & Threading Model Audit

**Agent:** `async-threading`
**Scope files:** `gui.py`, `websocket.py`, `twitch.py`, `channel.py`, `main.py`
**Baseline commit:** `3aec843` "Fix UI freeze during maintenance reloads on Wayland/Linux (#1114)"
**Purpose:** Document how concurrency works today so the rewrite never freezes, races, or deadlocks.

---

## 0. Executive summary

TwitchDropsMiner is **single-threaded asyncio**. There is exactly **one OS thread** (the
pystray tray-icon thread), **zero** `queue.Queue`/`asyncio.Queue` instances, zero
`threading.Thread`, and **zero** `asyncio.to_thread`/`run_in_executor` for work off-loading
(the only `run_in_executor` is the tray icon, `gui.py:1154`). The Tkinter event loop is **not
run by `mainloop()`** — it is *emulated* on the asyncio loop by a polling task that drains Tk
events every `0.05s` via `dooneevent(DONT_WAIT)` (`gui.py:2395-2430`). Everything — Tk redraws,
GQL HTTP, websocket I/O, state transitions — shares one event loop in one thread, so it is
**concurrency-safe by construction** (no locks needed for UI marshaling), but it is also
structurally **non-concurrent**: any long synchronous stretch blocks both the UI and the
network simultaneously. The rewrite must preserve the no-thread model OR introduce a proper
UI◄→async bridge; it must never mix the two half-heartedly.

---

## 1. Concurrency architecture diagram

```
                    ┌────────────────────────────── SINGLE THREAD ──────────────────────────────┐
  main.py:206       │                                                                            │
  asyncio.run(main())                                                                           │
   │                                                                                            │
   ▼                                                                                            │
 ┌───────────────────────────────── asyncio EVENT LOOP (main thread) ─────────────────────────┐ │
 │                                                                                            │ │
 │  Twitch.run() -> _run()  [twitch.py:592 / 608]  — one giant state machine                   │ │
 │        │  gui.start()  twitch.py:608                                                       │ │
 │        ▼                                                                                   │ │
 │   GUIManager._poll()  [gui.py:2395-2430]   ★ Tk "mainloop" emulation                       │ │
 │        loop: while dooneevent(DONT_WAIT): pass   → yields every 0.05s                      │ │
 │              await asyncio.sleep(0.05)            → Tk and HTTP cooperate on one loop      │ │
 │                                                                                            │ │
 │   ┌──────────┬──────────────┬───────────────┬─────────────────┬───────────────────────┐    │ │
 │   ▼          ▼              ▼               ▼                 ▼                       ▼    │ │
 │  WATCH      MAINT       WEBSOCKET         GQL/REQ          TRAY-bridge             IMAGES   │ │
 │  task       task        pool (≤8)         limiter          (call_soon_              [cache] │ │
 │  [895]      [961]       shards           [RateLimiter       threadsafe)              (loop) │ │
 │                        _handle_task      utils.py:348       gui.py:1141-1143               │ │
 │                        websocket.py:58   (cap=5, win=1)                                    │ │
 │                        per-shard         twitch.py:446                                     │ │
 │                        create_task :89          │                                          │ │
 │                                    ▲           │                                          │ │
 │   fan-out via create_task          │           │                                          │ │
 │   websocket.py:277                 │           │                                          │ │
 │        │  topic(msg)               │           │                                          │ │
 │        ▼                           │           │                                          │ │
 │   process_drops / process_         │           │                                          │ │
 │   notifications / process_         │           │                                          │ │
 │   stream_state / stream_update     │           │                                          │ │
 │   (twitch.py:1052-1233)            │           │                                          │ │
 │        │  mutate Channel / drop / state events / gui.* (same thread, no marshaling)       │ │
 │        ▼                                                                                  │ │
 │   gui.widget updates          state event           ▲                                      │ │
 │   (display, status, inv)   twitch.py:438            │  tray thread only bridge            │ │
 │                                                       │                                   │ │
 └────────────────────────────────────────────────────────┼───────────────────────────────────┘ │
                                                          │  run_in_executor(None, icon.run)   │
                                                          │  gui.py:1154                        │
 ┌────────────────────────────────────────────────────────┴────────────────────────────────────┐
 │          OS THREAD #2 (default ThreadPoolExecutor) — pystray tray icon                       │
 │              call_soon_threadsafe(gui.py:1143) bridges menu/quit/restore BACK to the loop   │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Cross-boundary handoffs (every producer→consumer edge, with refs)

| # | Producer | Consumer | Handoff mechanism | Refs |
| --- | ---------- | ---------- | ------------------- | ------ |
| H1 | asyncio loop (any coroutine) | Tk widgets | Direct call — **same thread**, no queue; drained by `_poll()` | `gui.py:2395-2430` |
| H2 | pystray tray thread (menu click) | asyncio loop / Tk | `loop.call_soon_threadsafe` | `gui.py:1141-1143` (bridge), `1154` (`run_in_executor`) |
| H3 | asyncio loop | tray thread | `loop.run_in_executor(None, self.icon.run)` | `gui.py:1154` |
| H4 | Websocket recv loop (per shard) | topic handler coroutines | `asyncio.create_task(topic(...))` fire-and-forget fan-out | `websocket.py:277` |
| H5 | per-shard `_handle` | aiohttp ws connection | `_backoff_connect` async generator (reconnect w/ backoff) | `websocket.py:130-154` |
| H6 | WebsocketPool add/remove topics | per-shard subscribe state | shared `ws.topics` dict + `_topics_changed` Event | `websocket.py:318-345`, `346-402` |
| H7 | `on_channel_update` / maintenance / notifications / drops | state machine | `self._state_change.set()` Event | `twitch.py:438`, `533-539` |
| H8 | `watching_channel` setters | `_watch_loop` | `AwaitableValue` (Event-backed) `set/clear/get` | `utils.py:393-420`, `twitch.py:895-959`, `1033-1050` |
| H9 | Inventory/drop UI rebuild | cross-loop `add_campaign` tasks | `asyncio.create_task` + `asyncio.as_completed` + `await` | `twitch.py:1507-1523`, `gui.py:1479` |
| H10 | event handlers → UI | channel list | direct method call `channel.display()` | `gui.py:1033-1073`, `channel.py:368` |

**Meaning of the architecture:** because producer and consumer share the loop, there is **no
serialization problem** — but the flip side is that every `await` on the loop must return
promptly or the *whole app* (UI included) stalls. There is no mechanism to hide latency behind
a second thread.

---

## 2. Event flow catalog

Every inbound event → producer → marshaling path → UI consumer. All run on the main loop.

### 2.1 Stream state (`Channel.StreamState` topic)

- **Events:** `viewcount`, `stream-up`, `stream-down`, `commercial`
- **Producer:** websocket shard recv loop → fan-out task → `process_stream_state` (`twitch.py:1052`)
- **Path:** `process_stream_state` (`twitch.py:1052-1076`) → `channel.check_online()` (schedules 120s `_online_delay`) / `channel.set_offline()` / `channel.viewers = …; channel.display()`
- **UI consumer:** `ChannelList.display` (`gui.py:1033`), sets status/game/viewers/ACL columns
- **Notes:** `stream-up` arrives *before* stream metadata is queryable; `check_online` waits `ONLINE_DELAY=120s` (`constants.py:129`, `channel.py:383-391`) then re-fetches via GQL.

### 2.2 Stream update (`Channel.StreamUpdate` topic)

- **Events:** `broadcast_settings_update` (title/tags, may include `game`)
- **Producer:** websocket → `process_stream_update` (`twitch.py:1078`)
- **Path:** → `channel.check_online()` (introduces delay to coalesce rapid title/tag changes) → `update_stream()` → `on_channel_update` (`twitch.py:1105-1160`)
- **UI consumer:** `on_channel_update` drives `watch()` (status/tray/PRINT), `change_state(CHANNEL_SWITCH)`, and `channel.display()`

### 2.3 Drop progress / claim (`User.Drops` topic)

- **Events:** `drop-progress` (current/required minutes), `drop-claim`
- **Producer:** websocket → `process_drops` (`twitch.py`)
- **Path:** events are routed by assigned Drop ID, so two concurrent targets cannot update each other; `drop.update_minutes()` is authoritative-only and `drop.claim()` reassigns the target or refreshes inventory.
- **UI consumer:** `DropProgress.display`/`update_time` (progress bar, `gui.py:762`), inventory `update_drop` (`gui.py:1527`)
- **Note:** `drop-claim` does up to 8×GQL polling with `asyncio.sleep(2)` (`twitch.py:1199-1212`).

### 2.4 Notifications (`User.Notifications` topic)

- **Events:** `create-notification` (drop/emote/badge confirmations)
- **Producer:** websocket → `process_notifications` (`twitch.py:1220`)
- **Path:** triggers a full `INVENTORY_FETCH` reload + `NotificationsDelete` GQL (`twitch.py:1225-1233`)
- **UI consumer:** inventory rebuild during the reload (status bar `fetching_inventory`…)

### 2.5 Maintenance (time-driven, not websocket)

- **Producer:** `_maintenance_task` (`twitch.py:961`) restarted at end of each `fetch_inventory`
- **Path:** triggers `CHANNELS_CLEANUP` at campaign trigger times, `INVENTORY_FETCH` every ≤60 min (`twitch.py:980-989`)
- **UI consumer:** full inventory + channel list rebuild — **this is the "maintenance reload" path that froze the UI on Wayland/Linux** (fixed by `3aec843`).

### 2.6 Websocket protocol lifecycle (internal, no UI event)

- **Producer:** `_handle_recv` (`websocket.py:308-331`) — `MESSAGE`, `PONG`, `RESPONSE`, `RECONNECT`
- **Path:** `PONG` clears ping timeout, `RECONNECT` sets `_reconnect_requested`, `MESSAGE` → `_handle_message` fan-out (`websocket.py:276-283`)
- **UI consumer:** `WebsocketStatus.update` (`gui.py:487`) via `set_status` — connection state per shard.

---

## 3. Freeze / race audit

### 3.1 The freeze mechanism

Because Tk and asyncio share **one thread**, UI responsiveness is entirely a function of how
long the loop spends between two `_poll()` iterations **without yielding**. `_poll` drains all
pending Tk events in a tight synchronous loop before sleeping `0.05s` (`gui.py:2419-2425`). Two
classes of stall:

- **Type A — a Tk event callback blocks.** `dooneevent(DONT_WAIT)` returns after *one* event
  group, so between events `_poll` re-enters; but a single slow callback (e.g. a large Treeview
  rebuild, a canvas layout) blocks the loop for its duration.
- **Type B — a non-`await` CPU/IO section blocks the loop from other coroutines.** PIL
  decode/resize + `PhotoImage` construction (`cache.py:117-161`) run inline on the loop inside
  `add_campaign` (`gui.py:1464-1481`); network is awaited so it yields, but image decode does not.

### 3.2 What commit `3aec843` fixed (and how)

| Site | Pre-`3aec843` | Post-`3aec843` |
|------|---------------|----------------|
| Tk event pump | `self._root.update()` in the poll loop (`gui.py` old `_poll`) | `dooneevent(DONT_WAIT)` drain; never blocks inside a native Tcl call (`gui.py:2403-2424`) |
| Linux XIM/Wayland | X Input Method registration could deadlock native input contexts during heavy redraws | `main.py:44-48` sets `XMODIFIERS=@im=none` before Tk init when unset |

**Reasoning:** `root.update()` processes events **and** waits on idle-task processing; under a
heavy maintenance redraw on XWayland/Wayland it could enter a native input-context call and
hang, stalling `_poll` and thus the whole loop. `dooneevent(DONT_WAIT)` (flag `1<<1`) returns
immediately if the queue is empty, so `_poll` is guaranteed to reach `await asyncio.sleep(0.05)`
and hand control back to asyncio. **This fixes the *specific* Wayland freeze.** See the exact
diff in `3aec843` (both hunks shown above in §6).

### 3.3 Remaining freeze candidates (NOT fixed)

1. **Single-loop image decode** — `cache.py:117-161` (`PIL.open`/`load`/`resize`,
   `PhotoImage(...)`) are CPU-bound on the loop. A burst of uncached campaign/benefit images
   during `fetch_inventory` (`twitch.py:1507-1523`, `gui.py:1464-1481`) still blocks the UI for
   the aggregate decode time. **Not** addressed by `3aec843`.
2. **GIL-native Tk callbacks** — any single Tk widget callback that is slow (large inventory
   canvas relayout `_canvas_update`, `gui.py:1379`) blocks `_poll`'s drain for that callback's
   duration. No yielding inside Tk callback code exists.
3. **Synchronous state-machine chunks** — `_run`'s per-state blocks do `await` on GQL so they
   yield, but pure-Python loops (sorting `ordered_channels`, `twitch.py:792-820`) are brief; not
   a real freeze risk, listed for completeness.
4. **Login / OAuth** (`twitch.py:121-380`) and `get_spade_url` (HTML parse chain,
   `channel.py:250-274`) are all `await`ed → they yield to `_poll`, so only network latency
   affects the UI, not a hard freeze. Client-side regex/parse of a large streamer HTML page runs
   inline though.

### 3.4 Race audit

Because everything is single-threaded, **data races are impossible**; the remaining concern is
**ordering/logic races** between interleaved coroutines on the loop:

| # | Race | Sites | Mitigation present? |
| --- | ------ | ------- | -------------------- |
| R1 | `stream-up` vs `stream-down` vs `viewcount` arriving near-simultaneously | `check_online`/`set_offline` both mutate `_pending_stream_up` and `_stream` | Partial — `_pending_stream_up is None` guard (`channel.py:375`); `set_offline` cancels pending (`channel.py:401-404`). A stale `_online_delay` task re-fetches after cancel only if not yet cancelled. |
| R2 | Websocket event for a channel removed during `CHANNELS_CLEANUP` | `process_stream_state` reads `self.channels.get(channel_id)` | **Good** — returns early on `None` (`twitch.py:1057-1059`); topics also unsubscribed in cleanup (`twitch.py:742-751`, `833-842`). |
| R3 | `watch()`/`stop_watching()` vs `_watch_loop` `AwaitableValue.get()` | `twitch.py:897`, `1033-1050` | Event-backed value; `watch` while offline guarded by `channel.online` check (`twitch.py:897-900`). |
| R4 | Two writers to `watching_channel` (event handler triggers switch while state machine also switches) | `watch()` (multiple callers) vs `on_channel_update` vs `CHANNEL_SWITCH` | Serialized on the loop (single-threaded); but last-writer-wins ordering depends on scheduling — no lock, no explicit priority. |
| R5 | Maintenance `change_state(INVENTORY_FETCH)` fired while a `drop-claim` is mid-`await` polling | `twitch.py:1199`, `961-989` | Both merely set `_state_change`; state machine drains them sequentially — benign overlap, just extra work. |
| R6 | `websocket.add_topics` assigning to a `_handle_task` that is mid-`stop()` | `websocket.py:89`, `346-402` | `_state_lock` (`asyncio.Lock`, `websocket.py:44`) covers start/stop only; `add_topics`/`remove_topics` mutate `ws.topics` **without** the lock while `_handle_topics` reads them — safe only because single-threaded + set-before-clear ordering. Fragile if a thread is added later. |

**Bottom line for the rewrite:** the single-threaded model eliminates data races but the moment
the rewrite adds a background worker (as Farmer/Wails does) it must re-introduce explicit
synchronization at every H#-edge above, and per-shard `ws.topics` mutation (R6) is the first
place that breaks.

---

## 4. Rewrite constraints ("must / must-not")

1. **MUST-NOT touch Tk from any thread other than the main/UI thread.** Today everything is the
   main thread; the pystray thread only bridges via `call_soon_threadsafe` (`gui.py:1143`). If
   the rewrite adds worker threads, all Tk/widget mutations MUST go through a
   `run_coroutine_threadsafe` / `call_soon_threadsafe` bridge (the mirror of the existing tray
   bridge) — or better, keep Tk confined to the UI thread entirely.
2. **MUST preserve a drain-non-blocking UI pump.** Whatever replaces `_poll`, it must never call
   a blocking Tk/native call on the async loop for the window's lifetime. The
   `dooneevent(DONT_WAIT)` fix (`gui.py:2413-2424`) and the `XMODIFIERS=@im=none` guard
   (`main.py:44-48`) are correctness requirements on Linux/Wayland, not style points — carry
   them over or their WebView/embed equivalent.
3. **MUST keep websocket shards out of the UI thread.** Per-shard `_handle` + fan-out
   (`websocket.py:89`, `277`) are coroutines on the main loop today. In a threaded rewrite,
   shard I/O and its event fan-out MUST live on background tasks/threads; only the resulting
   UI mutations may come back over the bridge. Inspecting/mutating shared in-memory state
   (`ws.topics`, `twitch.channels`, `self._drops`) from multiple threads REQUIRES locks (see
   R6 — currently lock-free and only safe under single-threading).
4. **MUST make long reloads cancellable.** `fetch_inventory` is a long multi-`await`
   sequence (`twitch.py:1414-1535`) already cancellable via `ExitRequest` checks
   (`twitch.py:1518-1521`) and task-cancel on error. Any new UI must keep reloads as cancellable
   background work, and must not block the UI thread on `await fetch_inventory()`.
5. **MUST NOT run CPU-bound decode on the UI loop.** Image decode/resize/`PhotoImage`
   (`cache.py:117-161`) must be off-loaded (thread/bin in the rewrite) or done incrementally so
   a maintenance reload never stalls painting.
6. **MUST NOT assume cross-loop mutation is safe in a threaded design.** The current lock-free
   shared-state model (`channel._pending_stream_up`, `Twitch.channels`, `watching_channel`)
   is only valid because there is one thread. Re-platforming to a multi-process/multi-thread
   architecture (Farmer's Go+Wails) transfers all of R1–R6 into real data races unless bridged.
7. **MUST funnel every external event through one UI-visible channel.** Today all events arrive
   as coroutine calls on the loop (`process_*`, `websocket.py:277`). The rewrite should keep a
   single, well-typed event queue/stream between backend and UI so ordering (R4) stays
   deterministic and debuggable.

---

## 5. Testing gaps

There are **zero automated tests** in the repo (no `test_*` files, no pytest/unittest/tox).
The only executable harness is `gui.py:2846-2971` (`if __name__ == "__main__"`), a
single-threaded scripted demo of the Tk component, not a concurrency test. These behaviors have
**no coverage and should be pinned before the rewrite**:

1. **`_poll` pump correctness** — that `dooneevent(DONT_WAIT)` drains events, yields to asyncio
   every ≤~0.05s, and detects window destruction (`gui.py:2419-2427`). No test verifies the
   loop keeps serving coroutines during a simulated "heavy redraw" (regression target for
   3aec843).
2. **Websocket shard lifecycle** — reconnect/backoff (`websocket.py:130-154`), topic add/remove
   convergence (`_handle_topics`), per-shard fan-out task cancellation. No test simulates a
   dropped connection and asserts reconnect + resubscribe.
3. **State-machine ordering** — `run()`/`run()` reload and the `ReloadRequest`/`ExitRequest`
   unwinding (`twitch.py:592-605`, `880`) with a live `_poll_task`. No test asserts the GUI
   returns to service after a reload (the exact 3aec843 scenario).
4. **Channel state transitions under event interleaving** — R1 (out-of-order `stream-up` /
   `viewcount` / `stream-down`) and the 120s `_online_delay` (cancellation and re-trigger).
   No test pins `pending_online` → `online`/`offline` convergence.
5. **`watching_channel` handoff (R3/R4)** — `watch()` / `stop_watching()` / `_watch_loop`
   wake-up when the watched channel goes offline mid-sleep.
6. **Rate limiter** (`utils.py:348`, `twitch.py:446`) and retry/backoff loops under
   `fetched_inventory` concurrency — burst of GQL while a previous chunk is in flight.
7. **`call_soon_threadsafe` tray bridge (H2/H3)** — that tray menu actions invoked from the
   executor thread are dispatched to the loop and mutate Tk safely. Only manually exercised today.
8. **Cancellability** — long `fetch_inventory` / `add_campaign` aborted mid-flight on close
   (ExitRequest/cancel paths, `twitch.py:1518-1523`) without leaking orphan tasks.

---

## 6. Verification appendix

- Lines verified by direct read of current HEAD (post-`3aec843`).
- Commit `3aec843` diff (quoted, both hunks):
  - `gui.py` `_poll`: replaced `update = self._root.update` … `update()` with a
    `dooneevent(DONT_WAIT)` drain loop and documented `TKINTER_DONT_WAIT`.
  - `main.py`: added `os.environ["XMODIFIERS"] = "@im=none"` guard for Linux before Tk init.
- Confirmed no `queue.Queue`, no `asyncio.Queue`, no `threading`, no `asyncio.to_thread`; only
  `run_in_executor` is the tray icon (`gui.py:1154`).
- Confirmed `ONLINE_DELAY=120s` (`constants.py:129`), `WATCH_INTERVAL=59s`
  (`constants.py:130`), `PING_INTERVAL=3m`/`PING_TIMEOUT=10s` (`constants.py:127-128`),
  `MAX_WEBSOCKETS=8`/`WS_TOPICS_LIMIT=50` (`constants.py:119-120`).
