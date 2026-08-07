# 02 — Backend Service Surface Audit

**Scope:** Map the non-UI backend of TwitchDropsMiner so the rewrite team knows the exact surface a new UI layer must bind to. The backend is Python 3.10+ (aiohttp + asyncio) and is assumed to survive the UI rewrite; the UI is the consumer.

**Method:** full read of `twitch.py` (1,642 LOC), `channel.py` (505), `websocket.py` (404), `inventory.py` (512), `cache.py` (142), `registry.py` (112), `utils.py` (458), `exceptions.py` (94), `constants.py` (511), `settings.py` (101), and `main.py` (208); targeted reads of the consumers inside `gui.py` (2,971 LOC). Every claim is cited `file:line`. No source was modified.

---

## 1. Service map

| Module (LOC) | Responsibility | Key classes / functions (signatures) | Consumed by |
| --- | --- | --- | --- |
| `twitch.py` (1,642) | Composition + orchestration core: owns `Twitch` client, state machine, watch loop, GQL I/O, auth. | `class Twitch` (`twitch.py:433`); `run()` (`:592`), `_run()` (`:608`) — main state machine; `get_session()` (`:463`), `shutdown()` (`:498`), `request()` (`:1240`), `gql_request()` (`:1298`), `fetch_inventory()` (`:1414`), `fetch_campaigns()` (`:1395`), `get_live_streams()` (`:1553`), `bulk_check_online()` (`:1582`), `watch()`/`stop_watching()` (`:1033`,`:1042`), `can_watch()`/`should_switch()` (`:992`,`:1014`), `change_state()`/`state_change()` (`:533`,`:539`), `get_auth()` (`:1235`), `save()` (`:564`), `print()` (`:558`); `class _AuthState` (`:80`) | `gui.py` (`twitch.gui.*`), `channel.py`, `websocket.py`, `inventory.py`, `main.py` |
| `channel.py` (505) | Channel + stream model, online/offline state, spade (watch) payloads, spawn URL. | `class Channel` (`:161`) — `display/remove` (`:293`,`:296`), `check_online` (`:395`), `set_offline` (`:412`), `update_stream` (`:376`), `external_update` (`:335`), `send_watch` (`:484`), `get_stream` (`:349`), factories `from_acl` (`:191`)/`from_directory` (`:201`); `class Stream` (`:28`) | `twitch.py`, `inventory.py` (`Channel.from_acl`), `gui.py` (`ChannelList`) |
| `websocket.py` (404) | Sharded pubsub WS pool, topic lifecycle, event dispatch → UI. | `class Websocket` (`:35`), `class WebsocketPool` (`:265`) — `add_topics`/`remove_topics` (`:401`,`:300`), `start`/`stop`; `_handle_recv`/`_handle_message` (`:223`,`:236`) route `MESSAGE` → topic callback | `twitch.py` (`Twitch.websocket`), `gui.py` (`WebsocketStatus`) |
| `inventory.py` (512) | Campaign / drop model, claim logic, progress math. | `class DropsCampaign` (`:340`), `class TimedDrop(BaseDrop)` (`:218`), `class BaseDrop` (`:58`), `class Benefit` (`:43`), `class BenefitType` (`:33`) | `twitch.py`, `gui.py` (`InventoryOverview`), `channel.py` (`_check_drops_enabled`) |
| `cache.py` (142) | Disk + memory image cache (perceptual hash dedup), TTL 7 days. | `class ImageCache` (`:44`) — `get(url, size)` (`:127`), `save(force)` (`:93`); hash algo `_hash` (`:107`) | `gui.py` (`InventoryOverview` via `manager._cache`) |
| `registry.py` (112) | Windows registry key wrapper (autostart). | `class RegistryKey` (`:21`), `class Access`, `class MainKey`, `class ValueType` | `gui.py` (`SettingsPanel._query_autostart`/`update_autostart`, Windows only) |
| `utils.py` (458) | Shared helpers: backoff, rate limiter, awaitable slot, task wrapper, JSON/state serialization, `Game`. | `ExponentialBackoff` (`:282`), `RateLimiter` (`:334`), `AwaitableValue` (`:392`), `task_wrapper` (`:183`), `json_load`/`json_save` (`:259`,`:277`), `lock_file` (`:72`), `class Game` (`:429`) | all modules |
| `exceptions.py` (94) | Exception hierarchy. | `MinerException` (base), `ExitRequest`, `ReloadRequest`, `RequestException`, `RequestInvalid`, `WebsocketClosed`, `LoginException`, `CaptchaRequired`, `GQLException` | all modules; `main.py` catches `CaptchaRequired` (`:171`) |
| `constants.py` (511) | Paths, enums, GQL persisted-query hashes, WS topic map, tuning constants. | `State` (`:389`), `PriorityMode` (`:408`), `ClientType` (`:467`+), `GQL_QUERIES` (`:463`), `WebsocketTopic`/`WEBSOCKET_TOPICS` (`:486`,`:499`), `MAX_*` limits (`:491`+) | all modules |
| `settings.py` (101) | Typed settings load/save with arg-overlay semantics. | `class Settings` (`:49`) — `__getattr__`/`__setattr__` (`:64`,`:73`), `alter()` (`:91`), `save(force)` (`:93`); `SettingsFile`/`default_settings` (`:14`,`:29`) | `twitch.py`, `gui.py`, `main.py` |
| `main.py` (208) | Composition root: CLI parse, lock, `Twitch(settings)`, run/shutdown, save-on-exit. | `async def main()` (`:120`) — `client = Twitch(settings)` (`:159`), calls `client.gui.close()` (`:164`), `client.shutdown()` (`:182`), `client.save(force=True)` (`:194`), `client.gui.stop()`/`close_window()` (`:195`,`:196`) | — (entry point) |

