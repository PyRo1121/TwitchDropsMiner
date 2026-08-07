# STATUS — backend surface audit (agent 02)

Backend service surface mapped for the UI rewrite. Full report: `02-BACKEND-SURFACE.md`.

- Service map complete for all 11 modules with `file:line` refs; `Twitch` (`twitch.py:433`) is the single orchestrating service that owns `gui`, `websocket`, `channels`, and `inventory`.
- UI consumption index built both ways: ~20 backend→UI callouts (status, tray, channels, inv, websockets, login, progress) and a small UI→backend set (`change_state`, `request`, `get_auth`, `_client_type`, `close`).
- State model: channels/inventory/drops/wanted-games are transient in-memory; only `settings.json`, `cookies.jar`, and the image cache are durable.
- Health: 5 dead GQL queries, 3 unused WS topics, 2 dead channel methods, disabled chrome/captcha path, several swallowed/soft errors, hardcoded GQL hashes.
- Top rewrite risk: bidirectional `twitch ↔ gui` import cycle (`twitch.py:19/448`) plus ~20 direct synchronous UI callouts inside backend hot paths — extract a service behind an event bus.
- No source code modified; report + status only.
