# BRIEF-03 — Farmer UI Reference Extraction

**Output file: `docs/ui-review/03-FARMER-UI-REFERENCE.md`** (overwrite).

## Mission

Extract everything about **Farmer's UI** (the reference the user loves) so it can be replicated in the TwitchDropsMiner rewrite. Read the Farmer frontend thoroughly; you are the "translation spec" author.

## Scope

- `/home/pyro1121/Documents/Farmer/frontend/` — read fully:
  - `src/App.tsx` (2,588) — app shell, layout, routing/view switching, top-level state.
  - `src/App.css` (2,147) + `src/style.css` — **design language**: color palette (esp. dark theme), gradients, radii, shadows, typography, spacing, animations, breakpoints.
  - `src/components/Chrome.tsx`, `CampaignWorkspace.tsx`, `CampaignCatalog.tsx`, `AccountModal.tsx` — component patterns: modals, tables, status badges, toasts, forms.
  - `src/hooks/useAutomation.ts`, `src/auth.ts`, `src/operational.ts` — state/data patterns and how the UI consumes the Go backend (Wails bindings: `src/wailsjs/go/...`).
  - `index.html`, `vite.config.ts`, `package.json` — tooling.
- `/home/pyro1121/Documents/Farmer/app.go` (54K) — skim the exported `App` methods to understand what the frontend binds to; note the binding pattern (Go struct → React).
- `/home/pyro1121/Documents/Farmer/README.md` — product description of Farmer for context (what the app does, branding).

## Deliverables

1. **Architecture pattern** — how Wails v2 binds Go ⇄ React; how screens are composed; state management approach (no Redux? plain hooks?).
2. **Design tokens** — extract concrete values: color hexes (background layers, accents, text, status colors), font stack/sizes, spacing scale, radii, shadows, transitions. Quote `App.css:line`.
3. **Component inventory** — each reusable component, its props/behavior, and a snippet or description of its visual style.
4. **Interaction patterns** — modal behavior, empty states, loading states, error states, confirmation dialogs, toasts.
5. **Translation map to TDM** — given TDM's screens (from BRIEF-01 knowledge: dashboard with stream/drop status, channel list, priority list, inventory, settings, login, tray): which Farmer patterns/components map to each TDM screen, and what's missing that TDM would need to add (e.g. drop-progress widget, campaign discovery UI).
6. **Consistency notes** — things Farmer does that TDM should copy verbatim (CSS approach, dark palette, layout rhythm) and things to improve.

## Rules

- Quote `file:line` for code/design claims. You may fetch screenshots if any exist in Farmer's docs.
- Do NOT modify Farmer or TDM source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-03.md` and stop.
