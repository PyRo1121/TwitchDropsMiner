# 03 — Farmer UI Reference (Translation Spec)

**Agent:** farmer-reference · **Source:** `/home/pyro1121/Documents/Farmer` (Go + Wails v2 + React 19 + Vite 7 + TypeScript + Vitest)
**Purpose:** Extract Farmer's UI wholesale so the TwitchDropsMiner rewrite ("TDM", Python 3 + Tkinter today) can replicate its visual language, architecture, and interaction patterns.
**Scope note:** All quotes below are `file:line` from the Farmer repo. Farmer ships no real UI screenshots (only `build/appicon.png`), so styling claims come from `App.css` / `style.css` and JSX structure.

---

## 0. TL;DR — What TDM should copy

1. **Architecture:** Wails v2 binds one Go `App` struct → auto-generated `window.go.main.App.*` JS proxy. React shell does **no Redux** — plain `useState` + `useEffect` + one custom hook (`useAutomation`). Screens are conditionally rendered by a nav-state switch.
2. **Design language:** a deep-navy dark palette (`--bg:#090d14`), a single purple accent (`--accent:#9b7cff`), green = healthy, amber = warning, red = error, tiny 7–10px type, 8–17px radii, layered translucent surfaces, soft purple glow shadows.
3. **Interaction grammar:** dashed-border empty states with icon + heading + CTA, green "notice" / red "error" banners inline, button text that mutates to the pending action ("Syncing…", "Claiming…"), a reusable modal with `Escape`/`Tab`-trap/keyboard focus restore, and pulsing "waiting" indicators.
4. **State/data:** every backend call is a plain async function whose result is set into state; background reconcile loops use `window.setInterval` firing Wails calls; stale-result guards via mutable ref counters (**seatbelt pattern**, below).

---

## 1. Architecture pattern

### 1.1 Wails v2 Go ⇄ React binding

- The Go binary defines one struct `App` with exported methods and binds it at startup: `Bind: []interface{}{app}` (`main.go:181`). Wails generates an ES module per struct in `frontend/src/wailsjs/go/main/App.js` (44 functions) plus `App.d.ts` with TypeScript signatures returning `Promise<...>`.
- Each generated function is a thin proxy over the injected bridge object, e.g. `export function GetAuthStatus() { return window['go']['main']['App']['GetAuthStatus'](); }` (`wailsjs/go/main/App.js`). Frontend code imports these directly:
  `import { GetBootstrap, GetAuthStatus, ... } from "../wailsjs/go/main/App";` (`App.tsx:10`).
- Runtime utilities (open URL, clipboard) come from `@wailsjs/runtime`: `BrowserOpenURL`, `ClipboardSetText` (`App.tsx:45`).
- **Shared model types** are mirrored Go structs auto-generated into `wailsjs/go/models.ts` (namespaces `store`, `session`, `automation`, `inventory`, `scheduler`, `discovery`, `collections`, `streaks`, `playbackbridge`, `watch`, `main`), each with a `createFrom()` constructor and `convertValues()` helper. This is the single source of truth for DTO shapes — TDM's rewrite should generate (or hand-maintain) the same shared-type boundary.
- **Binding surface (Go ⇄ React):** the exported `App` methods enumerate exactly what the UI can do (from `app.go` `func (a *App) ...` at 1475 LOC):
  - **Bootstrap/health:** `GetBootstrap` (336), `GetSupportBundle` (376), `GetDebugLogInfo` (440), `ReportFrontendError` (451).
  - **Auth (two isolated sessions):** `GetAuthStatus` (501), `GetRegisteredAuthStatus` (512), `StartTwitchLogin` (523), `GetRiskDisclosure` (550), `AcceptRiskDisclosure` (566), `StartRewardLogin` (584), `CancelTwitchLogin` (599), `DisconnectTwitch` (613), `DisconnectRegisteredTwitch` (625), `GetCachedRewardInventory` (652), `SyncRewardInventory` (668), `ClaimDropReward` (691), `GetRewardCode` (722).
  - **Channels:** `ListChannels` (898), `ImportLiveFollowedChannels` (965), `AddChannel` (1011), `SetChannelEnabled` (1034), `RemoveChannel` (1048).
  - **Discovery/scheduler:** `SyncBadgeCampaigns` (1083), `SyncStreaks` (1125), `ValidatePlayback` (1157), `PlanSchedule` (1182), `ScanConfiguredOpportunities` (1424), `ScanPublicOpportunities` (1457).
  - **Watch/session + automation:** `StartWatchSession` (1270), `GetActiveWatchSession` (1339), `StopWatchSession` (1353), `VerifyWatchSession` (1375), `GetPlaybackBridgeStatus` (806), `StartScheduledPlayback` (816), `ResetPlaybackCompanion` (870), `ListSchedulerDecisions` (1412).
