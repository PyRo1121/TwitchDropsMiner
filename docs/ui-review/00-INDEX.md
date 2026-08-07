# UI Redesign Review — Index

**Mission:** Complete UI rewrite of **TwitchDropsMiner** (Python 3 + Tkinter), adopting the visual language and architecture of **Farmer** (Go + Wails v2 + React 19 + Vite 7).

**Swarm:** 8 parallel agents, each with a non-overlapping review brief. Every agent documents findings to its numbered output file, cites external sources (2026-standard), and never modifies source code.

**Outputs:**

| # | Agent | Brief | Output |
| --- | ------- | ------- | -------- |
| 01 | UI current-state audit | `BRIEF-01-ui-audit.md` | `01-UI-CURRENT-STATE.md` |
| 02 | Backend service surface | `BRIEF-02-backend-surface.md` | `02-BACKEND-SURFACE.md` |
| 03 | Farmer UI reference | `BRIEF-03-farmer-reference.md` | `03-FARMER-UI-REFERENCE.md` |
| 04 | Stack recommendation | `BRIEF-04-stack-recommendation.md` | `04-STACK-RECOMMENDATION.md` |
| 05 | Async & threading | `BRIEF-05-async-threading.md` | `05-ASYNC-THREADING.md` |
| 06 | i18n & settings | `BRIEF-06-i18n-settings.md` | `06-I18N-SETTINGS.md` |
| 07 | Build & packaging | `BRIEF-07-build-packaging.md` | `07-BUILD-PACKAGING.md` |
| 08 | UX design & tokens | `BRIEF-08-ux-design.md` | `08-UX-DESIGN-TOKENS.md` |
| 00 | **Synthesis (orchestrator)** | — | `00-UI-REDESIGN-MASTER-PLAN.md` |

## Common ground truth

- Repo root: `/home/pyro1121/Documents/TwitchDropsMiner` (~8,171 LOC across 14 Python modules; `gui.py` = 2,971 lines of Tkinter).
- Reference UI: `/home/pyro1121/Documents/Farmer` — Wails v2 desktop app; frontend at `Farmer/frontend` (React 19, Vite 7, TypeScript, Vitest; `App.tsx` 2,588 LOC, `App.css` 2,147 LOC).
- Guidelines: `/home/pyro1121/.pi/agent/AGENTS.md`.