**N.B. `gui.py` is imported by the backend, not the reverse:** `twitch.py` does `from gui import GUIManager` (`twitch.py:19`) and instantiates it in `Twitch.__init__` via `self.gui = GUIManager(self)` (`twitch.py:448`). `channel.py` couples to the GUI directly with `self._gui_channels = twitch.gui.channels` (`channel.py:177`), and `websocket.py` with `self._twitch.gui.websockets` (`websocket.py:43`). This **bidirectional** dependency is the core coupling the rewrite must sever (see §5).

---

## 2. UI consumption index

### 2a. Backend → UI calls (backend is the active caller)

The `Twitch` object owns a `self.gui: GUIManager` and a `self.websocket`; `GUIManager` exposes these sub-components, all constructed once in `GUIManager.__init__` (`gui.py:2209`): `tabs`, `tray`, `status`, `websockets`, `login`, `progress`, `output`, `channels`, `inv`, `settings`, `help`.

| Caller (file:line) | Target GUI method | Args | Sync/Async | Purpose |
| --- | --- | --- | --- | --- |
| `twitch.py:448` | `GUIManager(self)` | `twitch` | sync | construct all UI |
| `twitch.py:664`,`:760` | `self.gui.tray.change_icon(state)` | `"idle"/"maint"/"active"/"error"/"pickaxe"` | sync | tray state |
| `twitch.py:481`,`:187`, `main.py:190` | `self.gui.close()`, `.wait_until_closed()`, `.grab_attention()` | — | sync / async | lifecycle |
| `twitch.py:512` | `self.gui.save(force)` | `bool` | sync | flush image cache |
| `twitch.py:558`,`:530` | `self.gui.print(msg)` / `wait_until_login()` | `str` | sync/async | console log |
| `twitch.py:608`,`:1387` `gui.py:866` | `gui.start()` / `gui.status.update(text)` | `str` | sync | poll loop / status bar |
| `twitch.py:1225`,`:999` etc. | `self.gui.coro_unless_closed(coro)` | `Awaitable` | async | abort on close |
| `twitch.py:1033`,`:1042` | `channels.set_watching(c)/clear_watching()` | `Channel` | sync | highlight row |
| `twitch.py:781`,`:822` | `channels.clear()`, `get_selection()` | — / → `Channel` | sync | channel list |
| `twitch.py:615` | `self.gui.set_games(set[Game])` | `set[Game]` | sync | game dropdowns |
| `twitch.py:1420`,`:1417` | `self.gui.inv.add_campaign(c)` / `.clear()` | `DropsCampaign` | async / sync | inventory tab |
| `twitch.py:468` `inventory.py:298` | `inv.update_drop(drop)` | `TimedDrop` | sync | live progress |
| `twitch.py:75`,`:472` | `self.gui.help._invalidate_button.config(...)` | — | sync | help tab button |
| `twitch.py:1132`,`:511` | `progress.stop_timer()` / `minute_almost_done()` | — → `bool` | sync | progress timer |
| `inventory.py:327` | `self._twitch.gui.display_drop(drop, countdown, subone)` | `TimedDrop` | sync | main-tab drop |
| `inventory.py:181` | `self._twitch.gui.tray.notify(text, title)` | `str,str` | async | tray toast |
| `websocket.py:73`,`:107` | `self._twitch.gui.websockets.update(idx,status,topics)` / `.remove(idx)` | `int,str,int` | sync | WS status widget |
| `twitch.py:81`,`:333`,`:606` | `self.gui.login` (`ask_login`, `ask_enter_code`, `update`, `clear`) | `LoginData` etc. | async | login form |