- **Window shell (`main.go`):** 1280×820, min 980×680, `BackgroundColour RGBA{9,13,20}` (matches `--bg`), single-instance lock with a second-instance-focus handler, embedded `frontend/dist` via `go:embed`.

### 1.2 Screen composition

- There is **no router**. `App()` holds a single `activeNav` string state (`App.tsx:100`), default `"overview"`. The `return` is one `<div className="shell">` → `<Sidebar>` + `<main className="main"><TopBar/>{...conditional...}</main>`.
- The body is a large conditional chain on `activeNav`:
  - `"channels"` → inline `<section className="campaign-workspace">` (`App.tsx:1545`)
  - `"streaks"` → inline section (`App.tsx:1818`)
  - `"campaigns"` → `<CampaignWorkspace>` component (`App.tsx:2031`)
  - `"collection"` → inline section (`App.tsx:2089`)
  - `"activity"` → inline section (`App.tsx:2655`)
  - `"overview"` → hero + metrics + panels + boundary bar (`App.tsx:2881`)
  - any other tab → `<section className="coming-soon">` placeholder ("MILESTONE 0… on the roadmap") (`App.tsx:2794`)
- **Navigation registration** is a single `navItems` const array of `[id,label]` pairs: `overview, channels, streaks("Streaks & Weekly"), campaigns, collection, activity, settings` (`Chrome.tsx:11`). Clicking a nav item in the `Sidebar` calls `onNavigate(id)`; the "settings" id is special-cased to open the account modal instead of a screen (`Chrome.tsx:92`).
- **Top-level layout CSS:** `.shell` is `display:grid; grid-template-columns:224px minmax(0,1fr); height:100vh` with a radial purple glow (`App.css:27-35`); `.sidebar` is a sticky 100vh column with sticky footer (operation status dot); `.main` is the scroll region (`padding:0 34px 30px`, `overflow-y:auto`) (`App.css:145-152`).

### 1.3 State management (no Redux)

- **Plain React state.** `App.tsx` declares ~40 `useState` slices (bootstrap, auth, registeredAuth, channels, inventorySnapshot, scan, schedulePlan, badgeCampaigns, streakSnapshot, activeWatch, activity, plus per-feature `busy`/`error`/`notice` trios). No context providers, no external store.
- **One custom hook:** `useAutomation()` (`hooks/useAutomation.ts`) wraps scheduler + companion pairing: it de-dups concurrent refreshes, exposes `{status, bridge, busy, error, notice, toggle, resetCompanion}`.
- **The seatbelt pattern (stale-response guard)** is pervasive and worth copying verbatim: each feature keeps a `useRef(0)` counter; before a call the handler does `const requestID = ++<ref>.current`, then after the `await` checks `if (requestID !== <ref>.current) return;` before mutating state. Seen in `refreshAuthStatuses` (`App.tsx:198`), `useAutomation` (`useAutomation.ts:31,43,56`), inventory sync (`App.tsx:320`), scan (`App.tsx:406`), streaks (`App.tsx:470`).
- **Polling** is done with `window.setInterval` inside `useEffect` with cleanup: scheduler/bridge every 3s (`useAutomation.ts:52`), auth status every 1.5s (`App.tsx:591`), active watch reconciliation every 1.5s while pending (`App.tsx:314`), authoritative Drop verify every 5 min while `ACTIVE` (`App.tsx:514`), scheduler-driven app-auth refresh every 15s (`App.tsx:551`).
- **Derived/helper logic** is extracted to pure functions and unit-tested: `auth.ts` (session-kind selection, expiry math, reauth detection) and `operational.ts` (dashboard slot builder, bridge label, playback label, progress %) are all plain functions — TDM's rewrite should keep the business-logic-strings-in-pure-modules pattern.

