STATUS — Agent 03 (farmer-reference)

Extracted the full Farmer UI reference spec into docs/ui-review/03-FARMER-UI-REFERENCE.md (8 sections: TL;DR, Architecture, Design tokens, Component inventory, Interaction patterns, Translation map, Consistency notes, Appendix token index).

Read Farmer fully: App.tsx (2,588L), App.css (2,147L) + style.css, all 4 components (Chrome, CampaignWorkspace, CampaignCatalog, AccountModal), hooks/useAutomation, auth.ts, operational.ts, wailsjs bindings, index.html/vite/package.json, app.go exported methods, main.go, README. Cross-referenced TDM's lang/*.json tabs for the translation map.
Key findings: plain-hooks/no-Redux state; Wails Go struct -> auto JS proxy; 12-variable dark-navy palette with single purple accent; tiny 7-12px type + gradient CTAs; controlled a11y modal with focus trap; reusable opportunity-row/catalog widgets; seatbelt stale-response guard. No Farmer screenshots exist; styling claims quoted from CSS line numbers.
No source code modified (review only). risks: Farmer Settings screen is a stub so TDM must keep its own richer settings; no real toast system in Farmer.
