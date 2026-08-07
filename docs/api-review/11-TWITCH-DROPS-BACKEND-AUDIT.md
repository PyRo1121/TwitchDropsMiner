# Twitch Drops backend and two-target audit

**Checked:** 2026-08-07
**Scope:** viewer farming, campaign/inventory projection, channel eligibility, claims, authentication, and the official Twitch API boundary.

## Executive finding

There is no public Helix endpoint that starts viewer watch-time progress, returns a viewer's current watch session, or claims a viewer Drop. Twitch's official Drops APIs are primarily **developer entitlement APIs**. They require a client owned by an organization that owns the game and operate on entitlements after a viewer has earned/claimed a reward.

The miner therefore has two deliberately separate planes:

1. **Viewer farming plane (Twitch's web-client surface):** persisted GraphQL queries, the legacy PubSub user-drop topic, and the web player's Spade `minute-watched` event. These are the only surfaces currently exposing the viewer inventory/current-drop/progress/claim behavior needed by this application. They are not a stable public developer API; persisted-query hashes and payload contracts can change.
2. **Developer entitlement plane (official API):** Helix `GET/PATCH /helix/entitlements/drops` and EventSub `drop.entitlement.grant`. These are appropriate for a game developer's fulfillment service, not for a generic viewer miner. Calling them with the miner's Twitch client would be unauthorized or semantically wrong.

Replacing the first plane with Helix/EventSub would remove the data required to farm drops, rather than make the miner more compliant.

## Official API matrix

| Need | Official Twitch option | Why it is / is not used here |
|---|---|---|
| Discover live channels | Helix `GET /helix/streams`, `Get Games`, `Get Users` | Officially available for metadata. It does not expose whether a viewer can earn a particular campaign or a campaign allowlist. Internal GQL directory/stream queries remain the farming candidate source. |
| Check campaign/channel eligibility | No generic viewer Helix endpoint | Internal `AvailableDrops`, campaign `allow`, game, active window, and viewer eligibility are required. |
| Read viewer inventory/progress | No public viewer-inventory/progress endpoint | Internal `Inventory`, `Campaigns`, `CampaignDetails`, `CurrentDrop`, and PubSub user-drop events are required. |
| Send viewer watch time | No public Helix endpoint | The web player's Spade event is the current working viewer client behavior. Each target now gets its own broadcast/channel/game/user identity and a fresh timestamp. |
| Claim a viewer Drop | No public Helix claim endpoint | The web-client GraphQL claim mutation is retained. |
| Fulfill an in-game entitlement | Helix entitlements + EventSub `drop.entitlement.grant` | Developer-owned service responsibility after the viewer claim; out of scope for a generic miner. |
| Validate OAuth | `GET /oauth2/validate` | Used at startup and at most hourly, with client/user matching. |

References:

- [Twitch API reference](https://dev.twitch.tv/docs/api/reference/)
- [Drops overview](https://dev.twitch.tv/docs/drops/)
- [Drops technical guide](https://dev.twitch.tv/docs/drops/technical-guide/)
- [EventSub subscription types](https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/)
- [OAuth token validation](https://dev.twitch.tv/docs/authentication/validate-tokens/)

The official Drops technical guide describes hosted inventory polling at 5–15 minute intervals and the entitlement APIs as a developer/game integration. It does not define a viewer watch-progress or viewer-claim API.

## Two-target farming contract

A watch target is an explicit `(channel, drop_id)` assignment. The selector only assigns a second target when all of these are true:

- both channels are live and locally eligible;
- the channels play different game IDs;
- the assigned Drop IDs are different;
- the Drop is active, unclaimed, within its time window, and its campaign/allowlist/account rules pass;
- when the optional `AvailableDrops` check is enabled, its result is used; otherwise the directory's drops filter and campaign ACL are trusted because Twitch's channel-availability response can be incomplete.

Progress is accepted only when the PubSub `drop_id` belongs to an active assignment, or when exactly one active target can earn that Drop and can safely adopt the event. A `CurrentDrop` response can reconcile the assignment to Twitch's reported Drop ID; if it reports a completed Drop, the miner generates the documented composite claim ID and attempts the claim before refreshing inventory. The operation is account-scoped in practice, so cross-channel stale sessions are routed by Drop ID or ignored rather than treated as channel failures. Unknown, already-claimed, or unscoped secondary progress is treated as unsafe: the affected channel is cooled down, inventory resyncs are debounced, and the second slot is disabled for the current run rather than repeatedly cycling through channels. The miner never fabricates progress or silently applies it to the wrong Drop.

The Qt channel page marks both assignments. The hero remains a primary-target projection because Twitch's viewer PubSub progress event is user-scoped and does not carry a channel ID.

## Remaining proof that requires a real account

Twitch does not publish a contract guaranteeing that two synthetic Spade minute events for two different games are credited independently. The implementation can now prove locally that it sends two distinct, correctly identified events and that it does not mix progress, but only an authenticated soak can prove server-side behavior:

1. Start two live channels in different campaign games with different Drop IDs.
2. Capture the two Spade requests without recording tokens or cookies; verify `broadcast_id`, `channel_id`, `game_id`, `user_id`, and changing `client_time`.
3. Confirm PubSub progress or `CurrentDrop` advances each assigned Drop independently.
4. Claim one Drop and verify the assignment is replaced with the next eligible distinct Drop without resetting the other target.
5. Take one channel offline/change its game and verify the remaining target continues while a valid replacement is selected.
6. Reconcile the final inventory from Twitch after the soak; local UI progress is not proof of entitlement.

No Helix/EventSub migration can replace this test because those official APIs do not expose the viewer farming state.