---

## 2. Design tokens (concrete, with `file:line`)

### 2.1 Color palette — CSS custom properties (`App.css:1-13`)

| Token | Value | Hex role |
| --- | --- | --- |
| `--bg` | `#090d14` (App.css:2) | Base canvas (near-black navy) |
| `--surface` | `#101722` (App.css:3) | Card / row surface |
| `--surface-2` | `#141d2a` (App.css:4) | Raised surface |
| `--surface-3` | `#192333` (App.css:5) | Raised further |
| `--border` | `rgba(255,255,255,0.075)` (App.css:6) | Hairline borders |
| `--text` | `#f3f6fb` (App.css:7) | Primary text |
| `--muted` | `#8995a8` (App.css:8) | Secondary text |
| `--subtle` | `#5f6b7e` (App.css:9) | Tertiary / labels |
| `--accent` | `#9b7cff` (App.css:10) | Primary purple accent |
| `--accent-2` | `#6e4bff` (App.css:11) | Deep purple |
| `--green` | `#55d6a8` (App.css:12) | Healthy / live / success |
| `--amber` | `#f1b86a` (App.css:13) | Warning / risk |

**Status semantic colors elsewhere in the file (recurring literals):**

- **error/red:** `#d98b8b` text on `rgba(232,111,111,0.06)` bg with `rgba(232,111,111,0.16)` border (`.auth-error`, App.css ~1746–1750); danger buttons `#d98d8d` on `rgba(231,105,105,0.06)` (App.css ~1898).
- **notice/success (green):** `.reward-notice` `#7cc6ab` on `rgba(85,214,168,0.05)` with `rgba(85,214,168,0.13)` border (App.css ~1404–1410).
- **reward-type status tints:** `.reward-type.badge #b59fff`, `.emote #5fd3aa`, `.code #edb96f` (App.css ~1342-1351).

### 2.2 Typography

- **Font stack** (global, `style.css:3-12`): `Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` with `font-synthesis:none`, `text-rendering:optimizeLegibility`, `-webkit-font-smoothing:antialiased`. Monospace code faces: `ui-monospace, SFMono-Regular, Consolas, monospace` (App.css ~1581, 1920).
- **Type scale (all px, very small by design):**
  - Eyebrow kicker: `9px / 700 / letter-spacing:0.13em / text-transform:uppercase`, color `#677388` (`.eyebrow`, App.css ~164-170).
  - Page title `h1`: `23px / -0.035em` (App.css ~172-175).
  - Workspace `h2`: `24px / -0.035em` (App.css ~860-863); hero `h2`: `clamp(25px,2.7vw,36px) / -0.045em` (App.css ~331-334).
  - Section titles: `12px` (`.section-title`, App.css ~1367).
  - Body / list text: **9–12px**; secondary/detail text: **7–9px muted/subtle**; labels on controls: 8–9px uppercase, `letter-spacing:0.04–0.08em`.
  - Primary buttons: `9–10px / 700`.
- **Text color hierarchy:** headings/strong `#aeb7c5–#cbd3df`; body `var(--muted)/#8995a8`; details `#596577`/`#687589`; disabled/inactive `#5f6b7e`.

### 2.3 Spacing & layout rhythm

