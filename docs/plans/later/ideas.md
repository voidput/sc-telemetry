---
title: Ideas (ice box)
tags: [docs, plans, later]
updated: 2026-11-21
---

# Ideas (ice box)

Potential future work only. These are not commitments. Move an item to `next/backlog.md` once it is confirmed.

## Rust port of the bridge (decided: Python now, Rust later)
- **When:** once the telemetry schema stabilises, e.g. after 5.0 reaches LIVE.
- **Plan:** most likely as a crate inside StarOrg Codex (see below), rather than a standalone `scgameapi-rs`, built with `tonic`/`prost` (gRPC) and `axum` (HTTP + SSE), shipped as a single binary for Windows and Linux through GitHub Releases.
- **Contract:** keep the bridge HTTP/SSE API in [api-spec.md §8](../../telemetry/api-spec.md) unchanged, so the dashboard and a planned OpenDeck plugin do not need changes.
- **Optional:** a native OpenDeck plugin (OpenDeck is itself Rust) that talks gRPC to the game directly and drops the bridge.

## StarOrg Codex integration (separate project)
Codex is the Rust workspace (crates + Tauri/Svelte app) for StarOrg Codex. It already has most of the building blocks, so it is the **natural home for the "Rust later" port**: a new `starorg-telemetry` crate instead of a standalone `scgameapi-rs`.

| Need | Already in Codex |
|---|---|
| Find installs and channels (LIVE/PTU/EPTU/HOTFIX) | install / channel discovery |
| Turn telemetry on: `server.game.telemetry.enabled`, `trigger.actions.enabled` | USER.cfg editing with backups |
| Env vars `GRPC_CLIENT_SERVER_ENABLED`/`PORT` on Linux | LUG Helper launch hooks |
| Session, shard and system from `Game.log` | an existing Game.log scanner |
| AI-agent access: telemetry and trigger tools | a local MCP server (off by default) |
| Sharing with the org | opt-in StarOrg Tools sync |
| UI | Tauri app: a "Telemetry" page reusing the dashboard design, with a toggle to enable it per channel |

**Sketch:**
- `crates/starorg-telemetry`: a `tonic`/`prost` client using the runtime-extracted descriptor (see Decisions; was: vendored or generated from the installed exe the same way `extract_protos.py` does).
- An `axum` bridge serving the same HTTP/SSE contract as [api-spec.md §8](../../telemetry/api-spec.md), so a planned OpenDeck plugin and the web dashboard keep working.
- `starorg-codex telemetry enable|disable|status|stream|trigger` on the CLI.
- MCP tools `telemetry_snapshot`, `telemetry_history`, `list_actions`, and `trigger_action`. `trigger_action` is opt-in, never offers dangerous actions, and is debounced.

**Constraints Codex already enforces that carry over:**
- Never write while the game or a launcher update is running. Telemetry enable/disable edits USER.cfg, so the same lock rule applies.
- No network access by default, which telemetry respects (loopback only).
- Privacy doc ([docs/privacy.md] in Codex) must list what telemetry reads and shares.

**Decisions (2026-11-21):**
- **Proto at runtime.** Codex extracts the schema from the user's installed `StarCitizen.exe` per channel, the same way `extract_protos.py` does. No CIG-derived `.proto` ships in the repo or the release. It is cached per build (keyed on `build_manifest.id` / FileVersion) and re-extracted after a patch. Messages are decoded dynamically, using prost-reflect or an equivalent, against that descriptor. A field the user's build lacks shows as absent, not as an error.
- **Codex hosts the bridge and the telemetry data, locally on the user's machine.** That covers the gRPC client, the HTTP/SSE bridge (same contract), recordings and history, all stored in Codex's app data dir. **This Python bridge becomes optional**: a dev and reference tool, not something users must run.
- **No server-side data yet.** Nothing goes to StarOrg Tools or any other remote service. All telemetry stays on the player's PC. The org-sharing idea below stays parked until that changes.

**Still open:**
- Exact decoding library and whether descriptor extraction moves into `starorg-gamedata`, which already reads game files.
- Retention and size limits for local recordings and history in the app data dir.

### Use real game files to build mappings and capabilities (2026-11-21)
Inside Codex, `starorg-gamedata` reads `Data.p4k` and DataCore straight from the user's install, so the hand-made data in this repo can be generated per build instead:

| Hand-made today (scgameapi) | Generated from game files in Codex |
|---|---|
| `scgameapi/data/action_state_map.json` (action → telemetry field, labels) | Action maps (`defaultProfile.xml` actionmaps): every `v_*` action with its category, activation mode (press/hold/toggle) and localized UI label. Pairs of on/off/toggle actions are grouped automatically. State fields are still matched to telemetry by rule, but the labels and groups come from the game. |
| Human labels derived from action names ("Ifcs Toggle Esp") | Localized action names from `global.ini`, which Codex already manages |
| Ship capabilities from `sc-gamedata` role and default loadout (mining `WeaponMining`, `TractorBeam`, QED, EMP; salvage and refuel by role only) | Full entity definitions, including salvage heads, refuel nozzles and scanner modules. Per-port loadout with item types, so capability can be exact per ship variant. Can be combined with the player's actual equipped items from the telemetry `weapons[]` names. |
| Annunciator / IFCS names | Game UI strings for the MFD labels |

The same rule as the schema applies: derive everything at runtime from the user's install, cache it per build, and ship nothing CIG-derived. Until then, this repo keeps the hand-made map, with interim capabilities read from an optional sc-gamedata export (`--gamedata` / `.tmp/gamedata/<channel>/`).

## StarOrg.tools shared org telemetry (WebSocket)
> **Parked (2026-11-21):** the owner does not want any player telemetry stored or relayed server-side yet. Revisit only after Codex hosts telemetry locally.

- **Idea:** org members opt in to stream sanitised stats and events to StarOrg, so everyone in an op can see each other: ship, status, shields and hull, shard, system, and events such as downed, death or QT.
- **Precedent:** the existing org-bot WebSocket / ws-ticket pattern (server-to-client session socket with origin check; OAuth + PKCE login for a local client, then a short-lived ticket for socket auth). The bridge could authenticate the same way.
- **Shape:**
  - The local bridge publishes to StarOrg through REST or a ws-ticket (the single write path).
  - StarOrg fans out per guild and op session.
  - The dashboard and StarOrg frontend gain an "Op board" view.
- **Privacy defaults:**
  - Never share server IP, session ids or aUEC balance.
  - Shard and system only when the user opts in.
  - Per-field toggles, and a rate cap of about 1–2 Hz upstream.
  - Sharing stops automatically when the game exits.
- **Open:** whether sharing is per op session or always-on while in the org; retention (live-only vs stored for after-action reports); and anti-spoof measures (a client can lie, so treat the data as informational).
