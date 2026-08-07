# 10 — Twitch API Boundary and Research Record

**Date:** 2026-08-06
**Purpose:** prevent the UI rewrite from making unsupported assumptions about Twitch's API.

## Official research

The official Twitch documentation describes the supported third-party integration surface as the Twitch API/Helix, OAuth 2.0, and EventSub:

- [Twitch API overview](https://dev.twitch.tv/docs/api/) says the API provides the endpoints for Twitch integrations and directs developers to the API reference; it also recommends EventSub for resource updates.
- [OAuth token guide](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth) documents the device-code flow used by standalone/limited-input clients, including `/oauth2/device`, `/oauth2/token`, `authorization_pending`, and refresh-token behavior.
- [Token validation](https://dev.twitch.tv/docs/authentication/validate-tokens/) requires third-party applications maintaining an OAuth session to call `/oauth2/validate` at startup and hourly.
- [Drops technical guide](https://dev.twitch.tv/docs/drops/technical-guide/) describes official Drops delivery through Helix entitlement APIs and EventSub; it recommends EventSub + API or a documented polling fallback.
- [API reference](https://dev.twitch.tv/docs/api/reference) lists documented resources such as Games, Streams, Entitlements, and EventSub.
- [Developer Services Agreement](https://legal.twitch.com/en/legal/developer-agreement/) states that developers must use documented Program Materials unless Twitch has granted permission; it also imposes rate-limit and security obligations.

## What the local backend actually does

The existing miner is not a Helix-only client. It uses a mixture of documented OAuth endpoints and internal Twitch client behavior:

| Local code | Behavior | Status for this UI port |
| --- | --- | --- |
| `twitch.py:110-158` | `POST https://id.twitch.tv/oauth2/device` and poll `POST https://id.twitch.tv/oauth2/token` | Existing backend behavior preserved; the documented `scopes` field is explicit and rotated refresh tokens are stored separately with restrictive permissions. |
| `twitch.py:160-270` | `POST https://passport.twitch.tv/login` with an internal/mobile-style client identity | Existing backend behavior preserved; not reimplemented in Qt. |
| `twitch.py:331-430` | `GET https://id.twitch.tv/oauth2/validate` and cookie/session restoration | Existing backend behavior preserved; the backend now validates at startup and at least hourly, while Qt only displays status. |
| `twitch.py:1290+` | `POST https://gql.twitch.tv/gql` with persisted queries from `constants.py` | Internal/undocumented path; explicitly out of scope for a visual port. Hashes, headers, and variables remain unchanged; the shared transport now records rate-limit headers and retries documented HTTP 429 responses. |
| `websocket.py` | Twitch websocket transport and backend event fan-out | Existing backend behavior preserved; Qt consumes `websockets.update/remove`. |
| `inventory.py:24-35, 341-359` | Campaign/game/benefit image URLs supplied by Twitch data | Qt cache consumes these model-provided URLs through the existing `Twitch.request` abstraction; it does not invent API endpoints or attach new auth headers. |
| `inventory.py:160-214` | Existing drop claim GQL mutation and notifications | Existing backend behavior preserved; Qt renders claim/progress state only. |

## Porting rule

The Qt UI may depend on these local, already-established contracts:

- `Channel` and its `display()`/state properties;
- `DropsCampaign`, `TimedDrop`, `Benefit`, and their progress/claim properties;
- backend callbacks on `gui.status`, `gui.channels`, `gui.progress`, `gui.inv`, `gui.websockets`, `gui.login`, and `gui.tray`.

The Qt UI must **not**:

1. Change Twitch endpoint URLs, Client IDs, user-agent/device headers, OAuth scopes, GQL operation hashes, persisted-query variables, websocket topics, or claim semantics from the backend. Transport-level rate-limit handling belongs in the backend boundary, not in Qt.
2. Treat official Helix Drops entitlement APIs as a drop-in replacement for the local miner's GQL inventory model.
3. claim that the internal GQL path is an officially supported public API.
4. add independent polling, retries, or authentication outside the backend's `Twitch.request()`/`gql_request()` abstractions.

A future API migration would be a separate project requiring a compatibility matrix, registered Twitch application credentials/scopes, live account testing, and explicit review against Twitch's current documentation and Developer Agreement. The August 2026 OAuth guidance also requires the device-code token exchange to carry the requested `scopes` field; the backend sends an explicit empty scope list because the miner requests no documented user scopes. Refresh-token rotation is handled only when Twitch returns a refresh token, and an invalid refresh token falls back to the existing device-code flow.

## Implementation consequence

The current UI work is therefore a **presentation-layer replacement only**. Image loading, settings, tray actions, and pages must call existing local abstractions and domain models. The backend remains the single owner of Twitch network behavior.

The backend watcher may keep up to two concurrent watch targets. It selects only live, eligible channels whose games and target drop IDs are distinct; if no second distinct target exists, it safely continues with one. The Qt channel list marks both assignments while the hero remains the primary target's compact view.