- **Grid gutters:** `.main` horizontal padding `0 34px` (App.css ~148); `.content-grid` 2 columns `minmax(0,1.15fr) minmax(320px,0.85fr)` gap `13px` (App.css ~453-457); `.metrics` 3 cols gap `12px` (App.css ~405-410).
- **Spacing scale** observed: 2/4/7/8/9/10/11/12/13/14/16/18/22/24/25/34. Section cards margin-bottom `12px`; `workspace-heading` margin-bottom `18px`; panel padding `0 19px`; row padding `9px 15px` / `12px 14px`.
- **Sidebar:** width `224px` (→ `190px` at ≤1080px, → `72px` icon-only at ≤850px), nav item height `43px`, gap `4px` (App.css ~92-112).

### 2.4 Radii (`border-radius`)

- Hero card `16px` (App.css ~278); modal `17px` (App.css ~1526); panels `14px` (App.css ~459); metrics/scan-summary/streak `11–13px`; rows `10–11px`; buttons `8px` (`.primary-button` App.css ~366; `.scan-button` ~1111); pilot badges `5–7px`; pill-shaped (`border-radius:99px`) for `.phase-pill`/badge chips (App.css ~325).

### 2.5 Shadows / glow

- Generic elevated: `box-shadow:0 16px 45px rgba(0,0,0,0.17)` on hero (App.css ~286); modal `0 28px 90px rgba(0,0,0,0.52)` (App.css ~1527).
- Purple glow: vertical `background:linear-gradient(135deg,#8664f7,#6846db)` + `box-shadow:0 8px 25px rgba(104,70,219,0.23)` on `.primary-button` (App.css ~365-371); `.connect-button` `0 10px 25px rgba(104,70,220,0.22)` (App.css ~1710).
- Status dots glow with their color: `.safety-dot` `box-shadow:0 0 12px rgba(85,214,168,0.7)` (App.css ~150-153); `.monitor-dot.running` `0 0 8px rgba(85,214,168,0.45)` (App.css ~1036).
- Brand/logo mark: `linear-gradient(145deg,#b08cff,#6945f6)` + `0 8px 24px rgba(108,71,247,0.28)` + white inner top inset highlight (`.brand-mark`, App.css ~69-77). The same gradient+inset recipe recurs for the orbit core/avatar.
- Ambient background glow: `.shell` radial `circle at 72% -20%, rgba(122,80,255,0.11)` (App.css ~34); `.hero-card::before` radial `circle at 85% 50%, rgba(132,88,255,0.19)` (App.css ~293).

### 2.6 Transitions & animations

- Navigation hover/active `transition:0.16s ease` (`.nav-item`, App.css ~105).
- Modal open: `animation: modal-in 0.16s ease-out` (scale 0.985→1 + fade) (App.css ~1534, keyframes ~2026-2035).
- `.modal-backdrop` uses `backdrop-filter:blur(10px)` over `rgba(3,6,11,0.78)` (App.css ~1537).
- Wait/pulse indicator: `@keyframes pulse` scaling a box-shadow ring 0→14px on a `12px` accent dot, `1.6s infinite` (`.pending-pulse` ~1786, keyframes ~2018-2024).
- Reduces-motion honored via `@media (prefers-reduced-motion: reduce)` zeroing all animations/transitions (App.css ~2107-2116).

### 2.7 Breakpoints

- `max-width:1080px` — sidebar 224→190, main padding 34→22, hero padding-inline 28, orbit dimmed (App.css ~2037-2050).
- `max-width:850px` — sidebar becomes 72px icon rail (hide brand text, nav labels, footer text); hero/`content-grid` collapse to 1 column; orbit hidden (App.css ~2118-2151).
- `max-width:600px` — workspace heading stacks; rows wrap (channel-row, opportunity-row flex-wrap); input full-width (App.css ~2052-2106).
- **Note:** the app targets a desktop Wails window (min 980×680), so ≤600px is essentially never hit in practice; the responsive rules exist for completeness.

---

## 3. Component inventory

All components are plain function components in `src/components/`; styling is global CSS classes (no CSS modules, no Tailwind, no styled-components).

