# 06 — i18n & Settings Audit

**Brief:** `docs/ui-review/BRIEF-06-i18n-settings.md`
**Scope examined:** `translate.py` (fully), `settings.py` (fully), `constants.py` (settings/paths/version parts), `utils.py` (`json_load`/`json_save`/serialization/merge), `cache.py`, `gui.py` (translator call sites + hard-coded literals), `twitch.py`/`inventory.py`/`main.py` (translator & persistence call sites), all `lang/*.json` files.
**Status:** review only — no source modified.

---

## 1. Translation mechanism (end-to-end)

### 1.1 The `Translator` object

A single module-level instance is created at import time:

- `translate.py:545` — `_ = Translator()` — the module global used by every other module (`from translate import _`).
- `Translator.__init__` (`translate.py:430`) starts with `_translation = default_translation.copy()` and, **in dev only** (`if not IS_PACKAGED`, `translate.py:438`), rewrites `lang/English.json` from the in-code default so the template stays canonical.
- It scans `LANG_PATH.glob("*.json")` (`translate.py:443`) to build the language list, sorts it, and inserts `DEFAULT_LANG` (`"English"`, `constants.py:151`) at index 0. `languages` (`translate.py:456`) exposes the names; `current` (`translate.py:460`) returns the active `language_name`.

### 1.2 Language switching (runtime vs. startup-only)

- `Translator.set_language(language)` (`translate.py:465`) validates against the loaded file list, then:
  - `DEFAULT_LANG` → uses the in-memory `default_translation.copy()` (`translate.py:474`);
  - otherwise → `json_load(LANG_PATH / f"{language}.json", default_translation)` (`translate.py:477`) — **note the merge**: `json_load(..., merge=True)` runs `merge_json`, so any key missing from the translation file is back-filled from the English default, and any key present but **not** in English is silently dropped (`utils.py:191` `merge_json` `del obj[k]`).
  - It rejects files that define `language_name` (`translate.py:480`) and sets it itself (`translate.py:482`).
- **The critical runtime fact:** `set_language` is called **only once at startup**, in `main.py:141` inside `async def main()`. The Settings panel (`gui.py:1632-1638`) writes `settings.language` only; there is **no call to `_.set_language(...)` when the user picks a new language**. The label even hard-codes "(requires restart)". So **language switching is restart-only in practice**, even though the translator itself fully supports in-process switching. The deferral comment at `gui.py:1579-1580` ("Translation calls have to be deferred here, to allow changing the language before the settings panel is initialized") shows the reload intent but the wiring was never completed.

### 1.3 Lookup mechanism

- `Translator.__call__(*path)` (`translate.py:487`) walks dotted keys — e.g. `_("gui", "status", "idle")`. A missing key raises `MinerException` (`translate.py:498`).
- Because every non-default load passes through `merge_json` against `default_translation` (`utils.py:191`), a missing key in a translation file **cannot normally fail** — it is back-filled with English. The `MinerException` path is effectively dead for loaded files and only reachable if the English default itself is malformed.

### 1.4 Formatting / variables / plurals

- **Variables** are Python `str.format()` placeholders, always using **named** args so translators may reorder words. Inventory of variables in the default strings:

  | Key | Placeholder(s) | Sample call site |
  | --- | --- | --- |
  | `status.watching` | `{channel}` | `twitch.py:870` |
  | `status.goes_online` / `goes_offline` | `{channel}` | `twitch.py:1118,1134` |
  | `status.claimed_drop` | `{drop}` | `inventory.py:179` |
  | `status.adding_campaigns` | `{counter}` | (GUI) |
  | `login.error_code` | `{error_code}` | `twitch.py:311` |
  | `error.site_down` | `{seconds}` | `twitch.py:1271` |
  | `error.no_connection` | `{seconds}`, `{url}` | `twitch.py:1282` |
  | `gui.websocket.websocket` | `{id}` | `gui.py:465` |
  | `gui.progress.remaining` | `{time}` | `gui.py:728,732` |
  | `gui.inventory.starts` / `ends` | `{time}` | `gui.py:1418,1421,1518,1531,1536` |
  | `gui.inventory.and_more` | `{amount}` | — |
  | `gui.inventory.percent_progress` | `{percent}`, `{minutes}` | `gui.py:1512` |
  | `gui.inventory.minutes_progress` | `{minutes}` | `gui.py:1523` |

