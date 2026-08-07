# BRIEF-06 — i18n & Settings Audit

**Output file: `docs/ui-review/06-I18N-SETTINGS.md`** (overwrite).

## Mission

Document the **localization system** and **settings persistence** so the rewrite preserves them exactly (or improves them deliberately).

## Scope

- `translate.py` (510) — read fully: how translations load, the string-key mechanism, fallback behavior, pluralization, variables in strings, how the UI switches languages at runtime.
- `lang/*.json` — list all 14+ languages; sample 2–3 files (e.g. the largest and smallest) to extract the string-key structure and count.
- `gui.py` — how UI code calls the translator (pattern inventory: direct key lookups, lazy vs eager), and whether strings are embedded anywhere else (hard-coded English in gui.py, README claims?).
- `settings.py` (101) — full read: settings schema, defaults, types, validation, where it's persisted (JSON path?), atomicity of writes.
- `constants.py` — anything settings-ish (version, defaults) that the UI reads.
- `cache.py` — persisted cache formats that might be affected by a settings schema change.

## Deliverables

1. **Translation mechanism** — how it works end-to-end with file:line refs; the full key namespace (group by feature area); variable/plural support; how a new language is added.
2. **Hard-coded string audit** — find UI-visible strings NOT routed through translation (grep for literals in gui.py); list them as cleanup candidates.
3. **Settings schema** — exact current JSON schema (field → type → default → meaning), file location, load/save code path, corruption handling.
4. **Rewrite requirements** — what the new UI must do for i18n (runtime language switching, RTL?, font coverage for non-Latin scripts — check lang dir for CJK/Arabic), and for settings (schema versioning, backward compat with existing settings file, UI-driven editing with validation).
5. **Gap analysis** — i18n/settings features the current app lacks that a 2026 UI should have (cite sources for best practice: e.g. Fluent/ICU message syntax, schema versioning).

## Rules

- Quote `file:line`; cite external best-practice claims with URLs.
- Do NOT modify source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-06.md` and stop.