### 3.1 `Chrome.tsx` — shell

- **`Icon({name})`** — inline SVG sprite keyed by string; each icon is a 24×24 viewBox drawn with `<rect>/<circle>/<path>`; styled `.icon` (18px, `stroke-width:1.65`, round caps) (`Chrome.tsx:18-78`, `App.css ~132-139`). Icons: `overview` (4 rounded squares), `channels` (play triangle in ring), `campaigns` (gift), `collection` (target/star), `activity` (activity line), `streaks` (heart+check), `settings` (gear).
- **`Sidebar`** — brand mark + `navItems` map + footer status dot. Props: `activeNav, watchChannel, schedulerRunning, onNavigate, onAccount`. Computes a one-line operation status ("Ready" / "Starting" / "Tracking @x" / "Reward scheduler active") (`Chrome.tsx:80-131`).
- **`TopBar`** — page eyebrow (`WORKSPACE / <TAB>`), dynamic `h1` ("Good evening" on overview, else tab label), health pill, and the **account button** (avatar initial + title/subtitle). The account button is the single entry to auth. `accountTitle`/`accountSubtitle` enumerate status strings (connected reward vs registered vs error vs pending) (`Chrome.tsx:134-220`).

### 3.2 `CampaignWorkspace.tsx` — Campaigns screen

Composed of 4 private sub-components:

- **`CampaignWorkspaceHeader`** — eyebrow + `h2` + two actions ("Explore top 30" secondary, "Scan watchlist" primary gradient).
- **`CampaignInventoryCoverage`** — a `.scan-summary` row of key/value stat tiles (account campaigns, drop rewards, global campaigns, badge events, watch badges) plus coverage notes.
- **`CampaignWorkspaceFeedback`** — renders error/notice banners + empty state gate ("Ready to discover rewards").
- **`CampaignScanResults`** — scan-summary stats, `SchedulePanel`, and the `.opportunity-list` table of `opportunity-row`s (thumb, title, type badge, requirement).
- **`SchedulePanel`** — the "One active viewer, one reserve" explainable plan: `active-watch-bar` (green dot + tracking status + Verify/Stop buttons), then `.schedule-slots` grid of ACTIVE/RESERVE slots each with mode chip, channel, reason+score, and an "Open in companion" action on the ACTIVE slot (`CampaignWorkspace.tsx:312-389`).

### 3.3 `CampaignCatalog.tsx` — reusable card-catalog widgets

- **`AccountCampaignCatalog`** — renders DROP and REWARD campaign rows from inventory; returns `null` if empty.
- **`BadgeCampaignCatalog`** — renders creator-badge campaign reward rows; returns `null` if empty.
Both share the `opportunity-row` visual (thumb, copy, type chip, requirement), used identically on the Campaigns and Collection screens. This is the closest thing to a reusable "campaign/reward row" component and is the template TDM should generalize.

### 3.4 `AccountModal.tsx` — the auth/account dialog

A fully-featured accessible modal with three internal views:

- **`AccountModal`** (dialog shell) — `role="dialog"`, `aria-modal`, focus trap (Tab/Shift+Tab wraps), `Escape` closes, backdrop `onMouseDown` self-only close, focuses first focusable on open and **restores previous focus** on close (`AccountModal.tsx:29-66`).
- **`ConnectAccountView`** — not-connected state: copy, `risk-note` (amber warning box), consent checkbox, connect button, auth divider, client ID/secret inputs, disconnect.
- **`ConnectedAccountView`** — connected state: `connected-identity` (large gradient avatar + label), credential note, scope chips, risk consent if reward not connected, registered-app connect inputs, danger "Disconnect".
- **`PendingAccountView`** — device-code flow: pulsing indicator, one-time code as a big copyable dashed box (`device-code`, `cursor:copy`), "Open Twitch again", "Discard resume", "Cancel", expiry note.
- **`AccountModalStatus`** — shared error/notice banner stack at the top of the modal.