- **No pluralization support whatsoever.** There is one hard-coded template per message (e.g. `and_more` = `"and {amount} more..."`, `percent_progress` = `"{percent} of {minutes} minutes"`). Languages with multiple grammatical number categories (Russian, Polish, Czech, Serbian, Arabic have 3–4 categories) cannot express them. CLDR mandates `one/two/few/many/other` (see §5).
- **No number/date/unit locale formatting** — times are pre-formatted strings (`f"{hours:>2}:{minutes:02}:{dseconds:02}"`, `gui.py:728`) before being passed to `.format(time=...)`, so locale-aware digit grouping is impossible.
- Formatting is **manual** at each call site via `.format(...)` — there is no centralized renderer; a translator who needs to reorder must edit the translator-owned string, but the app-side format args are already keyword-based so reordering works.
- `english_name` is informational (credit lists); `language_name` is set at runtime.

### 1.5 Full key namespace (127 leaf keys in the English default, incl. `english_name`)

Grouped by feature area. Excludes runtime `language_name`. Line refs point into `translate.py` `default_translation`.

**Core (non-GUI) status & messaging — `status.*`, `login.*`, `error.*`:**

- `status`: terminated, watching`{channel}`, goes_online`{channel}`, goes_offline`{channel}`, claimed_drop`{drop}`, no_channel, no_campaign
- `login`: unexpected_content; chrome.startup / login_to_complete / no_token / closed_window; error_code`{error_code}`, incorrect_login_pass, incorrect_email_code, incorrect_twofa_code, email_code_required, twofa_code_required
- `error`: captcha, site_down`{seconds}`, no_connection`{seconds},{url}`

**GUI — `gui.*`:**

- `gui.output`
- `gui.status`: name, idle, exiting, terminated, cleanup, gathering, switching, fetching_inventory, fetching_campaigns, adding_campaigns`{counter}`
- `gui.tabs`: main, inventory, settings, help
- `gui.tray`: notification_title, minimize, show, quit
- `gui.login`: name, labels, logged_in, logged_out, logging_in, required, request, username, password, twofa_code, button
- `gui.websocket`: name, websocket`{id}`, initializing, connected, disconnected, connecting, disconnecting, reconnecting
- `gui.progress`: name, drop, game, campaign, remaining`{time}`, drop_progress, campaign_progress
- `gui.channels`: name, switch, online, pending, offline; headings.channel / status / game / viewers
- `gui.inventory`: filter.{name,show,not_linked,upcoming,expired,excluded,finished,refresh}; status.{linked,not_linked,active,upcoming,expired,claimed,ready_to_claim}; starts`{time}`, ends`{time}`, allowed_channels, all_channels, and_more`{amount}`, percent_progress`{percent,minutes}`, minutes_progress`{minutes}`
- `gui.settings`: general.{name,autostart,tray,tray_notifications,dark_mode,priority_mode,proxy}; advanced.{name,warning,warning_text,enable_badges_emotes,available_drops_check}; priority_modes.{priority_only,ending_soonest,low_availability}; game_name, priority, exclude, reload, reload_text
- `gui.help`: links.{name,inventory,campaigns}; how_it_works, how_it_works_text, getting_started, getting_started_text; invalidate.{button,text}

### 1.6 Languages present

**20 files** in `lang/` plus the built-in English default — **21 languages total**:

Dansk, Deutsch, Español, Français, Indonesian, Italiano, Magyar, Nederlandse, Norsk, Polski, Português, Română, Türkçe, Čeština, Русский, Українська, العربية, 日本語, 简体中文, 繁體中文 (+ English).

Script coverage relevant to rendering: **Arabic (RTL)**, **Chinese simplified + traditional (CJK)**, **Japanese (CJK)**, Cyrillic (Русский, Українська), Central-European Latins (Čeština, Magyar, Polski, Română, Türkçe). The Arabic file is missing English keys — e.g. `gui.settings.general.priority_mode`, the whole `gui.settings.advanced` block, `gui.settings.priority_modes`, `gui.help.invalidate`, `gui.inventory.minutes_progress` — confirming the **English back-fill via `merge_json` is load-bearing** for incomplete/lagging translations (117 Arabic leaves vs 127 canonical).

### 1.7 How to add a new language (today)