### 2b. UI → backend calls (UI presses backend)

Interface methods the GUI directly invokes (every `self._twitch.` hit in `gui.py` — the full set is small):

| Method | Arguments | Return shape | Sync/Async | Caller (gui.py:line) |
| --- | --- | --- | --- | --- |
| `Twitch.change_state(State)` | `State` | `None` | sync | `2200` (via `self._twitch.change_state(State.RESTART)`); `866` (via `state_change`) |
| `Twitch.state_change(State)` | `State` | `Callable[[],None]` | sync | `866` (Switch button `command`) |
| `Twitch.get_auth()` | — | `_AuthState` | async | `2187` |
| `Twitch.request(method,url,**kws)` | HTTP params | `AsyncIterator[ClientResponse]` | async | `2188` |
| `Twitch._client_type.CLIENT_ID` | — | `str` | sync (attr) | `2192` |
| `Twitch.get_priority/channel props` | via `Game` etc. | — | sync (attr) | throughout `ChannelList.display` `gui.py:1033` |
| `Twitch.close()` | — | `None` | sync | `2438` (`GUIManager.close`) |

**Backend types the UI reads as plain attributes** (the *de-facto* data contract the rewrite must preserve):

- `Channel`: `name` (`:231`), `iid` (`:241`), `online` (`:248`), `pending_online` (`:262`), `game` (`:271` → `Game.name`), `drops_enabled` (`:288`), `viewers` (`:277`), `acl_based` (`:167`) — all read in `ChannelList.display` `gui.py:1033-1073`.
- `DropsCampaign`: `name`, `active`/`upcoming`/`expired` (`inventory.py:381`,`:385`,`:389`), `eligible` (`:397`), `link_url`, `starts_at`/`ends_at`, `image_url`, `allowed_channels`, `drops`, `finished`, `required_minutes`, `game.name` — read in `InventoryOverview.add_campaign` `gui.py:1396-1494` and `_update_visibility` `gui.py:1327`.
- `TimedDrop`: `id`, `is_claimed`/`can_claim` (`inventory.py:143`), `current_minutes`/`required_minutes` (`:246`,`:250`), `progress` (`:274`), `starts_at`/`ends_at`, `benefits`, `can_earn()`, `rewards_text()` — read in `update_progress`/`update_drop` `gui.py:1501-1546`.
- `Benefit`: `name`, `image_url` (`inventory.py:46-56`) — read `gui.py:1466+`.
- `ImageCache.get(url,size)` → `PhotoImage` — read `gui.py:1437`,`:1466`.
- `Settings.*` attributes — read/written throughout `SettingsPanel` (`gui.py:1588+`) and `InventoryOverview` (`gui.py:1242`,`:1333`).