Props pattern: the modal receives **all** state and **all** callbacks as props (no context, no internal data fetching) — `AccountModal.tsx:1-28`. This is the canonical "controlled modal" pattern.

### 3.5 Recurring mini-templates (in App.tsx JSX + CSS)

- **Stat tile** (`.scan-summary > div`, `.metrics article`): `<small>label</small><strong>value</strong>`.
- **Row** (`channel-row`, `activity-row`, `opportunity-row`): thumbnail/icon + copy (strong/small) + status chip + action button(s).
- **Status chip** (`.channel-state`, `.reward-type`, `.schedule-slot > span`): tiny uppercase padded label with semantic tint.
- **Progress bar** (`<progress className="mini-progress">`): 2px tall, gradient fill `linear-gradient(90deg,#7454de,#9b7cff)` (App.css ~1254-1265).

---

## 4. Interaction patterns

1. **Modals** — one dialog at a time, controlled (state-drawn `{showAccount && <AccountModal/>}`), `aria-modal`, full keyboard support via code (not native `<dialog>`), focus restore on close. Consumer never renders modal internals directly.
2. **Empty states** — `.workspace-empty`: 410px min-height, dashed `1px rgba(255,255,255,0.085)` border, centered 34px accent icon → `h3` → muted description → primary CTA button (`App.css ~1119-1149`). Also inline `.list-empty` for zero rows inside an existing list.
3. **Loading states** — (a) **button-label mutation**: `Syncing…/Scanning…/Checking…/Claiming…/Verifying…/Refreshing…` with `disabled` (e.g. `App.tsx:1860,1874,2012`); (b) **`.workspace-empty` as the initial progress screen** (e.g. "Synchronizing your collection"; `App.tsx:2381`); (c) **health pill "Starting"** when DB not ready; (d) pending/pulse dot for waiting flows.
4. **Error states** — two flavors: inline **`.auth-error`** (red, `role="alert"`) rendered per-feature next to its section, and top-of-main aggregated banners for global failures (bootstrap error, automation/inventory/watch/channels/streak/badge errors cross-rendered when not on that tab, `App.tsx:1298-1361`). Errors are set as plain strings from caught exceptions.
5. **Notice / toast** — `.reward-notice` (green, `role="status"`, `aria-live="polite"`) used for transient success/info (e.g. "Reward claimed and verified.", "Authorization code copied."). There is no auto-dismissing toast system — notices persist until the next state change. TDM may want true toasts; see §6.
6. **Confirmation dialogs** — no generic confirm component; destructive actions ("Remove", "Disconnect") are single-click in-context (`removeChannel`, `disconnect`). Risk consent is a per-action-gated checkbox in the auth modal rather than a popup.
7. **Session-expiry / reauthorization recovery** — elegant: actions call `ensureRewardSession(...)`; if the reward session is missing/expired they queue a `PendingAuthIntent`, open the modal, and **auto-resume the original action** once the session reconnects (`App.tsx:249-275`, `auth.ts`). This is the single most sophisticated UX flow to preserve.
8. **Explainability** — the scheduler shows *why*: every slot carries a reason + score; every switch gets a "Every switch has a reason." explainability bar (`App.tsx:3198`).

---

## 5. Translation map → TDM screens

TDM screens today (from BRIEF-01 + `lang/<lang>.json` `gui.*`): **Main** (status + channels + drop progress), **Inventory**, **Settings**, **Help**, plus system **tray** and a **login** flow. Farmer surface mapping:

