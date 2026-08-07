# 08 — UX Design & Design Tokens for TwitchDropsMiner Rewrite

**Agent:** ux-design | **Brief:** `BRIEF-08-ux-design.md`
**Output:** `08-UX-DESIGN-TOKENS.md` | **Date:** 2026

---

## 0. Executive summary

This report defines the **target UX and design system** for the rewritten TwitchDropsMiner, and is intended to feed the master plan (`00-UI-REDESIGN-MASTER-PLAN.md`). It is built on **four inputs**:

1. **Farmer's extracted design language** — read directly from `/home/pyro1121/Documents/Farmer/frontend/src/App.css` (2,147 LOC) and `style.css`. This is the reference design the rewrite is adopting (`docs/ui-review/00-INDEX.md` names Farmer as the adopted visual/architectural language). The upstream cross-check doc `03-FARMER-UI-REFERENCE.md` did not exist at review time, so tokens were extracted directly from the CSS.
2. **The current TwitchDropsMiner UI** (`gui.py`, 2,971 LOC Tkinter) — inventoried to map existing screens/settings to the new system (tabs: Main, Inventory, Settings, Help; tray icon; ~10 settings).
3. **Cited 2025/2026 UX research** for desktop dark-theme dashboard/daemon apps (Material 3, shadcn/ui + Radix, Fluent 2, Apple HIG, WCAG 2.2/APCA, Carbon, NN/g).
4. **Twitch's own visual language** — users are Twitch natives, so the palette borrows Twitch Purple.

Key decisions up front:

- **Adopt Farmer's token set as the canonical dark palette** (its `:root` custom properties), extend it with the missing status/motion/typography/spacing scales TDM needs, and reconcile with TDM's existing raw color usage (green/yellow/red text, tray icon states).
- **Accent = periwinkle violet `#9b7cff`** (Farmer's accent), not literal Twitch Purple `#9146FF`. Rationale: Farmer's accent is softer and already proven for an automated "reward optimizer" product; Twitch's raw brand purple is reserved for sparse high-priority signal, following both Farmer's usage and Twitch's "[purple reserved for high-priority interactive accents]" guidance ([Beyond Purple, Twitch](https://blog.twitch.tv/en/2019/12/03/beyond-purple/), accessed 2026).
- **Tray-first, interruption-minimal** "AFK 24/7" mindset: the app's default resting state is the system tray; the main window is a live status surface that never demands attention for routine progress.
- **All contrast claims cite WCAG 2.2** (4.5:1 normal, 3:1 large & non-text); APCA is used as a secondary perceptual check, not a conformance substitute ([WCAG 2.2](https://www.w3.org/TR/WCAG22/), accessed 2026).

---

## 1. Design tokens

### 1.1 Token architecture (semantic layers, not hard-coded hex)

Per 2025 best practice, the system uses **two token layers**: primitive scales (color stops, space units, type sizes) mapped to **semantic roles** (surface, text, accent, status) that are swapped per theme. This is exactly how [shadcn/ui's theming](https://ui.shadcn.com/docs/theming) (semantic `--background`/`--foreground`/`--primary` tokens overridden under `.dark`) and [Fluent 2's alias tokens](https://fluent2.microsoft.design/design-tokens) (e.g. `colorNeutralBackground1` aliased per theme) work, and it matches how both [Material 3](https://m3.material.io/foundations/design-tokens) and [USWDS](https://designsystem.digital.gov/design-tokens/) structure tokens. Components reference semantic roles only; the theme layer owns the values. (Accessed 2026.)

### 1.2 Canonical palette — extracted from Farmer

These are the **verbatim** `:root` custom properties from `Farmer/frontend/src/App.css` (lines 1–21). This is the ground truth palette.

| Semantic token | Value | Role |
| --- | --- | --- |
| `--bg` | `#090d14` | App background (deep navy-black) |
| `--surface` | `#101722` | Base card/surface |
| `--surface-2` | `#141d2a` | Raised surface (hover, sub-panel) |
| `--surface-3` | `#192333` | Highest surface |
| `--border` | `rgba(255,255,255,0.075)` | Hairline border/dividers |
| `--text` | `#f3f6fb` | Primary text / foreground |
| `--muted` | `#8995a8` | Secondary text |
| `--subtle` | `#5f6b7e` | Tertiary text / captions |
| `--accent` | `#9b7cff` | Accent (periwinkle violet) |
| `--accent-2` | `#6e4bff` | Deep accent (hover/press, gradient end) |
| `--green` | `#55d6a8` | Success / live / operational |
| `--amber` | `#f1b86a` | Warning / caution |

Plus sheet-level tokens from `style.css`: background `#090d14`, text `#f3f6fb`, accent `#9b7cff` (focus ring), selection `rgba(155,124,255,0.45)`.

### 1.3 Extended status palette (new for TDM)

Farmer only ships green/amber (its UI is a status dashboard with a healthy/unhealthy health dot). TDM needs a fuller status semantic set. Values chosen and cited against WCAG:

| Semantic token | Value | WCAG contrast vs `--bg` (#090d14) | Used for |
| --- | --- | --- | --- |
| `--success` (live/operational) | `#55d6a8` | ≈ 10.3:1 | Watching, connected, healthy |
| `--warning` | `#f1b86a` | ≈ 10.1:1 | Degraded, rate-limit near, countdown advisory |
| `--error` | `#e86f6f` | ≈ 7.6:1 | Auth failure, websocket drop, fatal |
| `--idle` | `#8995a8` | ≈ 6.0:1 | Not watching, waiting |
| `--offline` | `#5f6b7e` | ≈ 3.9:1 | Backend unreachable, daemon stopped |
| `--info` | `#7fb4e8` | ≈ 7.1:1 | Neutral informational notices |

**Contrast rationale:** all status text colors exceed WCAG 2.2 **4.5:1** for normal text ([WCAG 2.2 SC 1.4.3](https://www.w3.org/TR/WCAG22/)); status *dots/indicators* are paired with shape + text label, and the dot itself needs only **3:1** non-text contrast ([WCAG 2.2 SC 1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html)). `--offline` at 3.9:1 is intentionally the dimmest because it is `--subtle`-adjacent (tertiary text tier) — see §1.5 note. All values were spot-checked for the 4.5:1 threshold; exact ratios above are approximations computed from sRGB luminance and should be **re-verified with a contrast tool during implementation** (see §5). (Accessed 2026.)

**Tray-status icon mapping:** TDM currently swaps tray glyphs by state — `pickaxe` (idle), `idle.ico`, `error.ico`, `maint.ico` (`gui.py` `TrayIcon._icon_images`). In the rewrite these map to: `--info`/idle → neutral, `--error` → error icon, `--warning`/maintenance → maint icon, `--success` → watching. Reconcile table in §1.8.

### 1.4 Typography

Farmer's font stack (style.css): `Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;` with `font-synthesis: none` and `text-rendering: optimizeLegibility`. Adopt this verbatim. Inter is the brand UI face; the fallback chain covers Windows (Segoe UI), macOS (SF), and Linux.

**Scale.** Farmer uses a tight, small type scale (its dashboard is dense). Recommended **rem-based** scale so user/OS text scaling works ([W3C design-system typography guidance](https://design-system.w3.org/styles/typography.html)) — note Farmer hard-codes px; the rewrite should prefer `rem` for the shared tokens while keeping Farmer's look. The row body sizes in Farmer are unusually small (7–13px) for a dense dashboard; for accessibility TDM should floor **readable text at 12px (0.75rem)** minimum and prefer 13–14px for body, since 2025 guidance sets body at **16px / 1rem** and warns thin/small text loses legibility in dark mode ([Apple HIG Typography](https://developer.apple.com/design/human-interface-guidelines/typography), [Mantlr dark-mode guide](https://mantlr.com/blog/dark-mode-design-guide-color-typography-accessibility), accessed 2026).

| Role | Size | Weight | Line-height | Letter-spacing | Use |
| --- | --- | --- | --- | --- | --- |
| `display` | clamp(25px, 2.7vw, 36px) | 900 | 1.05 | −0.045em | Hero / onboarding headline (Farmer `.hero-card h2`) |
| `h1` / page title | 23px | 700 | 1.2 | −0.035em | Top bar page title (Farmer `h1`) |
| `h2` / section | 24px | 700 | 1.2 | −0.03em | Campaign/collection headings |
| `h3` / card | 13–15px | 600 | 1.3 | −0.015em | Panel headings, empty-state titles |
| `body` / row title | 14px | 500–600 | 1.4 | — | Channel/game/reward titles |
| `body-secondary` | 13px | 400 | 1.5 | — | Descriptions, explanations |
| `caption` | 12px | 500 | 1.45 | ±0.01em | Metadata, timestamps |
| `micro` | 11px | 600–800 | 1.4 | +0.06–0.13em | Labels, eyebrows, status pills (Farmer uses 7–9px; raised to floor) |
| `mono` | 12–21px | 600–700 | 1.2 | +0.12em | Device/pairing codes, tokens, logs |

**Weight note:** use medium/bold for emphasis, avoid very thin weights in dark mode (light-on-dark thin glyphs blur) — consistent with Apple HIG and the dark-mode guide above.

### 1.5 Spacing scale

Administer on an **8px rhythm with a 4px base**, the documented 2025 convention ([CMS design-system spacing](https://design.cms.gov/foundation/spacing/), accessed 2026). Farmer's actual paddings cluster around 8/12/14/16/18/22/24/34px; the token scale below normalizes and harmonizes those:

| Token | Value | Typical use |
| --- | --- | --- |
| `--space-1` | 4px | Dense pill padding, icon gaps |
| `--space-2` | 8px | Control-internal gap (row action ≤ 8) |
| `--space-3` | 12px | Between rows, card padding (small) |
| `--space-4` | 16px | Card padding, sidebar padding (default) |
| `--space-5` | 20–24px | Section gaps, panel padding |
| `--space-8` | 32px | Page/section separation |
| `--space-12` | 48px | Empty-state spacing, large hero padding |

Sidebar: `padding: 25px 16px 18px` (Farmer). Main content: `padding: 0 34px 30px`. These are harmonized to `--space-4`/`--space-8` tokens.

### 1.6 Radii, shadows, borders, transitions, motion

| Token | Value | Source / use |
| --- | --- | --- |
| `--radius-sm` | 6–8px | Buttons (Farmer 8px), slots, row-action |
| `--radius-md` | 10–13px | Rows, panels, metrics cards (Farmer 10–14px) |
| `--radius-lg` | 16–17px | Hero card (16px), modal (17px) |
| `--radius-full` | 999px | Pills, dots, phase badges |
| `--shadow-card` | `0 16px 45px rgba(0,0,0,0.17)` | Hero/card elevation (Farmer) |
| `--shadow-modal` | `0 28px 90px rgba(0,0,0,0.52)` | Modal depth (Farmer `.account-modal`) |
| `--shadow-accent` | `0 8px 25px rgba(104,70,219,0.23)` | Primary button glow (Farmer) |
| `--border` | `rgba(255,255,255,0.075)` | Global hairlines (Farmer) |
| `--border-accent` | `rgba(155,124,255,0.13–0.23)` | Accent-tinted frames (Farmer hero/modal) |

**Motion/transitions:**

| Token | Value | Use |
| --- | --- | --- |
| `--ease-default` | `0.16s ease` | Hover/press micro-interactions (Farmer `.nav-item`, `.modal-close`) |
| `--ease-modal` | `0.16s ease-out` with scale(0.985)→1 | Modal enter (Farmer `modal-in`) |
| `--pulse` | 1.6s infinite | Live/waiting pulse dot (Farmer `pulse`), device-code pending |

**Reduced motion:** Farmer ships a `prefers-reduced-motion: reduce` block collapsing all animation/transition to 0.01ms. **This must be preserved** as a core token policy — it is a WCAG 2.2 SC 2.3.3 friendly default ([WCAG 2.2 Motion](https://www.w3.org/TR/WCAG22/)) and 2025 best practice ([Apple HIG Dark Mode](https://developer.apple.com/design/human-interface-guidelines/dark-mode), accessed 2026).

### 1.7 Reconcile with Farmer tokens (list both → reconcile)

| Concern | Farmer (canonical) | TDM current (Tkinter) | Reconcile |
| --- | --- | --- | --- |
| Background | `#090d14` | OS-native Tk bg / `dark_mode` toggle | **Adopt farmer `--bg`** as dark bg; keep light mode as a `Light` token set, not a different palette philosophy. TDM currently just flips a coarse `dark_mode` bool (`SettingsPanel.update_dark_mode`); the rewrite should swap a full semantic token set. |
| Surface | `#101722`/`#141d2a`/`#192333` | ttk default | **Adopt 3-step surface scale.** |
| Text | `#f3f6fb` / `#8995a8` / `#5f6b7e` | `green/yellow/red.TLabel` only | **Adopt 3-tier text**; replace raw label colors with semantic status tokens. |
| Accent | `#9b7cff` / `#6e4bff` | none (native blue) | **Adopt periwinkle accent.** |
| Success | `#55d6a8` (green) | `green` (tk `~#008000`, poor contrast) | **Adopt `#55d6a8`** for all success/live; fix TDM's raw `foreground="green"` (e.g. `ChannelList`/`CampaignProgress`) which fails 4.5:1. |
| Warning | `#f1b86a` (amber) | `goldenrod` (`#daa520`) | **Adopt `#f1b86a`**; map goldenrod→amber token. |
| Error | none in Farmer | `red` (`#ff0000`) | **Add `--error #e86f6f`**; replace raw red (7.6:1 is acceptable but the token system prevents drift). |
| Idle/offline | `--muted`/`--subtle` | `idle.ico` tray state | **Map tray-idle→`--muted`; offline→`--subtle`.** |
| Focus ring | `#9b7cff` 2px + 2px offset | Tk dotted `Notebook.focus` (explicitly removed in `gui.py`) | **Restore a visible focus ring** using `--accent` (Farmer `style.css` has `:focus-visible { outline: 2px solid #9b7cff; outline-offset: 2px; }`); see §5. |

### 1.8 Twitch-brand reconciliation

TDM's audience is Twitch natives. Twitch brand color = **`#9146FF` (RGB 145,70,255)** with `#F0F0FF` Ice and `#000000` Black as the core palette ([Twitch Corporate Identity Manual](https://fliphtml5.com/snxys/wvbo/Twitch_Corporate_Identity_Manual/)); Twitch's own dark theme uses `#201C2B` background with `#6441A4` accent and `#E5E3E8` text ([Twitch extension design docs](https://dev.twitch.tv/docs/extensions/designing/), accessed 2026).

**Decision:** keep Farmer's periwinkle (`#9b7cff`/`#6e4bff`) as the global accent for consistency with the adopted reference UI, but weave Twitch's identity in deliberately:

- **Primary CTA / "Go live / Watch" buttons** may use a stronger Twitch-purple-leaning gradient (`#8a63f2 → #6e4bff`) to read as "the Twitch action."
- The **login/account** affordance and any **Twitch-connected** badge use Twitch Purple `#9146FF` as a "Twitch brand" signal, distinct from the app's neutral periwinkle accent.
- Avoid literally replicating Twitch logo marks per Twitch trademark guidance ([Trademark Guidelines](https://legal.twitch.com/en/legal/trademark/)); a decorative "drop/badge" glyph (Farmer's `.drop-node` uses `#6ee0b9` on `#19352f`) stays generic.

---

## 2. Layout blueprint (information architecture)

### 2.1 Global shell

Adopt Farmer's **sidebar + main** grid shell (`grid-template-columns: 224px minmax(0,1fr)`), with the safety/status footer. TDM's current 4 tabs (Main, Inventory, Settings, Help) map to Farmer's 7 nav pages.

**Farmer nav → TDM page map:**

| Farmer nav (`navItems`) | TDM mapping |
| --- | --- |
| Overview | **Dashboard** (merge TDM Main-tab: status bar, websocket status, login status, campaign progress, current channel) |
| Channels | **Channels** (TDM `ChannelList` grid) |
| Streaks & Weekly | **Weekly** (streak counts / availability — new, from websocket/twitch data; TDM tracks per-campaign) |
| Campaigns | **Drops/Campaigns** (TDM `CampaignProgress` + inventory campaign cards) |
| Collection | **Inventory** (TDM `InventoryOverview`, currently tab 2) |
| Activity | **Activity log** (TDM `ConsoleOutput` turned into a structured activity feed + raw log viewer) |
| Settings | **Settings** (TDM `SettingsPanel`; currently tab 3) |
| (Help) | **Help / About** folded into a top-bar ? action or sidebar footer |

- **Top bar** (Farmer `.topbar`): eyebrow + page title on left; on right a **health dot + account button**. TDM's login status lives here as the account button (Farmer pattern) rather than a floating form.
- **Sidebar footer**: operational status (Farmer `safety-dot` + "Operational/Ready/Starting/Tracking @channel"). This is TDM's splat of websocket/status info — moved to a single always-visible footer.

### 2.2 Wireframe — Dashboard (Overview)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ▍ F  TwitchDropsMiner      │  ● Operational  Tracking @xqc        [acct]│
│       drops optimizer      │                                            │
│ ┌───────┐ ┌────────────────────────────────────────────────────────────┐│
│ │Overview││  Good evening                                        [accnt]││
│ │Channels││  ◉ Drop farming is live                                   ││
│ │Weekly  ││                                                           ││
│ │Drops   ││  ┌────────────────── hero ────────────────────────────────┐││
│ │Inventory││  │ [WATCHING GAME]   [current campaign hero]              ││
│ │Activity ││  │  Eternights: 2h 14m watched   ▓▓▓▓▓░░ 62%  (3/5 drops) ││
│ │Settings ││  └────────────────────────────────────────────────────────┘││
│ │         ││  ┌ summon-bar: ● active watch bar  [Pause] [Change]      ┐││
│ │         ││  ┌─ Drops / Campaigns ──────────┬─ Metrics ─────────────┐││
│ ─────────── │  │ CampaignCard   ▓▓░ 62%      │ Watching  2h14m       │││
│ ● Operational│  │ CampaignCard   ░░░ -        │ Droplets  3/5          │││
│  Ready       │  │ CampaignCard   ▓▓▓ 100% ✓   │ Active cams 1          │││
│              │  └─────────────────────────────┴───────────────────────┘││
└─────────────────────────────────────────────────────────────────────────┘
```

**Priority order (top→bottom):** (1) hero = current campaign + drop progress bar, (2) active-watch summon bar with Pause/Change, (3) campaign cards grid, (4) side metrics. The hero carries TDM's "1 campaign being farmed now" message, mirroring Farmer's `.hero-card` + `.active-watch-bar` + `.metrics`.

### 2.3 Wireframe — Channels

```
┌─ Channels ──────────────────────────────────────────────────────────────┐
│ eyebrow CHANNELS                          [🔍 search] [+ Add channel]    │
│ h1  Channels                                             [Import follows]│
│ ┌ Monitor bar ───────────────────────────────────────────┐               │
│ │ ● monitor running · policy: priority only  [Pause]     │               │
│ └──────────────────────────────────────────────────────────┘              │
│ ┌ ChannelRow ──────────────────────────────────────────────────────────┐  │
│ │ [FX]  channel-avatar  @channel-name    STATE:●●watching  live   [−]  │  │
│ │                         2h30m · Campaigns:3              progress    │  │
│ └──────────────────────────────────────────────────────────────────────┘  │
│ ┌ ChannelRow (disabled, opacity .56) ───────────────────────────────────┐ │
│ │ [GG]  @channel2          STATE:○idle              paused      [−]     │ │
│ └──────────────────────────────────────────────────────────────────────┘  │
│                                                          [+ Add channel]  │
└──────────────────────────────────────────────────────────────────────────┘
```

Columns per Farmer `.channel-row` grid: `38px avatar | copy | state | playback-state | (row-action) | remove`. Disabled rows use `.channel-row.disabled { opacity:.56 }` and remove/restore actions.

### 2.4 Wireframe — Drops / Campaigns

```
┌─ Drops ─────────────────────────────────────────────────────────────────┐
│ eyebrow CAMPAIGNS                                                     │
│ h1  Campaigns                       [Scan public] [Scan watchlist]      │
│ ┌ scan-summary: Total Drops 42·Active 3·Upcoming 12·Expired 9·Claimed 18─┐│
│ ┌ CampaignCard ─────────────────┐  ┌ CampaignCard ────────────────────┐ │
│ │ [img] Game: Eternights         │  │ [img] Game: Honkai: Star Rail    │ │
│ │   3 rewards · 2h to go         │  │   4 rewards · ending soon ●      │ │
│ │   ▓▓▓▓▓▓▓░ 62%  3/5           │  │   ▓▓▓▓░░░░ 41%  2/5  [Claim]     │ │
│ └───────────────────────────────┘  └──────────────────────────────────┘ │
│ ┌ list-empty: No eligible campaigns — [Scan for drops]                ┐  │
└──────────────────────────────────────────────────────────────────────────┘
```

Card grid matches Farmer `.streak-card`/`.reward-thumb` + `.mini-progress`. Each card shows thumbnail, game, rewards, progress bar, earn-progress, and a Claim action when a drop is complete. Filtering (not-linked/upcoming/expired/excluded/finished from TDM `InventoryOverview._filters`) becomes card-grid checkboxes or a segmented control.

### 2.5 Wireframe — Inventory

```
┌─ Inventory ─────────────────────────────────────────────────────────────┐
│ eyebrow COLLECTION                                                      │
│ h1  Inventory · 42 rewards                                  [Sync] [Refresh]│
│ ┌ filter bar: [x]Upcoming [ ]Expired [ ]Excluded [ ]Finished [ ]Not linked┐
│ ┌ OpportunityRow ─────────────────────────────────────────────────────┐ │
│ │ [img] Reward name            TYPE:badge   ● progress  2h/4h  [Claim]│ │
│ └─────────────────────────────────────────────────────────────────────┘ │
│ ┌ OpportunityRow ─────────────────────────────────────────────────────┐ │
│ │ [img] Emote name             TYPE:emote   ▓▓▓ 100% ✓   [Copy code]   │ │
│ └─────────────────────────────────────────────────────────────────────┘ │
│ ┌ list-empty: Nothing to show here. [Sync reward inventory]            ┐ │
└──────────────────────────────────────────────────────────────────────────┘
```

Maps TDM `InventoryOverview` (component grid of `CampaignDisplay` + per-campaign drop labels) to Farmer's `.opportunity-row` list (`40px thumb | copy | 110px type | 105px requirement`). Reward-type pills mirror Farmer `.reward-type.badge/emote/code`.

### 2.6 Wireframe — Settings

```
┌─ Settings ──────────────────────────────────────────────────────────────┐
│ eyebrow SETTINGS                                                        │
│ h1  Settings                        [Account · @user ▾]                 │
│ ┌ General ────────────────────────┐ ┌ Account / Twitch ───────────────┐ │
│ │ Language            [en ▾]      │ │ ● Connected @user               │ │
│ │ Launch at startup   [on]        │ │   scopes: channel_read, chat     │ │
│ │ Minimize to tray    [on]        │ │ [Reconnect] [Sign out]           │ │
│ │ Tray notifications  [on]        │ │ ┌ risk-note: session consent   ┐ │ │
│ │ Dark mode           [on]        │ └──────────────────────────────────┘ │
│ │ Priority mode [ending soonest ▾]│ ┌ Advanced ─────────────────────────┐ │
│ │ Proxy        [http://______]    │ │ Badges & emotes [on]              │ │
│ └─────────────────────────────────┘ │ Drops check [on]                  │ │
│ ┌ Priority games ──┐ ┌ Exclude ──┐   │ [Reload campaigns]               │ │
│ │ Eternights       │ │ Game B     │   └─────────────────────────────────┘ │
│ │ + add            │ │ + add      │                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

Maps all TDM `SettingsPanel` fields (`_SettingsVars`: autostart, language, proxy, tray, dark_mode, priority_mode, tray_notifications, enable_badges_emotes, available_drops_check), grouping them under **General / Account / Priority & Exclude / Advanced** sections (Farmer uses `LabelFrame`-style grouped panels). Login/account moves to Account section + top-bar button with modal (Farmer `.AccountModal`).

### 2.7 Wireframe — Activity / Log

```
┌─ Activity ──────────────────────────────────────────────────────────────┐
│ eyebrow ACTIVITY                                [Copy support bundle] [▾]│
│ h1  Activity                                                            │
│ ┌ ActivityRow: [icon] Drop claimed  Eternights · 3/5 · +reward  12:04 ┐ │
│ ┌ ActivityRow: [icon] Playback confirmed @channel                 11:58 ┐ │
│ ┌ ActivityRow: [icon] Rate limit warning  Retrying in 60s         11:40 ┐ │
│ ┌ Collapsible: Raw console log (mono, scrollable)                    ┐  │
└──────────────────────────────────────────────────────────────────────────┘
```

Structured feed (Farmer `.activity-list`/`.activity-row`) on top; **raw console output** (TDM `ConsoleOutput` + `_TKOutputHandler`) collapsed below for debugging. Duplicates Faraday's pattern and preserves TDM's log power-users.

### 2.8 Wireframe — Tray menu

```
 ┌──────────────┐
 │ TwitchDropsMiner  ● ▸        │ toolbar tooltip = live progress
 │   Eternights                │ (TDM `TrayIcon.get_title` + `update_title`)
 │   2h14m · 3/5 drops · 62%   │ tray tooltip
 ├─────────────────────────────┤
 │   Show                    ▸ │ (restore window)
 │   ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┤
 │   Quit                     │
 └─────────────────────────────┘
```

Mirrors TDM `TrayIcon` (`pystray.Menu`: Show→`restore`, Quit→`quit`) but enriches the tooltip with live drop progress (already implemented via `get_title`) and adds the status color to the tray icon state. **Tray is the default resting surface** (§4.4).

---

## 3. Component inventory

Named components the new UI needs, with visual spec + behavior notes. All values reference the tokens in §1; component proportions follow Farmer classes.

| Component | Visual spec | Behavior / notes |
| --- | --- | --- |
| **Sidebar** | `224px`; `--surface` translucent (`rgba(12,17,26,.94)`); brand mark (rotated gradient tile) + wordmark; nav items 43px, `--radius-sm`; active = left 3px accent bar + gradient. Footer: safety dot + status. | 7 nav items + footer. Collapses to 72px icon rail at <850px (Farmer media query). `aria-current="page"` on active. |
| **TopBar** | Eyebrow (`--subtle`, uppercase, tracked) + `h1`. Right: **HealthDot** + **AccountButton**. | Health dot: green/amber/red with `aria-label` text; account button opens Account modal. |
| **HealthDot / StatusDot** | 6–8px circle, status color, glow on healthy; ALWAYS paired with text label (`aria-label` / adjacent label). | States: Operational/Live (green), Warning (amber), Error (red), Offline (subtle). Color never sole signal (Carbon status-pattern). |
| **StatusBadge** | Pill, `--radius-full`, 4–7px vertical padding, uppercase `--micro`; tinted bg per state (`green/rgba(85,214,168,.07)` etc). | States: LIVE / WATCHING / ENABLED / IDLE / WARNING / ERROR / NEW. Non-text 3:1 contrast. |
| **ChannelRow** | Grid `38px avatar ︱ copy ︱ state ︱ playback ︱ row-action ︱ remove`; `--surface` card; disabled opacity .56. | Toggle enable, remove (confirm), click→focus. Replaces TDM `ChannelList` Treeview. |
| **ChannelAvatar** | 38px rounded tile, accent-tinted bg, channel initial; optional 36px thumbnail for rewards. | |
| **CampaignCard** | Card grid (1–3 col); thumbnail, game, rewards, **DropProgressBar**, earn `x/y`, Claim button on complete. | Card-level Claim; disabled state when not eligible. Maps TDM `CampaignDisplay`. |
| **DropProgressBar** | `progress` element; 2px thin (`--mini-progress`) or 8px (hero); gradient `#7454de→#9b7cff` fill; `<progress aria-valuenow>` for a11y. | Determinate when a percentage is known; indeterminate spinner only when unknown (Carbon progress guidance). |
| **RewardRow / OpportunityRow** | Grid `36px thumb ︱ copy ︱ 110px type-pill ︱ 105px requirement`; type pills badge/emote/code. | Maps TDM per-drop inventory labels → structured reward rows with `aria-label`. |
| **Pill / TypePill** | 5–7px padding, `--radius-sm`, tinted bg + colored text (badge purple / emote green / code amber). | |
| **Button primaries** | Primary: gradient `#8664f7→#6846db`, 35px, `--shadow-accent`, white text ≥4.5:1. Secondary: accent-tinted border/bg. Ghost/text: transparent. Danger: red-tint. Disabled opacity `.45–.58`. | All focusable, focus-visible ring `--accent`. |
| **MonitorBar** | Toolbar card w/ status dot + policy label + Pause/Scan actions. | Pooled monitor controls (priority policy). |
| **ScanSummary / MetricCard** | 5-col stat grid (`--surface`, 14px pad, micro label + strong value). | Used for total/active/upcoming/expired/claimed counts. |
| **EmptyState** | Dashed-border card (`--radius-lg`), centered icon + title + one CTA; compact left-aligned for panels. | Distinguish: no data yet / no results / all clear / needs setup / error (Carbon empty-states pattern). Replaces TDM empty lists. |
| **Loading / Skeleton** | Neutral shimmer or indeterminate bar; never fake-empty. | Show while data is expected; switch to real empty only when known-none (Carbon progress guidance). |
| **AccountModal** | `min(480px,100%)`, `--radius-lg` 17px, `--shadow-modal`, backdrop blur; header + body + actions. States: connect / pending (device code w/ pulse) / connected. | Focus trap, ESC closes, initial focus on first field, `role="dialog"`. Maps TDM `LoginForm` + `ask_enter_code`. |
| **Modal (generic)** | Shared modal shell (backdrop + card). | Used for confirmations (remove channel), device code, risk disclosure. |
| **Toast / Notification** | Slide-in, top-right; status-tinted border; auto-dismiss 4–10s; pairs with OS tray notifications. | Non-blocking; honors `tray_notifications` setting. |
| **SettingsSection** | Grouped panel (`LabelFrame`-equivalent) with `h3` + control rows. | Grouped per §2.6. |
| **SelectCombobox** | Replaces TDM `SelectCombobox`; custom styled dropdown w/ `aria-expanded`. | Language, priority mode. |
| **Switch (Toggle)** | Custom switch replacing Ttk `Checkbutton` for on/off toggles. | `role="switch"`, visible focus. |
| **ActivityRow** | `34px icon ︱ copy ︱ time`; status icon matches severity. | Structured feed + raw log collapsible. |
| **StatsFooter / SidebarFooter** | safety dot + "Operational/Ready/Starting/Tracking @channel". | Single source of daemon-health truth. |

---

## 4. State & interaction design

### 4.1 Per-screen state model

Apply the dashboard state machine `Loading → Live/healthy → Stale/warning → Error → Empty/no-data` recommended by Carbon's dashboard guidance ([Carbon status-indicator pattern](https://carbondesignsystem.com/patterns/status-indicator-pattern/), [Carbon progress-bar usage](https://carbondesignsystem.com/components/progress-bar/usage/); the same model is articulated for real-time UIs in ["Dashboards are not reports"](https://brenthaskins.com/blog/dashboards-are-not-reports-real-time-ux)). Each state answers: *what is happening, how current is it, do I act, what do I do* ([Carbon](https://carbondesignsystem.com/patterns/status-indicator-pattern/)).

| Screen | Loading | Empty | Error | Live |
| --- | --- | --- | --- | --- |
| Dashboard | Skeleton hero + metrics | "No active campaign — [Scan for drops]" hero | "Backend unreachable · [Retry]" + offline dot | Hero progress + active-watch bar + metrics |
| Channels | Skeleton rows | "No channels — [Add channel] / [Import follows]" | "Couldn't load channels · [Retry]" | ChannelRow list + monitor bar running |
| Drops/Campaigns | skeleton cards | "No eligible campaigns — [Scan public]" | "Rate limit / scan failed · [Retry in 60s]" | cards with live progress |
| Inventory | skeleton rows | "Nothing to show — [Sync inventory]" | "Sync failed · [Retry]" | reward rows |
| Activity | "Loading log…" | "No activity yet" | "Log unavailable" | live feed + expanded log |
| Settings | — | — | "Couldn't save setting" (inline per control) | grouped panels; save on change w/ toast |

**Live staleness:** every live region shows a `Last updated <ts>` and distinguishes "watching"/"processing"/"stale"/"error" ([Carbon](https://carbondesignsystem.com/patterns/status-indicator-pattern/)).

### 4.2 Live-update behavior (websocket / poll → UI)

TDM receives live events via websocket (`websocket.py`) and periodic polls. Map events → UI updates:

| Event | UI reaction | a11y note |
| --- | --- | --- |
| Channel watching state change | ChannelRow playback pill + Dashboard hero update | `aria-live="polite"` on status region |
| Drop progress tick | ProgressBar value + tray tooltip update (`update_title`) | Update `<progress aria-valuenow>` |
| Drop completed / claimable | Toast "Drop ready to claim" + Claim button enable + tray notify | Screen-reader announcement |
| Rate-limit / websocket disconnect | HealthDot amber/red, Warning badge, retry affordance | `aria-live="assertive"` only for blocking errors |
| Maintenance reload | Maintenance/offline dot; don't freeze UI (per 3aec843 fix note in git log) | Show "Maintenance…" stale state |
| Campaign list refresh | Recompute cards; keep layout stable (avoid scroll jump) | — |

Guidance: batch frequent updates (throttle progress ticks to ~1s to avoid layout churn and screen-reader noise); status changes communicated **programmatically**, not via color/animation alone ([Carbon loading a11y](https://preview.carbondesignsystem.com/building-blocks/core/components/loading/accessibility)).

### 4.3 Confirmation flows

- **Remove channel / remove priority game / sign out / quit:** confirm with a `Modal` (generic). Destructive actions use a `--error`-tinted danger button; never a bare icon-only remove without accessible label.
- **Claim reward:** immediate action (no confirm) but with an undo/result toast.
- **Risk consent (session upgrade):** modal with explicit consent checkbox (maps TDM login risk flow).
- **Proxy / advanced settings:** validate on blur (TDM `proxy_validate`) with inline error + `aria-describedby`.

### 4.4 The "AFK 24/7" mindset — minimize interruption, tray-first

TDM is a **long-running background daemon that should not demand attention**:

1. **Default resting surface = system tray.** On launch with `minimize to tray` enabled (TDM `TrayIcon` + `_root.withdraw()`), the app starts in the tray and never flashes a window for routine progress. Restore via tray "Show" (mirrors current behavior).
2. **Tray tooltip = live progress** (TDM `get_title`/`update_title` already renders game, watched time, percent, `claimed/total`): glanceable without opening the window.
3. **Non-blocking toasts**, never modal dialogs, for routine news (drop claimed, playback confirmed). Modal dialogs reserved for genuinely blocking decisions (auth, destructive).
4. **Adaptive tray icon color** (idle/error/maint/watching) so even the tray glyph communicates state at a glance.
5. **No auto-focus stealing**: when the window is open, live updates must not yank focus/scroll. Dashboard maintains stable layout (fixed-height rows) to prevent jump-scare scroll.
6. **Graceful close:** Windows `WM_CLOSE`/shutdown → hide to tray (current `wnd_proc` handling preserved); "Quit" from tray is the only true exit. Preserve the `ShutdownBlockReason` for clean exit during system shutdown.

This "window is a passive status surface, tray is the cockpit" model aligns with daemon-app guidance to distinguish staleness and keep the UI from collapsing uncertainty into false certainty ([Design a Health Dashboard That Exposes Uncertainty](https://dev.to/babycat/design-a-health-dashboard-that-exposes-uncertainty-and-connection-errors-1412)).

---

## 5. Accessibility & platform checklist

### 5.1 Keyboard navigation

- **Tab order** must reach every control in a logical order; no keyboard traps; standard activation keys ([Windows keyboard interactions](https://learn.microsoft.com/en-us/windows/apps/develop/input/keyboard-interactions), accessed 2026).
- **Global shortcuts:** `Ctrl+1..7` switch pages; `Ctrl+F` focus search; `Esc` close modals / clear selection (TDM already binds `Esc` → `unfocus`); `Space`/`Enter` activate.
- **Focus ring:** always-visible 2px `--accent` outline + 2px offset on `:focus-visible` (from Farmer `style.css`). TDM currently strips the Tk focus dotted line; the rewrite must **restore** a visible ring. Recommended ring ≤3:1 non-text contrast vs adjacent ([WCAG 2.2 SC 1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html)).

### 5.2 Focus & modal handling

- Modals: `role="dialog"` + `aria-labelledby`, **focus trap**, return focus to trigger on close, ESC closes, initial focus on first field.
- Status/aria-live regions as in §4.2. Heading/region landmarks for screen-reader navigation ([Windows a11y overview](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessibility-overview)).

### 5.3 Contrast compliance (WCAG 2.2)

- **4.5:1** normal text, **3:1** large text (≥18.5px bold / 24px) ([WCAG 2.2 SC 1.4.3](https://www.w3.org/TR/WCAG22/)).
- **3:1** non-text (focus rings, status dots, chart lines, input borders) ([SC 1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html)).
- **Never communicate state by color alone** — always pair with shape/icon/text ([Material color guidance](https://m1.material.io/style/color.html), [Carbon status pattern](https://carbondesignsystem.com/patterns/status-indicator-pattern/)).
- Avoid very thin lines (anti-aliasing weakens perceived contrast) ([WCAG 1.4.11 understanding](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html)).
- **Verify at runtime**, not just on `--bg` — against each surface (`--surface`, badges, disabled states) ([WCAG 2.2 overview](https://www.w3.org/TR/WCAG22/Overview.html)).
- **APCA** as a secondary perceptual check (esp. small/dark-mode text), NOT a WCAG 2.2 conformance substitute — APCA is for the future WCAG 3.0 draft ([Myndex/SAPC-APCA](https://github.com/Myndex/SAPC-APCA), [WCAG 2.2](https://www.w3.org/TR/WCAG22/), accessed 2026).

### 5.4 Screen readers

- Prefer native toolkit controls (Wails/React will render real DOM: `<button>`, `<input>`, `<progress>`, `role="switch"`, `aria-*`). This is a **huge win over Tkinter**, where custom widgets lacked proper roles.
- Windows: expose correct role/name/value/state + change events via UI Automation ([Windows inclusive apps](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/developing-inclusive-windows-apps)); test with Narrator + [Accessibility Insights](https://accessibilityinsights.io/docs/windows/overview/).
- Linux: GTK/Chromium exposes AT-SPI; test with Orca + [GTK Inspector](https://docs.gtk.org/gtk4/section-accessibility.html); GNOME suggests testing high-contrast + large text ([GNOME HIG a11y](https://developer.gnome.org/hig/guidelines/accessibility.html)).
- Provide text equivalents for status dots (adjacent `aria-label`), progress bars (`aria-valuenow`), and reward-type pills.

### 5.5 High-DPI & scaling

- **No hard-coded px for layout**; use `rem`/system scaling. Test at **125% and 150%** (Windows) and fractional/mixed-DPI ([Windows High-DPI](https://learn.microsoft.com/en-us/windows/win32/hidpi/high-dpi-desktop-application-development-on-windows), [Accessibility best practices](https://learn.microsoft.com/en-us/windows/win32/winauto/accessibility-best-practices)).
- Vector assets (SVG icons, as Farmer's `Icon` uses inline SVG strokes) instead of raster where possible; provide hi-res variants for raster reward thumbs (Twitch images are already high-res).
- Layouts must survive text/spacing overrides without clipping (WCAG 2.2 text-spacing: line-height 1.5×, paragraph 2×, letter-space .12×, word-space .16×) ([WCAG text spacing understanding](https://www.w3.org/WAI/WCAG22/Understanding/text-spacing)).
- Respect OS dark/light preference and high-contrast/contrast-themes; test reduced-motion ([Apple HIG Dark Mode](https://developer.apple.com/design/human-interface-guidelines/dark-mode)).

### 5.6 Platform differences

| Platform | Notes |
| --- | --- |
| **Windows** | Primary target. UIA + Narrator + Accessibility Insights; DPI scaling; tray via pystray works; `WM_CLOSE`→tray preserved; inverse (high contrast) theme. |
| **Linux** | pystray/Wails tray support can vary by DE; verify tray availability per Wayland/X11 (repo already has a Wayland freeze fix in git history). Orca + AT-SPI; GTK Inspector. Tray "minimize" disabled on macOS path (`sys.platform != "darwin"` guard in TDM). |
| **macOS** | No "minimize to tray" (TDM disables it on darwin); use menu-bar extra; respect `prefers-reduced-motion` and system dark mode; SF font fallback. |

**Definition of done (a11y):** a user can install, launch, navigate, run every core workflow, recover from errors, and quit using **only keyboard + screen reader**, at 125%/150% scaling, in dark mode + high-contrast, on both Windows and Linux ([Windows a11y](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessibility-overview), [GNOME HIG](https://developer.gnome.org/hig/guidelines/accessibility.html)).

---

## References (URL + accessed 2026)

**Design systems & theming**

- Material Design 3 — Design tokens: <https://m3.material.io/foundations/design-tokens>
- Material Design 3 — Color roles: <https://m3.material.io/styles/color/roles>
- Material (m1) — Color & contrast: <https://m1.material.io/style/color.html>
- shadcn/ui — Theming (CSS-variable dark mode): <https://ui.shadcn.com/docs/theming>
- shadcn/ui — Dark mode: <https://ui.shadcn.com/docs/dark-mode>
- Radix UI — Colors usage (light/dark scales): <https://www.radix-ui.com/colors/docs/overview/usage>
- Radix Themes — Dark mode: <https://www.radix-ui.com/themes/docs/theme/dark-mode>
- Fluent 2 — Design tokens: <https://fluent2.microsoft.design/design-tokens>
- Fluent 2 — Color tokens (dark values + status): <https://fluent2.microsoft.design/color-tokens>
- Fluent 2 — Color: <https://fluent2.microsoft.design/color>
- USWDS — Design tokens: <https://designsystem.digital.gov/design-tokens/>
- CMS Design System — Spacing: <https://design.cms.gov/foundation/spacing/>
- W3C Design System — Typography: <https://design-system.w3.org/styles/typography.html>
- Apple HIG — Typography: <https://developer.apple.com/design/human-interface-guidelines/typography>
- Apple HIG — Dark Mode: <https://developer.apple.com/design/human-interface-guidelines/dark-mode>
- Mantlr — Dark Mode Design Guide (2026): <https://mantlr.com/blog/dark-mode-design-guide-color-typography-accessibility>

**Accessibility / contrast**

- WCAG 2.2: <https://www.w3.org/TR/WCAG22/>
- WCAG 2.2 — Non-text contrast SC 1.4.11: <https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html>
- WCAG 2.2 — Text spacing: <https://www.w3.org/WAI/WCAG22/Understanding/text-spacing>
- Myndex/SAPC-APCA (APCA): <https://github.com/Myndex/SAPC-APCA>
- web.dev — Color and contrast: <https://web.dev/articles/color-and-contrast-accessibility>
- Windows — Keyboard interactions: <https://learn.microsoft.com/en-us/windows/apps/develop/input/keyboard-interactions>
- Windows — Developing accessible/inclusive apps: <https://learn.microsoft.com/en-us/windows/apps/design/accessibility/developing-inclusive-windows-apps>
- Windows — Accessibility overview: <https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessibility-overview>
- Windows — High-DPI desktop development: <https://learn.microsoft.com/en-us/windows/win32/hidpi/high-dpi-desktop-application-development-on-windows>
- Win32 — Accessibility best practices: <https://learn.microsoft.com/en-us/windows/win32/winauto/accessibility-best-practices>
- Accessibility Insights: <https://accessibilityinsights.io/docs/windows/overview/>
- GNOME HIG — Accessibility: <https://developer.gnome.org/hig/guidelines/accessibility.html>
- GTK 4 — Accessibility: <https://docs.gtk.org/gtk4/section-accessibility.html>

**Dashboard / status / empty-state UX**

- Carbon — Status indicator pattern: <https://carbondesignsystem.com/patterns/status-indicator-pattern/>
- Carbon — Progress bar usage: <https://carbondesignsystem.com/components/progress-bar/usage/>
- Carbon — Empty states pattern: <https://carbondesignsystem.com/patterns/empty-states-pattern/>
- Carbon — Loading accessibility: <https://preview.carbondesignsystem.com/building-blocks/core/components/loading/accessibility>
- Fluent 2 — Badge usage: <https://fluent2.microsoft.design/components/web/react/core/badge/usage>
- NN/g — Empty states: <https://www.nngroup.com/articles/empty-state-interface-design/>
- "Dashboards are not reports" (real-time UX): <https://brenthaskins.com/blog/dashboards-are-not-reports-real-time-ux>
- "Design a Health Dashboard That Exposes Uncertainty": <https://dev.to/babycat/design-a-health-dashboard-that-exposes-uncertainty-and-connection-errors-1412>

**Twitch brand**

- Twitch Corporate Identity Manual: <https://fliphtml5.com/snxys/wvbo/Twitch_Corporate_Identity_Manual/>
- Twitch — Beyond Purple (design language): <https://blog.twitch.tv/en/2019/12/03/beyond-purple/>
- Twitch — Designing Extensions (dark theme colors): <https://dev.twitch.tv/docs/extensions/designing/>
- Twitch — Trademark guidelines: <https://legal.twitch.com/en/legal/trademark/>

**Primary source (extracted, no URL)**

- `Farmer/frontend/src/App.css` and `style.css` — verbatim token palette and component recipes (read from `/home/pyro1121/Documents/Farmer`).
- `TwitchDropsMiner/gui.py`, `main.py`, `settings.py` — current TDM screens, settings, and tray behavior.