---

## 3. State model

**In-memory (all on `Twitch`, `twitch.py:434-458`):**

| State | Field | Type | Notes |
| --- | --- | --- | --- |
| Channels | `self.channels` (`:449`) | `OrderedDict[int,Channel]` | evicted/rebuilt each `CHANNELS_FETCH` cycle |
| Watching targets | `self.watching_channel`, `self._watching_channels`, `self._watch_drop_ids` | primary `AwaitableValue[Channel]` plus ordered channel/drop maps | up to two concurrent assignments; each game and Drop ID is unique |
| Inventory | `self.inventory` (`:438`) | `list[DropsCampaign]` | rebuilt on every inventory fetch |
| Drop index | `self._drops` (`:439`) | `dict[str,TimedDrop]` | id → drop |
| Campaign index | `self._campaigns` (`:440`) | `dict[str,DropsCampaign]` | id → campaign (used by `channel._check_drops_enabled`) |
| Selected games | `self.wanted_games` (`:437`) | `list[Game]` | ordered by priority |
| State machine | `self._state`/`self._state_change` (`:435-436`) | `State` enum + `asyncio.Event` | `State` values at `constants.py:389` |
| Maintenance triggers | `self._mnt_triggers` (`:441`) | `deque[datetime]` | schedule channel-cleanup/reload |
| Auth tokens | `_AuthState` attrs (`:86-91`) | `str/int` | volatile, derived from cookie jar |

**Persisted (disk):**

| Artifact | Location | Written by | Contents |
| --- | --- | --- | --- |
| `settings.json` | `SETTINGS_PATH` (`constants.py:109`) | `Settings.save` (`settings.py:93`); triggered by `Twitch.save` (`twitch.py:564`) and `main.py:194` | `SettingsFile` keys (`settings.py:14-28`): proxy, language, dark_mode, exclude, priority, autostart_tray, connection_quality, tray_notifications, enable_badges_emotes, available_drops_check, priority_mode |
| `cookies.jar` | `COOKIES_PATH` (`constants.py:108`) | aiohttp jar, saved on login & shutdown | session + auth cookies |
| `oauth.json` | `OAUTH_TOKEN_PATH` (`constants.py:110`) | `OAuthTokenStore` during device login/refresh | one rotated refresh token, permission-restricted |
| `cache/mapping.json` + `.png` | `CACHE_PATH`/`CACHE_DB` (`constants.py:106-107`) | `ImageCache.save` (`cache.py:93`) | URL → perceptual-hash → image, 7-day TTL (`cache.py:45`) |
| `dump.dat` | `DUMP_PATH` (`constants.py:105`) | `_run`/`fetch_inventory` when `--dump` (`twitch.py:605`,`:1479`) | debug campaign JSON |
| `log.txt` | `LOG_PATH` (`constants.py:103`) | logging FileHandler (`main.py:146`) | logs |

**State transience risk for the rewrite:** all gameplay-relevant state — channels, watch assignments, inventory, drops, wanted games — is **in-memory only** and is inherently re-derived from Twitch on each cycle. Only settings, cookies, and the refresh-token record are durable. The new UI cannot rely on any backend-persisted "app state" beyond settings; it must re-fetch inventory (or hold its own projection of it).

Note the ordering guarantee encoded in code, not data: GUI state is always cleared/rebuilt in sync with backend (`self.gui.channels.clear()` `twitch.py:785`, `self.gui.inv.clear()` `twitch.py:1406`) — a tight ordering contract any new UI must replicate.

---

## 4. Backend health (report only — no fixes)