| TDM screen (today) | Farmer counterpart | Farmer component/pattern to copy |
| --- | --- | --- |
| **Main / dashboard** (status, drop progress, channels, timers) | **Overview** (`activeNav==="overview"`) | Hero card + phase pill + CTA; `.metrics` stat row (Drop progress, watch sessions, reward inventory); `.content-grid` of two panels (**Watch slots** = live/active channels with reason, **Reward progress** = per-drop `<progress>` list); `.boundary-bar` safety footer. `buildDashboardSlots` (`operational.ts`) produces the slot rows. |
| **Channel list / switch** | **Channels** screen | `.channel-row` table: avatar initial, display name, `@login · priority N`, `.channel-state` enabled/paused chip, `.playback-state` live/offline chip, per-row Check-live / Pause+Enable / Remove actions; `.monitor-bar` scheduler control w/ live dot; `companion-onboarding` pairing panel; `.workspace-empty` watchlist states. |
| **Drop progress / campaign progress** | **Collection** + **Campaigns** | The **drop-progress widget** = `progress.mini-progress` + `rewardProgressPercent()` (`operational.ts:126`) rendered in `.dashboard-rewards`; the full **campaign discovery UI** = `CampaignWorkspace` scan-summary tiles + `SchedulePanel` + `opportunity-list` from a public `ScanPublicOpportunities` (TDM has no equivalent — **new**). |
| **Inventory** | **Collection** | `.scan-summary` stat tiles (in progress / claimable / earned), `.opportunity-list` of `opportunity-row` rewards with state/requirement + `Claim`/`Copy code` row actions, earned-rewards + creator-badge sections, "Open Twitch Drops inventory" external link. |
| **Priority list / excluded list** | (no direct equivalent — found in `operational.ts` channel `priority` string only) | TDM keeps its priority/exclude lists; suggestion: render as `.opportunity-list`-style rows with drag/checkbox + `channel-state`-style chips (§ Consistency/missing). |
| **Settings** | **Account modal** + "Settings" nav item stub | Farmer's settings are split: account/auth in the modal; app prefs minimal. TDM needs a fuller settings screen — map Farm's `workspace-heading` + `coverage-note` + labeled-form styles onto it. |
| **Login** | **AccountModal** (`ConnectAccountView`/`PendingAccountView`) | Copy slam-dunk: "Connect reward session" primary + device-code flow (big copyable code, open-twitch-again, discard-resume, cancel), consent checkbox gated. TDM's username/password login maps to the registered-app `clientID/secret` form styling. |
| **Tray icon** | Wails single-instance + window focus (`main.go:onSecondInstanceLaunch`) | No tray in Farmer; TDM preserves tray via its own mechanism (Out of scope for this brief, see 05/07). |

**What TDM must add that Farmer has no direct equivalent for:**

