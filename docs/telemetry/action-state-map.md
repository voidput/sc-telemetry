---
title: Action state map (live state on action buttons)
tags: [docs, telemetry]
updated: 2026-11-21
---

# Action state map (live state on action buttons)

`scgameapi/data/action_state_map.json` is the single source of truth for which telemetry field shows the state of a triggerable action. The bridge serves it at `GET /api/action-states` ([api-spec.md §8.5](api-spec.md#85-action-state-map-ship-capabilities-shield-faces)). The dashboard Controls panel reads it at runtime. The OpenDeck plugin (on hold) is meant to load the same endpoint, with a bundled fallback copy.

Rendering state never sends an action.

## File format

```json
{"version": 1,
 "groups": [{"id": "lights", "path": "vehicle.state.lights_enabled", "kind": "bool", "labels": ["OFF", "ON"],
             "actions": {"v_lights": "toggle", "v_lights_on": "on", "v_lights_off": "off"}, "verified": true}, ...],
 "stateless": ["mobiglas", "v_eject", "emote_*", ...],
 "semantics": {...}}
```

Paths are dotted into the proto3-JSON frame, using snake_case field names. These group fields are recognised:

| Field | Meaning |
|---|---|
| `path` | Field that holds the state. `a.b[*].c` means "field `c` of every element of list `b`". It is only used with `any_bool`. |
| `path_by_master` | `{NAVIGATION: path, SCM: path}`. The path is picked by the current `vehicle.state.master_mode.mode` (used by the advanced HUD). |
| `alt_path` | Read when `path` is absent, for example `turret.light_amplification` when not in a pilot seat. |
| `kind` | `bool`, `enum` or `availability` (an `{enabled, active}` object). `annunciator`, `value` (a number), `any_bool` (true if ANY `[*]` element is true). |
| `labels` | `bool`, `availability` and `any_bool` take `[off, on]`. `enum` takes `{KEY: label}`. |
| `prefix` | Enum prefix that is stripped before the `labels` / `on` / `active_when` lookup. |
| `on` | The enum keys that count as "on", for example `DEPLOYED` and `DEPLOYING` for gear. |
| `invert` | Flips the meaning of on. `decoupled` reads `ifcs_coupled`, so "on" = decoupled. |
| `sub` | `{path, false_label}`. When that field is `false`, ` · <false_label>` is appended, for example `NAV · SPOOLING`. |
| `format` | Format of a `value` group, for example `peak {v:.1f} G`. |
| `absent_label` | Text shown when the field is missing. The default is `N/A`. |
| `verified` | `true` means the field was seen changing in real recordings. `false` means the link was inferred from the names. |
| `known_issue` / `note` | Free text shown in the tooltip. |

`availability`: `active` → `ON`, `enabled` without `active` → `ARMED`, otherwise `OFF`. The group counts as on when it is enabled or active.

## Action semantics → "lit"

A button is **lit** (nominal green accent) when its action matches the current state:

| Spec | Lit when | Text |
|---|---|---|
| `toggle` | state is on | the state label (for example `ON`, `DOWN`) |
| `on` | state is on | the state label |
| `off` | state is off | the state label |
| `cycle` | never | the current value (for example `NAV`) |
| `show` | never | a related value (for example `peak 9.0 G`, `NOM`) |
| `{active_when: X}` | the enum key equals `X`, or for `any_bool` the result equals `X` | the state label |

## Rendering rules (dashboard, and later the plugin)

- **Text is always present.** State is never shown by color alone.
- Lit uses the nominal green accent. Not lit is muted. Annunciator `MINOR` / `MAJOR` use the warning / critical ink, and `SPOOLING` uses warning.
- Unknown or absent field: `N/A` (or `absent_label`) in grey. No vehicle: `NO SHIP`. Bridge stale or offline: `STALE`.
- After a press, the feedback (`OK` / `RATE LIMITED` / an error) owns the button for 1.5 s, then the state shows again. The game applies most actions within about 1 s, so the state flip doubles as press confirmation.
- `verified: false` adds a small `?` marker, and the tooltip says "State mapping unverified".
- `known_issue` (doors, door locks) is shown in the tooltip. If a trigger for such an action returns `FAILED`, the feedback reads `UNSUPPORTED`, not an error. The whole group then stays `N/A` for that ship class until the page reloads or Refresh is pressed (Refresh clears these verdicts, since a FAILED can be transient, e.g. ship powered off).
- Touch has no tooltips, so the dashboard also shows the notes (known issue, unverified mapping, no-hardware warning) of the focused or last-tapped button in a line pinned to the bottom of the Controls panel, and links them to the button with `aria-describedby`.
- Actions in `stateless` (globs allowed) are unchanged and show no state line.

## Operator modes and ship capabilities

Telemetry cannot tell operator modes apart beyond "guns vs not guns" (`operator_guns`: any `weapons.weapons[*].ready_to_fire`). The game answers `OK` to `v_set_mining_mode` / `_salvage_mode` / `_refuel_mode` even on ships without that hardware (verified: Prospector salvage/refuel, Perseus mining). So the buttons are gated by **game data** (`snapshot.capabilities`, see api-spec §8.5):

| Action | Capability |
|---|---|
| `v_set_mining_mode` | `mining` |
| `v_set_salvage_mode` | `salvage` |
| `v_set_refuel_mode` | `refuel` |
| `v_set_scan_mode` | `scan` |
| `v_set_quantum_mode` | `quantum` |
| `v_set_guns_mode` | `guns` |
| `v_set_flight_mode` | never gated |

When the capability is `false`, the button shows `N/A · no <cap>`, muted. It can still be pressed, and the tooltip warns that game data may be stale or the loadout may be modified. When `capabilities` is `null` (unknown class or no dataset), nothing is gated and the tooltip says so.

## Shield faces

`vehicle.state.shields.faces` has a variable length that follows the ship's face layout. It is **not** the generator count: Prospector has 1 face (bubble) and 3 generators, Perseus has 4 faces and 2 generators. `scgameapi/data/shield_faces.json` (`GET /api/shield-faces`) describes the layout for each count:

- 1 face: a full ring, `BUBBLE`
- 2 faces: front/back halves
- 4 faces: quadrants
- 6 faces: quadrants plus top/bottom pips
- any other N: N equal segments

The face order is unverified, so labels stay `F1..Fn` until confirmed. A class can be pinned with `overrides_by_class`. A face at ≤ 0 while the overall level is > 0 shows `DOWN` (critical).

## Tests

- `tests/test_dashboard_js.py` extracts the dashboard's `<script id="state-logic">` block and runs it in node on synthetic frames against the real JSON files. It covers every kind, lit rules, `N/A` / `NO SHIP` / `STALE`, unsupported, capability gating, and face layouts for 1/2/4/6/5 faces.
- `tests/test_shipcaps.py` covers capability derivation from synthetic datasets, cache/mtime reload, and the endpoints.