1. **Dead GQL queries (declared in `GQL_QUERIES` but never referenced):** `ClaimCommunityPoints` (`constants.py:325`), `ChannelPointsContext` (`:336`), `SlugRedirect` (`:422`), `NotificationsView` (`:429`, self-annotated `# unused`), `NotificationsList` (`:436`, `# unused`). 0 code references (verified). Surviving dead weight.
2. **Dead/unused websocket topics:** `User/Presence` ("presence", `constants.py:500` `# unused`), `Channel/Drops` ("channel-drop-events", `:506` `# unused`), `Channel/CommunityPoints` (`:509` `# unused`), `User/CommunityPoints` (`:505`) — never subscribed. Kept alive by `WEBSOCKET_TOPICS` map.
3. **Dead channel methods:** `_send_watch_playlist` (`channel.py:432`, annotated `# NOTE: This is currently unused.`), `_send_watch_gql` (`channel.py:498`, same note). Only `send_watch` (`:484`, spade POST) is live.
4. **Disabled auth code paths:** `_chrome_login` is entirely commented out (`twitch.py:327`); `client_version` extraction is a commented-out regex block (`twitch.py:393-397`). The APP cannot run the "open in Chrome" fallback — CAPTCHA is a hard stop (`raise CaptchaRequired()` `twitch.py:327`).
5. **Swallowed/soft errors:**
   - Cookie load failure silently swallowed (`twitch.py:486` `# if loading in the cookies file ends up in an error, just ignore it`).
   - Image-fetch failures in `ImageCache.get` are eaten with `except Exception: pass` (`cache.py:147`), falling back to a blank white 10×10 placeholder (`:149`).
   - GQL "server error" nullifies a path key instead of retrying (`twitch.py:1328-1334`); `gql_request` raises on the first `else`-reaching error and has a fragile retry/`else` control-flow (`:1312-1364`) — an easy breakage point.
   - `RequestException` in `send_watch` returns `False` silently (`channel.py:494`); `_claim` returns `False` on any `GQLException` (`inventory.py:211`).
6. **Fragile hashes / hardcoded magic:** 10 GQL `sha256Hash` persisted-query IDs are hardcoded (`constants.py:463`+) and commented with known-from-cache warnings ("GQL is pretty volatile", `twitch.py:443`). `Game.SPECIAL_GAME_IDS = {509663, 509672}` (`utils.py:431`) is a hardcoded special-case. `RateLimiter(capacity=5, window=1)` is explicitly flagged "Do not modify" (`twitch.py:443-444`).
7. **Opaque/private-state dependence:** `shutdown()` reaches into `cookie_jar._cookies` (private) to clean empty cookies (`twitch.py:506-508`), and `get_session` clamps `connection_quality` in-place on the settings object (`twitch.py:472-475`).
8. **Unused imports / vestigial:** `Settings` declares `PASSTHROUGH` and a deliberate missing-setter `TypeError` for unknown names (`settings.py:74-78`); `Settings.alter()` exists but the design leans on implicit `__setattr__` mutation. `registry.py` ships a `__main__` self-test (`registry.py:104`) — harmless.
9. **Volatile session restore heuristics:** login recovery relies on cookie `auth-token` + `unique_id` (`twitch.py:397`,`:405-413`) and a "claimed if award-timestamp within drop window" heuristic in `BaseDrop.__init__` (`inventory.py:75-84`) — confident-but-heuristic claim detection.

---

## 5. Stability risks for the rewrite