1. Copy `lang/English.json` (regenerated on every dev launch, `translate.py:438`) to `lang/<Native Name>.json`. (No English.json is committed — it is the in-memory `default_translation`.)
2. Translate every value; leave `language_name` out (forbidden, `translate.py:480`); `english_name` is optional-ish.
3. Ship the file with the build (`build.spec` / AppImage bundle `lang` dir); it is auto-discovered by glob (`translate.py:443`).
4. The language appears in the Settings combo (name = filename stem). No registry/constant change needed.

---

## 2. Hard-coded string audit (NOT routed through `_()`)

These are user-visible strings in `gui.py` NOT in the translation system — rewrite cleanup candidates:

| Location | String | Context |
| --- | --- | --- |
| `gui.py:1632` | `"Language 🌐 (requires restart): "` | Settings tab – the **language selector label itself is untranslated** (ironic for an i18n feature) |
| `gui.py:2105` | `"About"` | Help tab LabelFrame title |
| `gui.py:2110` | `"Application created by: "` | Help tab label |
| `gui.py:2116` | `"Repository: "` | Help tab label |
| `gui.py:2126` | `"Donate: "` | Help tab label |
| `gui.py:2127-2131` | `"If you like the application and found it useful, please consider donating..."` | Help tab donate sentence |
| `gui.py:1082` | `TITLE = "Twitch Drops Miner"` | Tray/toolbar window title base |
| `gui.py:604` | `f"Enter this code on the Twitch's device activation page: {user_code}"` | Login device-activation instruction printed to log/output |
| `gui.py:2298` | `f"Logging level: {logging.getLevelName(...)}"` | Diagnostic line printed to output log |
| `constants.py:155` | `WINDOW_TITLE = f"Twitch Drops Miner v{__version__} (by DevilXD)"` | Window title (arguably fine as a fixed title; still untranslated) |

Other literals found are *not* UI-facing: internal exceptions (`gui.py:489,1134,1202`), test stubs (`gui.py:2840-2925`, incl. `tray.notify(claim_text, "Mined Drop")`), log/sentry formatting (`gui.py:2199`), theme internals (`gui.py:2570` `"Courier New"`). The **About/Donate block (6 strings)** and **language label** are the real i18n gaps. Note `gui.py:1632` vs `constants.py:155` show two distinct "title" strings.

---

## 3. Settings schema & persistence

### 3.1 Location & serialization primitives

- **Path:** `SETTINGS_PATH = WORKING_DIR / "settings.json"` (`constants.py:126`). In packaged builds `WORKING_DIR` = dir of the executable / inside the macOS bundle (`README.md:56,73`). Not committed to the repo.
- **Atomic writes:** `json_save` (`utils.py:220`) writes to `settings.json.new` then `new_path.replace(path)` — an **atomic rename** (`utils.py:226`). `json_load` (`utils.py:200`) prefers a leftover `.new` (crash-recovery) file, reading it, then falls back to the main file.
- **Typed serialization:** `_serialize` (`utils.py:100`) wraps `set`, `URL`, `Enum`, `datetime` as `{"__type": name, "data": ...}`; `_deserialize` + `SERIALIZE_ENV` (`utils.py:123`) reverse it. So the on-disk JSON uses typed wrappers for non-primitives.

### 3.2 Schema (field → type → default → meaning)

From `settings.py:16-36` (`default_settings`) and `SettingsFile` TypedDict.

| Field | Type | Default | Meaning / UI anchor |
| --- | --- | --- | --- |
| `proxy` | `yarl.URL` (JSON `{"__type":"URL","data":str}`) | `URL()` (empty) | `gui.settings.general.proxy` — requires restart |
| `priority` | `list[str]` | `[]` | Ordered game list to mine first — `gui.settings.priority` listbox |
| `exclude` | `set[str]` (JSON `{"__type":"set","data":[...]}`) | `set()` | Games never mined — `gui.settings.exclude` listbox |
| `dark_mode` | `bool` | `True` | `gui.settings.general.dark_mode` |
| `autostart_tray` | `bool` | `False` | `gui.settings.general.tray` — autostart minimized |
| `connection_quality` | `int` | `1` | (advanced; WS quality) |
| `language` | `str` | `DEFAULT_LANG` = `"English"` | `gui.settings.general` language combo |
| `tray_notifications` | `bool` | `True` | `gui.settings.general.tray_notifications` |
| `enable_badges_emotes` | `bool` | `False` | `gui.settings.advanced.enable_badges_emotes` |
| `available_drops_check` | `bool` | `False` | `gui.settings.advanced.available_drops_check` |
| `priority_mode` | `PriorityMode` Enum (JSON `{"__type":"PriorityMode","data":int}`) | `PRIORITY_ONLY` (0) | `gui.settings.general.priority_mode`; enum in `constants.py:409-412` (PRIORITY_ONLY=0, ENDING_SOONEST=1, LOW_AVBL_FIRST=2) |

