# BRIEF-08 — UX Design & Design Tokens

**Output file: `docs/ui-review/08-UX-DESIGN-TOKENS.md`** (overwrite).

## Mission

Define the **target UX and design system** for the rewritten TwitchDropsMiner. Base it primarily on **Farmer's extracted design language** (cross-check `03-FARMER-UI-REFERENCE.md` if it exists; otherwise extract directly from `Farmer/frontend/src/App.css` and `style.css`), and back it with **cited 2026 UX research** for desktop dark-theme dashboard/daemon apps.

## Research task (citation-based)

- Modern desktop dark-theme design best practices 2025/2026 (e.g. Material 3 dark theming, shadcn/ui/Radix dark mode, Microsoft Fluent 2, Apple HIG for dark mode) — color contrast (WCAG 2.2, APCA), typography scale, spacing.
- Dashboard/status-app UX: live-status indicators, progress visualization (drops), empty states, refresh/maintenance states, long-running background tasks.
- Twitch ecosystem visual language (Purple #9146FF, dark themes) — relevant because users are Twitch natives.
- Desktop app accessibility: keyboard navigation, screen readers, high-DPI.

## Deliverables

1. **Design tokens** — complete token set: color palette (backgrounds, surfaces, borders, text, accent, status colors — success/warning/error/idle/offline), typography (stack, sizes, weights, line-heights), spacing scale, radii, shadows, transitions, motion. Prefer concrete values with cited rationale; harmonize with Farmer's tokens (list both, then reconcile).
2. **Layout blueprint** — information architecture for TDM's screens (main dashboard: current channel, drop progress, stream status; channel list; priority/game list; inventory; settings; login; tray menu). ASCII wireframes for each screen.
3. **Component inventory** — named components the new UI needs (e.g. ChannelRow, DropProgressBar, StatusBadge, CampaignCard, SettingsSection, Modal, Toast) with visual spec and behavior notes.
4. **State & interaction design** — loading/empty/error states for each screen; live-update behavior (websocket events → UI); confirmation flows; the "AFK 24/7" mindset (minimize interruption, tray-first).
5. **Accessibility & platform checklist** — keyboard nav, focus rings, contrast compliance (cite WCAG), scaling on 125%/150% DPI, Windows/Linux/macOS differences.

## Rules

- **Every external claim MUST have a citation** (URL + accessed date, 2025/2026 preferred). Inline links `[title](url)`.
- Do NOT modify source. Report only.
- When done, write a 5–10 line status to `docs/ui-review/STATUS-08.md` and stop.