1. **Bidirectional import cycle — the top risk.** `gui.py` defines `GUIManager`, but the backend imports it at module load (`twitch.py:19`) and constructs it from `Twitch.__init__` (`twitch.py:448`). Meanwhile `channel.py` and `websocket.py` grab UI references (`channel.py:177`, `websocket.py:43`), and every widget resolves `self._twitch`. To detach a new UI you must **invert the dependency**: the backend should expose an event/observer interface and stop importing/constructing the UI. Any new UI layer will need this broken into: a pure service client (`Twitch`, `Channel`, `DropsCampaign`, …), a **UI-agnostic event bus**, and a separate adapter that subscribes. This is the single largest refactor boundary.
2. **Backend calls UI methods directly and synchronously, everywhere.** ~20 distinct UI entry-points (§2a) are invoked from inside backend hot paths (websocket message handlers, `_watch_loop`, per-drop claims). The rewrite must define an event contract (`channel.online_changed`, `drop.progress`, `inventory.updated`, `status.text`, `ws.status`) to replace ~20 hard calls, and must decide **who owns the progress timer / countdown** (`CampaignProgress` `gui.py:640`) currently driven by `restart_watching` (`twitch.py:1047`) and `process_drops`.
3. **GUI-primitive types leak into the backend.** `PhotoImage` and Tk `master` are coupled inside `cache.ImageCache` (`cache.py:4-5`,`:153-155`) and `utils.set_root_icon` (`utils.py:40`). A web/other UI can't reuse `ImageCache` as-is; the cache must be split into a pure byte/pixel cache + a UI-side adaptor that turns a `PhotoImage` into whatever the new stack draws.
4. **State-ordering contract is implicit and shared.** The backend clears and repopulates GUI collections in lockstep (`twitch.py:785` channels.clear before repopulate; `twitch.py:1406` inv.clear before campaign adds). A new UI must mirror this exactly or it will display stale rows; there is no "diff" API — it's full clear-and-rebuild.
5. **Control flow is embedded in the backend state machine with side effects on the UI.** `State` (`constants.py:389`) transitions call `gui.status.update`, `gui.tray.change_icon`, `gui.print`, `channel.display` inline (`twitch.py:608-887`). The rewrite should replace the monolith `_run` loop with either a kept backend state machine exposing state as data, or a decoupled producer that pushes events — but choosing wrong will duplicate the ordering logic.
6. **The "UI" is the only console/logger target.** `Twitch.print` wraps `gui.print` (`twitch.py:558-562`) and `main.py:183` prints fatal errors into the GUI console. Error surfacing (fatal tracebacks, captcha) is UI-mediated; a headless/foreign UI must still capture these channels or lose all user-visible diagnostics.
7. **Settings tight-coupling to widget vars.** `SettingsPanel` writes settings attributes on widget change (`gui.py:1637` language, `:1853` dark_mode, `:1916` autostart_tray, `:2040` priority_mode). The `Settings.__getattr__/__setattr__` overlay (`settings.py:64-78`) means a new UI must route **all** writes through `settings.<attr> = value` + `alter()`/`save()` — preserving the exact key set in `SettingsFile` (`settings.py:14`) or the JSON merge (`utils.merge_json` `utils.py:237`) will rewrite unknowns.
8. **Weakly-typed data contract.** The UI consumes plain attributes/properties on `Channel`/`DropsCampaign`/`TimedDrop` and raw GQL `JsonType` dicts (e.g. `channel_data["stream"]`). There is no schema/DTO layer — any UI rewrite should either keep these exact attributes or formalize the shapes, or the new frontend will silently break on field renames.
9. **OS/packaging coupling inside the UI layer.** `registry.py` (autostart) and `win32*`/AppKit branches live in `gui.py`/`settings.py`; a new cross-platform UI should keep autostart as a service, not UI code.
10. **Single-instance + lifecycle coupling.** `main.py` drives `Twitch.run`/`shutdown` and calls `client.gui.*` directly (`main.py:164-196`). The new build must keep the `lock_file` single-instance guard (`main.py:203`) and the asymmetric save-after-close window (`main.py:194`, "user can alter settings between termination and closing") — easily lost in a rewrite.

---

## Bottom line

The backend is a **single self-orchestrating asyncio service owned by `Twitch`**, with 20+ direct UI callouts interleaved into its control flow and GUI-primitive types leaking into image cache and models. The contents worth keeping are entirely re-derivable: channels, inventory, drops, and wanted games are transient in-memory projections; only `settings.json`, the cookie jar, and the image cache are durable. The rewrite should treat the backend as a **service boundary to extract behind an event bus**, not as something the new UI can import the way `gui.py` does today.