Plus **CLI-only** values held by the `Settings` object but not persisted: `log`, `tray`, `dump`, `debug_ws`, `debug_gql`, `logging_level` (class attrs, `settings.py:39-50`).

### 3.3 Load / save code path

- **Load:** `Settings.__init__` (`settings.py:62`) does `self._settings = json_load(SETTINGS_PATH, default_settings)` — `merge=True` (default), so `merge_json` (`utils.py:191`) reconciles disk vs. defaults: unknown keys deleted, type-mismatched values replaced by default, missing keys filled from default. **There is no schema `version` field** — backward-compat relies purely on this key/type reconciliation.
- **Access:** `Settings.__getattr__` (`settings.py:75`) resolves **args first, then settings file**; `__setattr__` (`settings.py:88`) writes into `_settings` and sets `_altered=True`; `__delattr__` raises (immutable removal). `alter()` (`settings.py:102`) forces dirty.
- **Save:** `Settings.save(force=False)` (`settings.py:105`) writes only if `_altered or force`. Callers: `twitch.py:569` (`self.settings.save(force=force)` via `Twitch.save`), `twitch.py:651` (every inventory fetch), `main.py:194` (`client.save(force=True)` on clean exit). UI edits mark dirty via `_settings.alter()` at `gui.py:1989,2028,2036,2054,2086` (priority/exclude list ops, etc.) and via `__setattr__` for the settings vars.
- **Corruption handling:** `main.py:124-134` wraps `Settings(args)` in a try/except that shows an error dialog and `sys.exit(4)`. Note `json_load` only catches `JSONDecodeError` for the `.new` recovery file (`utils.py:204`); a corrupt **primary** `settings.json` propagates the decode error up to this top-level catch → **app refuses to start**. There is no automatic reset-to-defaults or backup for the primary file.
- **GUI editing:** Settings tab (`gui.py:1619+`) binds an `IntVar`/`StringVar` per control; checkboxes for autostart/tray additionally write OS autostart (Windows registry `HKCU/.../Run` at `gui.py:1921`, Linux `.desktop` `gui.py:1937`, macOS `launchd` plist `gui.py:1953`). Proxy is a text box; the **language combo and proxy are persisted but applied only on restart** (proxy: "requires restart").

### 3.4 Cache (interaction with settings schema)

`cache.py` persists its own file, `cache/mapping.json` (`constants.py:125`, `CACHE_DB`), an image-hash → filename map with expiry, using the same `json_load`/`json_save` stack (`cache.py:36,47`). It is **independent of the settings schema** — a settings change does not touch it — but it shares the same serialization/merge machinery (`ImageCache` catches `JSONDecodeError` and purges, `cache.py:36-44`), so the same atomic `.new`+rename and typed-dict patterns apply. The image files under `cache/` are derived from URLs → hash and are self-healing.

---

## 4. Rewrite requirements

### 4.1 i18n requirements for the new UI

1. **True runtime language switching.** The current `set_language` supports it but it's only invoked at startup (`main.py:141`). The new UI must call `_.set_language(...)` on combo change **and re-render the active view** (all `_()` sites are read at widget-construction time, so today they must be re-built or the model must push updates). Preserve the existing `default_translation.copy()` + merge-back-fill semantics so partial files degrade to English instead of crashing.
2. **Translate the remaining hard-coded strings** (the entire Help→About/Donate block, the language label itself, device-activation and logging-level output lines). Add keys to the canonical English default and all files.
3. **RTL (Arabic).** There is zero bidi/RTL handling today (no `ttk` RTL layout, no mirroring — grep finds no `rtl`/`textdirection` anywhere). `العربية` is a first-class shipped language, so the rewrite framework (Tkinter / a proper toolkit) must support per-widget `textdirection=rtl` or framework-level locale mirroring.
4. **Font coverage for non-Latin scripts.** Default Tk font handles system fallback, but the **monospaced log font is hard-pinned to `"Courier New"`** (`gui.py:2570`) and the config for large/arrow/underlined fonts is manual (`gui.py:2553-2566`) — Courier New lacks CJK/Arabic glyphs. The rewrite should use font-family fallback lists (e.g. `Courier New` → `Noto Sans Mono`/`IPAGothic`) and let CJK/Arabic/Cyrillic resolve.
5. **Keep keyword placeholders; consider centralized rendering.** Named `.format()` placeholders are already good (translators can reorder). A centralized renderer would let the same message be used in plain string + tooltip + aria contexts.

