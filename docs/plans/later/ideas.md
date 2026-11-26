---
title: Ideas (ice box)
tags: [docs, plans, later]
updated: 2026-11-25
---

# Ideas (ice box)

Potential future work only. These are not commitments. Move an item to `next/backlog.md` once it is confirmed.

## Rust port of the bridge
- **When:** once the telemetry schema stabilises, e.g. after 5.0 reaches LIVE.
- **Shape:** `tonic`/`prost` (gRPC) and `axum` (HTTP + SSE), shipped as a single binary for Windows and Linux.
- **Contract:** keep the bridge HTTP/SSE API in [api-spec.md §8](../../telemetry/api-spec.md) unchanged, so the dashboard and other clients do not need changes.
- **Schema:** same rule as today: extract the descriptor at runtime from the user's own `StarCitizen.exe`, cache it per build, decode dynamically (e.g. `prost-reflect`). Nothing CIG-derived ships. A field the user's build lacks shows as absent, not as an error.

## MCP / AI-agent integration
- A local MCP server (off by default) exposing `telemetry_snapshot`, `telemetry_history`, `list_actions` and `trigger_action`.
- `trigger_action` is opt-in, never offers destructive actions, and is debounced.

## Hardware button panels
- A plugin for a hardware macro-key panel that consumes the existing endpoints (`/events`, `/api/action-states`, `/api/shield-faces`) to show live state on keys and trigger allowlisted actions.

## Generate mappings from the user's game files
The hand-made data in `sctelemetry/data/` (action → telemetry field map, labels) and the interim ship capabilities from an optional game-data export could instead be derived per build from the user's own install:

- Action maps: every `v_*` action with category, activation mode (press/hold/toggle) and UI label; on/off/toggle pairs grouped automatically.
- Localized action names and MFD labels from the game's UI strings.
- Exact per-variant ship capabilities (mining, salvage, refuel, scanning) from entity definitions and default loadouts.

Same rule as the schema: derive at runtime, cache per build, ship nothing CIG-derived.

Status 2026-11-25: partly covered from outside this repo. sc-gamedata datasets fetched per installed build (opt-in `--gamedata-fetch`, [api-spec.md §8.5](../../telemetry/api-spec.md)) now supply action labels, actionmaps and default inputs (`/api/actions` `keybind`). They will also supply upstream per-vehicle `capabilities`. Still open: on/off/toggle grouping and the action → telemetry field map (`action_state_map.json`), which remain hand-made.

## Opt-in shared org telemetry
- **Idea:** org members opt in to share sanitised status (ship, shields/hull, system, events such as downed or QT) so an op can see each other.
- **Needs a backend** to fan out per org/op session, plus authentication. Out of scope while the bridge is local-only.
- **Privacy defaults:** never share server address, session ids or wallet balance; shard and system only on opt-in; per-field toggles; low upstream rate (about 1–2 Hz); stops when the game exits.
- **Open:** per-op vs always-on, retention, and anti-spoofing (a client can lie, so treat it as informational).