1. **Drop-progress as a first-class dashboard widget** (Farmer has it on Overview via `dashboard-rewards` — TDM can lift it wholesale since it already has drop progress; just restyle to `.mini-progress`).
2. **Campaign discovery UI** (TDM currently shows only configured campaigns; Farmer's public/top-30 scan + explainable schedule panel is new surface).
3. **A real priority-list editor** (Farmer only shows a priority number; TDM's priority management needs its own ordered-list widget; reuse `opportunity-row` + `channel-state` chips).
4. **Auto-resuming pending-action** after re-auth (transfer `PendingAuthIntent` idea to TDM's login).
5. **True configurable settings screen** (Farmer's Settings is a stub; TDM's existing settings must keep their richness).

---

## 6. Consistency notes

### Copy verbatim from Farmer

1. **CSS approach:** one global `App.css` (2,147 LOC) + a tiny `style.css` reset. Custom properties at `:root` for the palette; everything else are plain descendant class selectors. No CSS-in-JS, no modules, no preprocessor. Cheap, unit-testable, trivially portable — **copy this file's structure wholesale**.
2. **Dark palette:** the 12 `:root` vars (§2.1) are the entire color system; all shades/variants derive from them. Adopt these exact hexes.
3. **Type discipline:** tiny 7–12px type, uppercase micro-labels, `letter-spacing` on eyebrow/status text, tight negative tracking on headings, `Inter` stack. This is what gives the "dense dashboard" look.
4. **Layout rhythm:** 224px sidebar + single scroll column; consistent gutters; 2-col content grid; stat tiles; card/row/pill taxonomy. Reuse `.metrics`, `.scan-summary`, `.opportunity-row`, `.channel-row` grid recipes.
5. **The seatbelt/stale-response guard**, button-label mutation, `role="alert"`/`role="status"` banners, `prefers-reduced-motion` handling, focus-trap modal — all best-practice patterns worth replicating in whatever TDM rewrite framework lands.
6. **Gradient CTAs:** primary buttons are always the purple `linear-gradient(135deg,#8664f7,#6846db)` + glow; secondary buttons are `rgba(155,124,255,0.08)` border/underline style. Keeps visual hierarchy consistent.

### Improvements TDM should make (Farmer's weaknesses)

1. **No real toast system** — notices persist until overwritten; TDM should add auto-dismissing, stacked toasts (kept in a corner of `.main`) while keeping the inline banner vocabulary.
2. **`activeNav` as one flat switch in a 2,588-line `App.tsx`** — TDM should split each screen into its own route/component file rather than mirroring the monolith.
3. **No dark/light theming toggle**; palette is hard-coded (fine for a native tool, but TDM may want the `:root` vars exposed so a future theme swap is a variable change).
4. **Inline-sprite `Icon` with manual path data** — brittle and list-defined; TDM could use a proper icon set, but keep the same 18px stroked style.
5. **Single account assumption** is deeply baked in; TDM is single-account too, so acceptable — just document it.
6. **Accessibility is decent (focus trap, aria) but untested**; TDM should add an a11y pass (focus order on mobile breakpoints, contrast at 9px text is borderline).

---

## 7. Appendix — exact token line index (App.css / style.css)

- Palette `:root`: `App.css:1-13`
- Global font/root: `style.css:1-22`; focus ring `style.css:25-27`; selection `style.css:29-32`
- `.shell` grid + bg glow: `App.css:27-40`
- `.sidebar`: `App.css:42-61`; `.brand-mark`: `App.css:69-85`; `.nav-item`: `App.css:92-124`; footer dot: `App.css:150-155`
- `.main`: `App.css:145-152`; `.topbar`: `App.css:153-161`; `.eyebrow`: `App.css:164-170`
- `.hero-card`: `App.css:266-297`; `.phase-pill`: `App.css:318-327`; `.primary-button`: `App.css:366-371`
- `.orbit`/reward nodes: `App.css:381-404`
- `.metrics`: `App.css:405-450`; `.content-grid`: `App.css:453-457`; `.panel`: `App.css:458-463`
- slot/explainability: `App.css:522-597`; mini progress: `App.css:1254-1265`
- `.boundary-bar`: `App.css:738-795`
- `.campaign-workspace`/heading: `App.css:821-884`; `.channel-search`: `App.css:882-918`; `.monitor-bar`: `App.css:924-966`
- rows: `.activity-row` `App.css:970-1000`; `.channel-row` `App.css:1002-1060`; `.opportunity-row` `App.css:1302-1365`
- `.scan-summary`: `App.css:1151-1167`; `.workspace-empty`: `App.css:1119-1149`
- `.streak-*`: `App.css:1169-1248`; `.schedule-*`: `App.css:1267-1300`
- `.reward-notice`: `App.css:1404-1410`; `.list-empty`: `App.css:1411-1415`
- Modal: backdrop `App.css:1417-1437`; `.account-modal` `App.css:1438-1452`; header/close `App.css:1453-1470`; input `App.css:1549-1594`; credential/risk notes `App.css:1595-1660`; `.auth-error` `App.css:1746-1750`; `.device-code` `App.css:1798-1806`; buttons `App.css:1701-/.secondary 1820+`
- `@keyframes pulse` `App.css:2018-2024`; `modal-in` `App.css:2026-2035`; media queries `App.css:2037,2052,2107,2118`

Framework footnotes: package.json pins React ^19, Vite ^7, TypeScript ^5.6, Vitest ^4 (`frontend/package.json`); Vite config is a thin React plugin + an empty-`dist/.gitkeep` embed helper (`vite.config.ts`); window is 1280×820/min 980×680 with navy background (`main.go:181-200`). No screenshots ship in the repo; all visual claims derive from CSS/JSX.