### 4.2 Settings requirements for the new UI

1. **Schema versioning.** Add an explicit `version` (or `schema_version`) key and a sequential migration chain on load (parse version → validate → migrate vN→vN+1 → validate current). Today the only "migration" is implicit key/type reconciliation via `merge_json` (`utils.py:191`), which cannot express renames, semantic changes, or value transforms and silently drops unknown keys.
2. **Backward compatibility with the existing `settings.json`.** Any existing user file must keep working: reuse the same typed-wrapper format (`{"__type":...}`), the `.new`-file crash recovery, and the atomic rename. Preserve the `__getattr__` args-first-then-file precedence.
3. **UI-driven editing with validation.** Today the proxy URL and lists are free-text with no validation at write time (only `PriorityMode` gets a range check at `gui.py:1585-1587`). The rewrite should validate types/ranges/enums in one place (a validator on the model), surface inline errors, and only persist through the dirty-flag path (`_altered` → save on shutdown or explicit "Apply").
4. **Corruption resilience.** Add automatic repair/backup for a corrupt primary file (currently a corrupt `settings.json` = startup error + exit 4). Reset-with-backup is the expected 2026 behavior.
5. **Clear the apply-timing model.** Distinguish settings that apply immediately (dark mode, tray toggles, language) from restart-required ones (proxy); the current "requires restart" is only a label.

---

## 5. Gap analysis (2026 best practice vs. current app)

| Area | Current | 2026 expectation | Sources |
| --- | --- | --- | --- |
| Pluralization | Single template per message; no `zero/one/two/few/many/other` | CLDR plural categories; `other` required; locale-dependent | <https://cldr.unicode.org/index/cldr-spec/plural-rules> |
| Message format | Ad-hoc `str.format` with keyword args | ICU **MessageFormat 2** (`plural`/`select`/`.local`) or **Fluent (FTL)** for linguistic freedom; full-sentence translation, not concatenation | <https://unicode-org.github.io/icu/userguide/format_parse/messages/> ; <https://messageformat.unicode.org/docs/> ; <https://projectfluent.org/fluent/guide/> |
| Number/date/unit formatting | Pre-formatted by caller (`f"{h:>2}:{m:02}"`) | Pass typed numbers/dates to the localization runtime; locale-aware | <https://unicode-org.github.io/icu/userguide/format_parse/messages/> |
| Locale testing | None (no tests for translations) | Locale-specific test coverage (English, French, Polish/Russian, Arabic; `0,1,2`, teens, large nums) | <https://cldr.unicode.org/index/cldr-spec/plural-rules> |
| Settings schema versioning | No `version`; implicit key/type merge | Explicit document-format version + sequential migrations; additive defaults; unknown-key policy; reserve retired names | <https://json-schema.org/understanding-json-schema/basics> (Exa synthesis) ; <https://github.com/jbtronics/settings-bundle/blob/master/docs/usage/migrations.md> ; <https://protobuf.dev/programming-guides/proto3/> |
| Setting validation | Free-text, minimal | Schema-validated model with inline errors | <https://python-config-secrets-hub.com/type-safe-validation-with-pydantic-settings/schema-evolution-versioning/> |

The single biggest quality gap is **pluralization** (3–4-category languages are shipped today with single-template messages), and the single biggest **structural** gap is **no settings schema version** — the implicit merge is brittle for renames/semantic changes.

---

## Appendix — reference file:line index

- Translator core: `translate.py:430-545`
- Default strings: `translate.py:170-427`
- Startup language load: `main.py:141`
- Settings model: `settings.py:16-107`
- Persistence primitives: `utils.py:100-130` (serialize), `utils.py:191-226` (merge/load/save)
- Settings paths/consts: `constants.py:126,151,155,409-412`
- Cache: `cache.py:33-56`
- Language combo (restart-only): `gui.py:1597,1630-1638`
- Hard-coded literals: `gui.py:1632,2105-2131,604,1082,2298,2570`
- Config save triggers: `twitch.py:569,651`; `main.py:194`
