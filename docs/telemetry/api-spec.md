---
title: Game Telemetry API specification (SC client, PTU 5.0)
tags: [docs, telemetry]
updated: 2026-11-21
---

# Game Telemetry API specification (SC client, PTU 5.0)

**Status:** unofficial, reverse-engineered, and verified live on 2026-11-21 against PTU `5.0.202.10025` (CL 13248297). CIG has published no documentation. Anything here may change in any patch.

**Companion docs:**
- [api-reference.md](api-reference.md): every message, field, enum and field number, generated from the runtime-extracted schema, plus observed value ranges.
- [game-telemetry-api.md](game-telemetry-api.md): discovery notes and test log.

---

## 1. Overview

| | |
|---|---|
| Protocol | gRPC over HTTP/2 |
| Transport security | **None** (plaintext). The log shows `Endpoint '127.0.0.1:50051' Secured: False`. |
| Authentication | **None** |
| Default bind | `127.0.0.1:<port>`, loopback only. `0.0.0.0` when `grpc-client-server-external=true`. |
| Port | Set with `GRPC_CLIENT_SERVER_PORT` (1024–65535). The default is unknown, so always set it. |
| Package / service | `sc.game.telemetry.v1.GameTelemetryService` |
| Schema | `sc/game/telemetry/v1/{api,types}.proto` (proto3). It is extracted at runtime from the `FileDescriptorProto`s embedded in the user's `StarCitizen.exe` and cached per build (`scgameapi/schema.py`). It is not committed. |
| Reflection | Not verified. Use the local `.proto` files. |
| Host process | `StarCitizen.exe`, which hosts the "local services server" (`CGameTelemetryServer`). Team `GameServices`, tag `[Haptic]`. |

### Access model: read + limited write

| Capability | RPC | Default | Gate |
|---|---|---|---|
| **Read** live game state | `TelemetryStream` | on once telemetry is enabled | `server.game.telemetry.enabled` |
| **Discover** triggerable actions | `ListTriggerableActions` | returns `{}` | `server.game.telemetry.trigger.actions.enabled` |
| **Write**: press or hold an input action | `TriggerGameAction` | refused | `server.game.telemetry.trigger.actions.enabled` |

Writing is restricted to **simulating digital input actions** (a key press, or a hold of `hold_ms`). The allowed actions come from a built-in allowlist, which can be read at runtime with `ListTriggerableActions`. The server enforces these limits:
- Only allowlisted action names are accepted. Anything else returns `UNKNOWN_ACTION`.
- Axis and analog actions are never allowed. A unit test in the exe asserts this, so there is no way to set throttle or stick positions.
- The action only fires if its action map is active (`MAP_INACTIVE` otherwise). For example, flight actions only work in the pilot seat.
- Repeats of the same action are debounced (`server.game.telemetry.trigger.debounce.ms`, returns `RATE_LIMITED`).
- `hold_ms` only works for actions with `holdable=true` (`HOLD_NOT_ALLOWED` otherwise).
- Actions run on the game's main thread.

There is **no way to write state directly**. You cannot set values such as power pips, shield levels or fuel. Every write goes through the same input actions the player could press.

---

## 2. Enabling

The settings come from the engine's "startup settings" system. Its sources are `CFG File`, `Env Var`, `Cmdline`, `Service` (remote config), `User CFG` and `CVAR`. The game logs every applied setting under `<Startup Settings> N settings set` in `Game.log`.

| Setting (dotted / kebab / ENV) | Verified source | Purpose |
|---|---|---|
| `grpc.client.server.enabled` / `--grpc-client-server-enabled` / `GRPC_CLIENT_SERVER_ENABLED` | **Env Var ✔** | Hosts the local gRPC server at launch |
| `grpc.client.server.port` / … / `GRPC_CLIENT_SERVER_PORT` | **Env Var ✔** | Bind port |
| `grpc.client.server.external` / … / `GRPC_CLIENT_SERVER_EXTERNAL` | – | Binds to `0.0.0.0` (LAN-exposed). Do not use. |
| `server.game.telemetry.enabled` | **User CFG ✔** (Env Var ✘, console ✘) | Turns on frame generation |
| `server.game.telemetry.stream.interval.ms` | untested | Interval between frames written to each client. `50` gives about 20 Hz; values below 1 are raised to 1. |
| `server.game.telemetry.trigger.actions.enabled` | untested (expected User CFG) | Turns on `TriggerGameAction` and `ListTriggerableActions` |
| `server.game.telemetry.trigger.debounce.ms` | untested | Minimum interval between repeats of the same action |
| `server.game.telemetry.test.data` | untested | Sends synthetic data instead of real game state |
| `server.game.telemetry.generate.without.clients` | untested | Generates frames even when no client is connected |

**Working setup, Windows:**
```powershell
setx GRPC_CLIENT_SERVER_ENABLED 1
setx GRPC_CLIENT_SERVER_PORT 50051
# then quit the RSI Launcher completely (tray icon too) and start it again
```
```ini
; C:\Program Files\Roberts Space Industries\StarCitizen\<CHANNEL>\USER.cfg
server.game.telemetry.enabled = 1
; server.game.telemetry.trigger.actions.enabled = 1   ; enables writes (see §1)
```

**Working setup, Linux (Wine / LUG / Lutris):**
```bash
# in the script / launcher settings that start the RSI Launcher under Wine (e.g. the LUG sc-launch.sh)
export GRPC_CLIENT_SERVER_ENABLED=1
export GRPC_CLIENT_SERVER_PORT=50051
```
```ini
; <WINEPREFIX>/drive_c/Program Files/Roberts Space Industries/StarCitizen/<CHANNEL>/USER.cfg
server.game.telemetry.enabled = 1
; server.game.telemetry.trigger.actions.enabled = 1   ; enables writes (see §1)
```

**Expected `Game.log` lines:**
```
[Env Var  ] GRPC_CLIENT_SERVER_ENABLED=true
[Env Var  ] GRPC_CLIENT_SERVER_PORT=50051
[User CFG ] server.game.telemetry.enabled=true
<Register Service> Service 'sc.game.telemetry.v1.GameTelemetryService' registered
<GRPC Server> ... [sc.game.telemetry.v1.GameTelemetryService]: 127.0.0.1:50051 [Insecure]
<GameTelemetry> Using the local services server started at launch
<GameTelemetry> Client connected (id=N total=N)
```
Failure hint: `gRPC telemetry service NOT running (set grpc-client-server-enabled true).`

**LIVE 4.10 differences:** LIVE has the stream only, with no trigger RPCs. It uses `grpc-client-server-address` instead of port/external, and it has no `server.game.telemetry.enabled` setting. This setup has not been tested on LIVE.

---

## 3. RPCs

### 3.1 `TelemetryStream` (server streaming)

```proto
rpc TelemetryStream(TelemetryStreamRequest) returns (stream TelemetryStreamResponse);
message TelemetryStreamRequest {}
```

**Behaviour (observed):**
- **Lifecycle:** the server accepts the stream as soon as it is listening, which happens before login. **No frames are sent until the local player exists**, which is after world load. Set a deadline of at least a few minutes, or reconnect in a loop.
- **Rate:** one frame every `stream.interval.ms`. The default measured 19.5 Hz, so about 50 ms.
- **Content:** each frame is a full snapshot, not a delta. Unchanged fields are sent again.
- **Optional sections:** `vehicle` and `weapons` are absent when you're not in a vehicle. `events` is present only on frames that carry events. `force_feedback` and `turret` were never seen.
- **Multiple clients** are supported, each with its own stream (`total=N` in the log). Disconnects are not logged.
- **Ending:** the stream ends when the game exits (`UNAVAILABLE`). Reconnect with backoff.

**Frame shape** (proto3 JSON, `preserving_proto_field_name`). Abridged real frame:
```json
{
  "actor": {
    "vitals": {"health": {"current": 100, "max": 100}, "oxygen": {"current": 1, "max": 1},
               "heart_rate": 60.0, "gravity": 1.0003, "body_temperature": {"current": 36.5, "max": 100}},
    "state": {"locomotion": "ACTOR_LOCOMOTION_STATE_SEATED", "crew_role": "ACTOR_CREW_ROLE_PILOT",
              "seat_name": "RSI_Perseus_Pilot_SeatDashboard_000000000002", "uec_balance": "123456"},
    "environment": {"external_temperature": 21.0, "breathable_atmosphere": true}
  },
  "vehicle": {"state": {
    "name": "RSI_Perseus_000000000001", "class_name": "RSI_Perseus", "type": "VEHICLE_TYPE_SPACESHIP",
    "health": 1.0,
    "shields": {"level": 0.83, "faces": [0.9, 0.8, 0.8, 0.8]},
    "master_mode": {"mode": "VEHICLE_MASTER_MODE_SCM", "spooled": true},
    "power": {"powerplants": true, "engines": true, "shield_emitters": true, "...": "9 bools"},
    "annunciators": {"pwr": "ANNUNCIATOR_STATE_NOMINAL", "prox": "ANNUNCIATOR_STATE_MINOR_ON", "...": "10 total"},
    "flight": {
      "state": {"is_landed": false, "gear": "VEHICLE_COMPONENT_DEPLOYMENT_STATE_RETRACTED"},
      "instruments": {"speed": 379.2, "heading": 9.1, "altitude": 3.4028235e38, "hydrogen_fuel": 0.99,
                      "g_meter": {"current": 1.4, "max": 12.08},
                      "boost": {"engaged": false, "output": 0, "reserves": 0.29, "critical": false}},
      "settings": {"ifcs_coupled": {"enabled": true, "active": true}, "...": "see reference"}
    }
  }},
  "weapons": {"weapons": [{"name": "GATS_BallisticGatling_S3_000000000003", "is_powered": true,
                           "ready_to_fire": true, "contained_in_preset": true}],
              "countermeasures": [{"type": "COUNTERMEASURE_TYPE_DECOY", "amount": 48},
                                  {"type": "COUNTERMEASURE_TYPE_NOISE", "amount": 20}]}
}
```

### 3.2 `ListTriggerableActions` (unary)

```proto
rpc ListTriggerableActions(ListTriggerableActionsRequest) returns (ListTriggerableActionsResponse);
message TriggerableAction { string action_name = 1; string category = 2; bool holdable = 3; }
```
Returns `{}` while trigger actions are disabled (the default), and also before the player has loaded into the world. With triggers enabled and the player in the world, PTU 5.0 returns **112 actions** in 11 categories: lights, player (mobiGlas, flashlight, starmap, visor wipe), 34 emotes, seat (eject, operator modes, remote turrets), doors and ports, landing gear, VTOL, IFCS toggles (G-safe, ESP, decoupling, proximity assist), master mode, power on/off per group, quantum engage, radar ping (the only holdable action), docking, ATC and hailing. Several are destructive (`v_self_destruct`, `v_eject`). The full list is in [triggerable-actions.md](triggerable-actions.md).

### 3.3 `TriggerGameAction` (unary)

```proto
rpc TriggerGameAction(TriggerGameActionRequest) returns (TriggerGameActionResponse);
message TriggerGameActionRequest  { string action_name = 1; optional uint32 hold_ms = 2; }
message TriggerGameActionResponse { TriggerGameActionStatus status = 1; string message = 2; }
```

| Status | Meaning |
|---|---|
| `OK` | Action injected |
| `UNKNOWN_ACTION` | Name not in the allowlist |
| `MAP_INACTIVE` | The action map it belongs to is not active in the current context |
| `RATE_LIMITED` | Within the debounce window of the previous trigger |
| `FAILED` | Injection failed |
| `HOLD_NOT_ALLOWED` | `hold_ms` given for an action that isn't holdable |
| `FILTERED` | Rejected by a filter (for example, trigger actions disabled) |

In-game console equivalent: `gt_trigger <action_id> [hold_ms]`.

**Verified 2026-11-21 (PTU 5.0, Perseus, pilot seat):** `v_lights` returned `OK`, and `vehicle.state.lights_enabled` flipped `true → false` in the stream within 1 s. An unknown name returned `UNKNOWN_ACTION`. Two `v_lights` calls about 1 s apart both returned `OK`, so the default debounce is under 1 s; the exact value has not been measured.

Application results come back in `status`. The gRPC status stays `OK` unless the transport fails.

---

## 4. Data semantics and gotchas

Units, scales and observed ranges for every field are in [api-reference.md](api-reference.md). The items most likely to trip up a client:

| Field | Gotcha |
|---|---|
| `altitude`, `radar_altitude` | `FLT_MAX` (3.4028235e38) when there is no reference body. Treat `abs(v) > 1e30` as absent. |
| `actor.vitals.oxygen` | Scale is 0..1. The other vitals use 0..100. |
| `body_temperature.max` | Always 100. Not a real limit, so use `current` in °C. |
| `g_meter.max` | Session **peak** G, which only goes up. Not a limit. |
| `uec_balance` | int64, so it is a **string** in JSON. |
| `name`, `seat_name`, weapon `name` | Include a trailing entity id (`_000000000001`). Strip it with `_\d{9,}$`. |
| `manufacturer`, `preset_name` | Empty strings in 5.0. |
| `shields.faces` | **Variable length = the ship's shield face layout**, not the generator count: 1 = bubble (MISC Prospector, which has 3 generators), 4 = quadrants (RSI Perseus, which has 2 generators). Front/back (2) and possibly 6-face ships exist. Face order is undocumented; a hit-direction test is pending. **Tuning is visible:** at full charge each face reads its tuning share ÷ 100% clamped to 1.0 (Paladin tuned Front 60 / Left 220 / Right 60 / Back 60 → `[0.6, 0.6, 1.0, 0.6]`), so on 4-face ships **index 2 = LEFT** (verified). A single boosted face's share = 4 − sum(others). A boosted face hides damage until it falls below 100%. See `scgameapi/data/shield_faces.json`. |
| Enums | Strings in JSON. `*_UNSPECIFIED` (0) means "not reported", and proto3 JSON leaves it out unless you print default values. |
| Optional sub-messages | `doors`, `annunciators`, `flight`, `power`, `shields`, `master_mode`, `g_meter`, `gear`/`vtol`/`transformation` are proto3 `optional`. Absent means the vehicle doesn't have it. |
| Power | On/off per subsystem only. **No pip or allocation data.** |
| Targets / contacts / scanning | **None.** The API has no locked or selected target, no radar contacts, no ping or scan results and no information about other entities. Related only: the `radr`/`prox`/`misl`/`hit` annunciator states, and the `v_invoke_ping` / `v_target_hail` actions, which act but return no data. `Game.log` has a little more; see [gamelog-signals.md](gamelog-signals.md). |
| Operator mode | No current-mode field. Guns mode can be inferred from vehicle guns `ready_to_fire`. `TriggerGameAction` returns `OK` even for modes the ship lacks. |
| Field-number gaps | `VehicleFrame` starts at 2, `ForceFeedbackFrame` at 2, `ActorVitals` skips 6, `TelemetryStreamResponse` starts at 3. Presumably fields removed before release. |
| Location / shard | **The gRPC API has no location, star system, shard, region or server data.** The only position-like hints are `altitude`/`radar_altitude` (absent off-planet), `atmospheric_density`, `breathable_atmosphere`, `external_temperature` and `heading`. The bridge reads shard, region and system from `Game.log` instead ([§8.3](#83-session-data-gamelog)). |

---

## 5. Client examples

**Python (this repo):**
```bash
uv run python scripts/telemetry_probe.py stream --n 3 --timeout 60
uv run python scripts/telemetry_probe.py actions
```

**grpcurl** (reflection unverified, so pass the protos):
```bash
grpcurl -plaintext -import-path proto -proto sc/game/telemetry/v1/api.proto \
  127.0.0.1:50051 sc.game.telemetry.v1.GameTelemetryService/TelemetryStream
grpcurl -plaintext -import-path proto -proto sc/game/telemetry/v1/api.proto \
  -d '{"action_name":"<from ListTriggerableActions>"}' \
  127.0.0.1:50051 sc.game.telemetry.v1.GameTelemetryService/TriggerGameAction
```

**Minimal Python async client:**
```python
import asyncio, grpc
from scgameapi import stubs
api, apig, _ = stubs.load()

async def main():
    async with grpc.aio.insecure_channel("127.0.0.1:50051") as ch:
        stub = apig.GameTelemetryServiceStub(ch)
        async for frame in stub.TelemetryStream(api.TelemetryStreamRequest()):
            v = frame.actor.vitals
            print(v.heart_rate, frame.vehicle.state.flight.instruments.speed)

asyncio.run(main())
```

---

## 6. Security

- With the default loopback bind, any local process can read your state and, if triggers are enabled, inject inputs. No authentication is involved.
- `grpc-client-server-external=true` exposes reading and writing to the whole LAN. The exe warns about this itself (`other computers on the network can read telemetry and trigger game actions`). Do not use it. Use an authenticated proxy if you need remote access.
- The stream includes your wallet balance (`uec_balance`).
- All of these are CIG's own settings, so no binaries are modified. The EAC implications are assumed benign but not confirmed.

---

## 7. Versioning

| Build | Stream | Triggers | Added fields |
|---|---|---|---|
| LIVE 4.10.0 (CL 13220821) | ✔ | ✘ | baseline |
| PTU 5.0.0 (CL 13248297) | ✔ | ✔ | `VehicleShields`, `VehicleState.health`, `VehicleMasterMode(State)`, `TriggerGameActionStatus`, trigger RPCs |

After each patch the schema is re-extracted automatically: each build gets its own cache key, `<Branch>-<RequestedP4ChangeNum>` from `build_manifest.id`. To see what changed, regenerate the reference:
```bash
uv run python scripts/gen_api_spec.py --recording .tmp/recordings/<latest>.jsonl
git diff docs/telemetry/api-reference.md   # schema changes
```
To compare two builds directly, run `diff -r .tmp/proto/<old-key> .tmp/proto/<new-key>`.

---

## 8. Bridge HTTP API (`scgameapi.server`)

The dashboard bridge (`uv run python -m scgameapi.server`) turns the gRPC API into plain HTTP on `127.0.0.1:8765` by default. Modes: `--grpc HOST:PORT` (default `127.0.0.1:50051`, the live game), `--mock` (synthetic frames), `--replay FILE.jsonl` (loops a `--record` capture). In `--mock` and `--replay` modes every trigger goes to a **fake backend**, so nothing reaches the game.

| Endpoint | Method | Returns |
|---|---|---|
| `/events` | GET | SSE stream. Each unnamed `data:` line is one snapshot (below), sent on every new frame (max 20 Hz) plus a 1 Hz heartbeat. Named `event: alert` lines carry alerts ([§8.6](#86-alerts)); `?client=ID` identifies a dashboard tab for audio routing |
| `/api/snapshot` | GET | `{status, source, hz, frames_total, age_s, stale, events[], frame, sample, session, signals, people, capabilities}` (`people` carries handles only for a same-origin request with Show names on, [§8.7](#87-people-and-org-lookup)) |
| `/api/session` | GET | `{session}`: the public Game.log session context ([§8.3](#83-session-data-gamelog)), `null` when disabled |
| `/api/signals` | GET | `{signals}`: zone, armistice and nearby ship classes from Game.log ([§8.4](#84-gamelog-signals-zone-nearby-ships)), `null` when disabled |
| `/api/session/private` | GET | `{session: {server_address, server_port, session_id, env_session}}`; same Host/Origin guard as `/api/trigger` (403 otherwise), `Cache-Control: no-store` |
| `/api/history` | GET | `{interval_s: 0.25, t: [...], series: {speed, g, heart_rate, shield, boost, vsi, hydrogen, hull}}`, a 180 s ring at 4 Hz |
| `/api/actions` | GET | the normalised trigger allowlist (below) |
| `/api/trigger` | POST | presses one game action (below) |
| `/api/action-states` | GET | the action → telemetry state map, verbatim from `scgameapi/data/action_state_map.json` ([§8.5](#85-action-state-map-ship-capabilities-shield-faces)); 500 `{status: "ERROR", message}` if the file is missing or invalid |
| `/api/shield-faces` | GET | shield face layout metadata by `faces[]` length, verbatim from `scgameapi/data/shield_faces.json` (§8.5); 500 when missing |
| `/api/ship` | GET | `{class, capabilities, gamedata}` for the current vehicle (§8.5) |
| `/api/alerts` | GET | `{recent[], rules[], audio, config}`: last 50 alerts, rules summary, audio routing state (§8.6) |
| `/api/alerts/rules` | GET / PUT | full rule list; PUT validates, applies and saves the diff to the user rules file (guarded) |
| `/api/alerts/test` | POST | `{id}` fires that rule's alert as `TEST:` through the active sink (guarded) |
| `/api/alerts/mute` | POST | `{minutes: 0..240}` silences alert audio; 0 unmutes (guarded) |
| `/api/alerts/audio` | PUT | `{mode: auto\|bridge\|browser\|off}` at runtime (guarded) |
| `/api/alerts/audio-client` | POST | `{client_id, enabled, speech?}` registration + heartbeat of a sound-enabled tab (guarded) |
| `/api/people` | GET | `{people, settings}`: players seen in Game.log; handles only while Show names is on, org info only while Look up orgs is on (guarded, `no-store`, [§8.7](#87-people-and-org-lookup)) |
| `/api/people/settings` | PUT | `{show_names?: bool, lookup_orgs?: bool}` at runtime, not persisted (guarded) |
| `/api/people/cache` | DELETE | forgets every cached RSI lookup, memory + `.tmp/rsi-cache.json` → `{status: "OK", removed}` (guarded) |
| `/api/ships` | GET | `?class=CLASS` → `{class, capabilities, gamedata}` (404 with `capabilities: null` when unknown); no query → `{classes: [...], gamedata}` |

**Snapshot semantics**
- `frame` is the last `TelemetryStreamResponse` as a dict with proto field names. It is kept for display even when it is old, so check `stale`.
- `stale` is `true` when no frame has arrived for more than 3 s, or when `status` is not `streaming` (the stream ended or errored). `age_s` is the number of seconds since the last frame.
- `sample` and the 4 Hz history record `null` for every series while the feed is stale. They never repeat a frozen frame.
- `events` holds the last 50 game events, as `{id, t, type}`, newest first. Bridge-side session events (`SESSION_*`, §8.3) share the list and add a `detail` string. `id` increases by one per event, so clients de-duplicate on it. The game repeats one event on about 3 consecutive frames, so an occurrence of a `type` counts as new only beyond what the previous frame already carried. Two real events of the same type on back-to-back frames therefore merge into one. Events of the same type on non-adjacent frames are all kept, however close together they are. Malformed `events` (not a list, or a `type` that is not a string) are counted as type `?`.
- If the source task (gRPC, mock or replay) crashes, the bridge logs the traceback to stderr and sets `status` to `error (<task>: <ExceptionType>)`. `stale` then becomes `true`. Replay skips records whose `t` is not a finite number.

### 8.1 `GET /api/actions`

```json
{"actions": [{"action_name": "v_lights", "category": "lights_controller", "holdable": false}, ...],
 "dangerous": ["v_eject", "v_emergency_exit", "v_power_set_off", "v_power_set_thrusters_off", "v_power_toggle",
               "v_power_toggle_thrusters", "v_self_destruct", "v_toggle_qdrive_engagement"],
 "source": "game",          // or "fake" in --mock / --replay
 "error": "UNAVAILABLE ..."}  // only when the game call failed
```
- In grpc mode the last **non-empty** list is cached for 30 s. While the list is empty (the game returns `[]` until the player has spawned, and also when triggers are disabled), every request queries the game again. On a transport error the bridge returns the stale cache, which may be empty, together with `error`.
- In fake mode the list comes from `.tmp/triggerable-actions.json` when that file exists. Otherwise a small built-in list is used.

### 8.2 `POST /api/trigger`

Request (`Content-Type: application/json` is required, and the body may be at most 1 KiB):
```json
{"action_name": "v_invoke_ping", "hold_ms": 1500, "confirm": false}
```
- `action_name`: required. The whole string must match `^[a-z0-9_]+$` and be 1 to 64 characters long. A trailing newline is rejected (the bridge uses `fullmatch`).
- `hold_ms`: optional integer from 1 to 10000, or `null`. Send it only for holdable actions (today that is only `v_invoke_ping`).
- `confirm`: must be `true` for any action in `dangerous`.

Response: `{"status": "<string>", "message": "<string>"}`. When the call reached the game or the fake backend, `status` is the proto enum name, for example `TRIGGER_GAME_ACTION_STATUS_OK`.

| HTTP | `status` | When |
|---|---|---|
| 200 | `TRIGGER_GAME_ACTION_STATUS_*` | the game (or fake) answered: `OK`, `UNKNOWN_ACTION`, `MAP_INACTIVE`, `RATE_LIMITED`, `FAILED`, `HOLD_NOT_ALLOWED`, `FILTERED` |
| 400 | `BAD_REQUEST` | invalid JSON, invalid `action_name` or invalid `hold_ms` |
| 403 | `FORBIDDEN` | `Origin` or `Host` is not this bridge's own origin |
| 409 | `CONFIRM_REQUIRED` | a dangerous action was sent without `"confirm": true` |
| 413 | `PAYLOAD_TOO_LARGE` | the body is larger than 1 KiB |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | `Content-Type` is not `application/json` |
| 429 | `RATE_LIMITED` (`message: "bridge debounce"` or `"bridge: previous call still pending"`) | the same action was sent again within 1 s of the previous reply, or while a call for it is still waiting on the game. The game is not called. |
| 502 | `BRIDGE_ERROR` | gRPC transport error or the 5 s timeout. The game may be down. |

**Safety rules enforced by the bridge**
1. **CSRF guard.** The `Origin` header must be absent, or it must be `http://127.0.0.1:<port>` or `http://localhost:<port>` (the port of this bridge). The `Host` header must name the same host and port. Combined with the JSON-only content type, this stops a web page on another site, or a DNS-rebinding page, from pressing game buttons through your browser. One side effect: when the bridge is bound to `0.0.0.0`, triggers from other LAN hosts are refused.
2. **Debounce and in-flight lock.** The bridge allows one pending call per action, across all clients, and refuses a repeat until 1 s after the previous reply. The game's own debounce is shorter than 1 s: two presses 1 s apart both fire, so a toggle sent twice cancels itself out. Presses more than 1 s apart are still forwarded.
3. **Confirmation for dangerous actions.** `v_self_destruct`, `v_eject`, `v_emergency_exit`, `v_power_set_off`, `v_power_toggle`, `v_power_set_thrusters_off`, `v_power_toggle_thrusters` and `v_toggle_qdrive_engagement` are refused with 409 unless the request carries `"confirm": true`. `v_power_toggle` is included because it powers the ship off when power is on, which is the same effect as `v_power_set_off`. The two thruster-off actions are included because they remove flight control in flight.
4. **Audit log.** Every request, rejections included, is logged to stderr as `[trigger] HH:MM:SS <action> [hold=Nms] -> <status> (<http>) <message>`.
5. **Fake backend in mock/replay.** It answers `OK` for allowlisted names, `UNKNOWN_ACTION` for other names, `HOLD_NOT_ALLOWED` when `hold_ms` is sent for a non-holdable action, and `RATE_LIMITED` when the same action repeats within 1 s. The bridge debounce normally catches such a repeat first. In `--mock`, `v_lights`, `v_lights_on` and `v_lights_off` change the mock's `lights_enabled`.

The dashboard's **Controls** panel adds its own client-side guards on top of these:
- a 600 ms guard against repeat clicks, and a lock while a request is in flight;
- key auto-repeat never activates a button, so holding Enter sends one press;
- two-step arming for dangerous actions. The confirming press must be a separate key press or click, made within 3 s. An expired arm re-arms and never sends. Esc, blur, switching tabs and window blur all disarm;
- press-and-hold for holdable actions. `hold_ms` is the hold time, clamped to 100 to 5000 ms, and the hold is sent automatically at 5 s. Pressing Esc, or releasing the pointer outside the button, cancels the hold;
- results and the armed state are announced through a polite live region for screen readers.

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"action_name":"v_lights"}' http://127.0.0.1:8765/api/trigger
```

### 8.3 Session data (Game.log)

The gRPC API carries no location or server data (§4), so the bridge follows the game's own `Game.log` (`scgameapi/gamelog.py`). The log is opened **read-only**; nothing is ever written, truncated or rotated.

| Flag | Effect |
|---|---|
| `--game-log PATH` | follow this file |
| `--channel PTU\|LIVE\|HOTFIX\|EPTU\|TECH-PREVIEW` | default path `<--sc-dir>/<CHANNEL>/Game.log`; default `$SCGAMEAPI_CHANNEL`, else the channel whose Game.log was written last, else `LIVE`. The schema uses the same channel. |
| `--sc-dir DIR` | install dir (holds `LIVE/`, `PTU/` ...); default `$SC_DIR`, else auto-detected per OS (`scgameapi/paths.py`, order in the README). The schema uses the same dir. |
| `--no-game-log` | disable; `session` is `null` |

Per mode: grpc uses `--game-log` or the channel default; `--mock` always serves a synthetic session (`mock_use1b_0_001`, `Stanton`, TEST-NET address); `--replay` reads a Game.log only when `--game-log` is given, else `session` is `null`.

**Tailer.** Polls every 0.5 s. On start it reads the whole current file to build the state, then follows appended lines. A partial last line (the game mid-write) stays buffered until its newline arrives. The game writes a new Game.log on each launch (the old one goes to `logbackups/`); an inode change (Linux/macOS only; Windows inode numbers are not relied on), a size shrink, a changed file head or a `Log started` line resets the session and re-reads from the start. A missing file is retried each poll (`log_found: false`). The file is opened and closed on every poll (never a persistent handle), so on Windows the game can still rename it on relaunch; there one poll reads at most 4 MB.

**Session object**

| Field | Source line (PTU 5.0) |
|---|---|
| `channel` | `Executable: ...\<CHANNEL>\Bin64\StarCitizen.exe`, else `--channel` |
| `build` | `FileVersion: 5.0.202.10025` |
| `session_id`, `env_session` | `[Trace] @session: '...'`, `[Trace] @env_session: '...'` |
| `shard`, `server_address`, `server_port`, `location_id`, `joined_at` | `[Notice] <Join PU> address[..] port[..] shard[..] locationId[..]` |
| `shard` (update) | `[Notice] <Update Shard Id> New Shard Id: <shard>.` |
| `region_code`, `region_name`, `shard_short` | parsed from the shard name `<env>_<region>_<build>_<n>`: `ptu_use1b_13248297_020` -> `use1b`, `US East`, `USE1B · 020`. Known prefixes: `use1` US East, `usw2` US West, `euw1` EU West, `euc1` EU Central, `apse2` Asia-Pacific SE (Sydney), `apne1` Asia-Pacific NE (Tokyo); anything else is shown raw. |
| `system` | most recent `Loading screen for <X> : ... closed after N seconds`; `Frontend_*` maps to `Main menu` |
| `log_started_at` | timestamp of `Log started on ...` |
| `source`, `log_found` | `game_log` or `mock`; whether the file currently exists |
| `has_server_address`, `has_session_ids` | whether the private fields are set (served instead of the values) |
| `log_age_s` | seconds since Game.log was last written (file mtime); `null` for mock / log not found |

Timestamps (`joined_at`, `log_started_at`) are the log's own ISO-8601 UTC strings.

**Leaving.** A `Loading screen for Frontend_*` (Main menu) after a join ends the shard session: `shard`, `region_*`, `shard_short`, `server_*`, `location_id` and `joined_at` are cleared (build, channel and session ids stay until the next launch). The dashboard treats the session as **idle** when telemetry is stale and `log_age_s > 120` (game closed, or bridge started after the game): badge `IDLE`, panel and header dimmed, the "In session" timer frozen at the last log write.

**Events.** Session changes are appended to the snapshot `events` list as `{id, t, type, detail}` with `type` one of `SESSION_JOIN` (every `<Join PU>`), `SESSION_SHARD_CHANGE` (an `Update Shard Id` naming a different shard than the current one) and `SESSION_SYSTEM` (the system changed) and `SESSION_LEAVE` (back to the main menu after a join; detail = the shard left). `t` is the log line's timestamp, so events from the initial full read carry their original time. `detail` is the shard plus region name, or the system name; it never carries the server address or session ids.

**Privacy.** `server_address`, `server_port`, `session_id` and `env_session` never appear in `/events`, `/api/snapshot` or `/api/session`; only `GET /api/session/private` returns them, behind the `/api/trigger` Host/Origin check (blocks DNS rebinding and other hosts; with `--host 0.0.0.0` only `localhost`/`127.0.0.1` pass). The dashboard fetches it only while a Reveal button is on. `--record` writes `{"t", "session"}` records with those four fields stripped (`gamelog.public()`), skipping identical consecutive sessions and the pre-first-poll placeholder; replay skips these records.

**Robustness.** The tailer's poll (open/fstat/read/parse) runs in a worker thread (`asyncio.to_thread`); `on_change` is dispatched back onto the event loop. Rotation uses `fstat` of the opened handle. A failing poll or consumer callback is logged and tailing continues; a Game.log failure never changes the telemetry `status`. An unknown `$SCGAMEAPI_CHANNEL` is rejected at startup.

### 8.4 Game.log signals (zone, nearby ships)

Parsed from unofficial debug lines in the same Game.log (formats and caveats: [gamelog-signals.md](gamelog-signals.md)). Available whenever the session tailer runs; `null` with `--no-game-log` or `--replay` without `--game-log`. `--mock` serves synthetic signals (armistice, a few ships, own ship `AEGS_Gladius`).

**`snapshot.signals` / `GET /api/signals`**
```json
{"zone": {"kind": "armistice", "text": "Entering Armistice Zone - Combat Prohibited", "since": 1795300561.3},
 "armistice": true,
 "jurisdiction": "People's Alliance",
 "nearby": [{"class": "DRAK_Corsair", "display": "DRAK Corsair", "count": 2,
             "first_seen": 1795299300.1, "last_seen": 1795299602.4}],
 "nearby_count": 25, "nearby_classes": 19,
 "own_ship": {"class": "RSI_Perseus", "display": "RSI Perseus", "source": "telemetry"}}
```
- `zone.kind`: `armistice`, `monitored`, `ungoverned`, `jurisdiction` or `other` (e.g. after "Leaving Armistice Zone"). `since` and the `*_seen` times are epoch seconds from the log line timestamps.
- `armistice` is true from "Entering Armistice Zone" until "Leaving Armistice Zone". `jurisdiction` is the last "Entered <X> Jurisdiction" name.
- `nearby`: one entry per ship class, most recently seen first, at most 50. `count` is distinct entities of that class (ship instances, not people: a reclaimed or respawned ship can count twice, parked and AI ships count too); `nearby_count` is all distinct entities. Your own ship(s) are excluded.
- `nearby[].info` and `own_ship.info` (snapshot and `/api/signals` only, not `--record`): `{display_name, manufacturer, manufacturer_code, role, career, size, crew, cargo_scu}` from the local game dataset (`shipcaps`, no network); `null` for a class the dataset does not know. `size` is the dataset's vehicle size (1 smallest … 6 capital), `crew` its crew seats.
- `own_ship.source`: `telemetry` (frame `vehicle.state.name`) or `game_log` (last `Vehicle Control Flow` seat release); `null` until known.
- All signal state resets on rotation, `Log started`, and `SESSION_JOIN` / `SESSION_SHARD_CHANGE` / `SESSION_LEAVE`.

**Events** (in the snapshot `events` list, `{id, t, type, detail, ...}`; `t` = log time):

| `type` | Extra fields | When |
|---|---|---|
| `NEARBY_SHIP` | `count`, `classes` (≤10), `class` (when `count` is 1) | first sighting of a ship entity; at most one event per second, a burst is one event with `detail` "N new ships: A, B, C…" |
| `QT_ARRIVED` | `class`, `self` | a ship (or you) finished quantum travel in your bubble |
| `ZONE` | `kind`, `text` | a zone HUD notice with a different text than the current one |
| `VEHICLE_RELEASED` | `class` | you gave up control of a ship |
| `CORPSE_ITEMS` | `count` | you died; number of recoverable items on the corpse |

**Privacy.** Entity ids are only used inside the bridge (de-duplication, own-ship exclusion). They never appear in `/events`, `/api/snapshot`, `/api/signals`, events or recordings; corpse item names are dropped too. `--record` writes `{"t", "signals"}` records (the public view above, identical consecutive views once); replay skips them. Note that the telemetry `frame` itself still carries entity-suffixed names (`vehicle.state.name`, `actor.state.seat_name`), unchanged by this feature.

**Dashboard.** Header zone badge: `ARMISTICE` (nominal), `MONITORED` (info), `UNGOVERNED` (warning) or the jurisdiction name (info); hover shows the notice text and time. The **Nearby** panel lists your ship (`YOU`) then classes with a game-data line (manufacturer · role · size · crew · cargo), `×count` (tooltip: distinct ship instances, not people) and time since last seen; empty state "No ships streamed in yet". Signal events appear in the Flight log with their own icons.

### 8.5 Action state map, ship capabilities, shield faces

Data for showing live state **on** action buttons (dashboard Controls panel; a planned OpenDeck plugin would consume the same endpoints). Rendering state never sends an action.

**`GET /api/action-states`** serves `scgameapi/data/action_state_map.json` unchanged (read once per bridge process, then cached). It maps each triggerable action to the telemetry field that shows its state: `groups[]` of `{id, path | path_by_master, alt_path?, kind, labels?, prefix?, on?, invert?, sub?, absent_label?, actions: {name: toggle|on|off|cycle|show|{active_when}}, verified, known_issue?, note?}`, plus `stateless[]` (with `emote_*` globs) and `semantics`. Full semantics and rendering rules: [action-state-map.md](action-state-map.md).

**`snapshot.capabilities` / `GET /api/ship` / `GET /api/ships?class=X`** — what the current ship can do, from game data (not telemetry: the game answers `OK` to `v_set_mining_mode` / `_salvage_mode` / `_refuel_mode` even on ships without the hardware, and `weapons[]` lists only guns):
```json
{"class": "MISC_Prospector", "display_name": "MISC Prospector", "role": "Light Mining",
 "caps": {"mining": true, "salvage": false, "refuel": false, "tractor": false, "guns": true, "scan": true,
          "quantum": true, "qed": false, "emp": false, "missiles": false, "cargo": false,
          "cargo_scu": null},
 "source_build": "5.0.202.10025", "source": "ptu/game-ships.json"}
```
- `null` when there is no vehicle or the class is not in the dataset (no gating; the dashboard shows `?`). `/api/ship` and `/api/ships` add `gamedata: {loaded, source, source_build, ships, error}`.
- `source` (both places) is a short label `<parent dir>/<file>` (e.g. `ptu/game-ships.json`, `live/game-ships.json`), never the absolute path; `error` names files the same way. The full path appears only in the bridge's stderr log.
- Source: sc-gamedata `game-ships.json` (+ `game-salvage.json` beside it). Priority: `--gamedata PATH` / `$SCGAMEAPI_GAMEDATA` (file or directory) → `.tmp/gamedata/<channel>/game-ships.json`. The dataset is optional and comes from a separate sc-gamedata extractor (not part of this repo). The derived table is cached in `.tmp/ship-capabilities.json` keyed by source path/mtime/size/build, and reloaded when the source mtime changes (checked at most every 5 s). A missing or half-written dataset only disables gating.
- Key: `recordName` `EntityClassDefinition.<CLASS>`, where `<CLASS>` equals telemetry `vehicle.state.class_name`. Variants sharing a class are OR-merged.
- Derivation (`scgameapi/shipcaps.py`, default loadout): `mining` = any `WeaponMining` or role contains "Mining"; `salvage` = role contains "Salvage" or the salvage dataset lists the class (current datasets list none); `refuel` = role contains "Refuel"; `tractor` = `TractorBeam`; `guns` = `WeaponGun` or turrets; `scan` = radars; `quantum` = quantumDrives; `qed` = quantumEnforcement; `emp` = `EMP`; `missiles` = missileRacks; `cargo` (bool) = `totals.cargoScu` > 0 or cargoGrids exist; `cargo_scu` (number | null) = `totals.cargoScu` when present. `cargo: false` means "not in the dataset", not "carries none" (the dashboard shows `?`). Display only, never gates.
- Gating reach: only `mining`, `salvage` and `refuel` gate in practice. `guns` follows the rule above, so mining ships with turrets or default-loadout repeaters (e.g. Prospector: `WeaponMining` + 2× `WeaponGun`) report `guns: true` and `v_set_guns_mode` is effectively never gated; `scan` / `quantum` are true for nearly every ship. Tightening `guns` to `WeaponGun` only is a possible later change.

**`GET /api/shield-faces`** serves `scgameapi/data/shield_faces.json`: `{by_count: {"1": {layout: "bubble", labels: ["ALL"], ring: [0]}, "2": {layout: "front_back"}, "4": {layout: "quadrant"}, "6": {layout: "quadrant_top_bottom", ring: [0,1,2,3], pips: [4,5]}}, overrides_by_class: {}}`. `vehicle.state.shields.faces` has a variable length equal to the ship's shield face layout, not its generator count (verified: MISC_Prospector 1 = bubble, RSI_Perseus 4). Face order is unverified, so labels stay `F1..Fn`. Any other count renders as N equal ring segments. A face ≤ 0 while the overall level is > 0 is shown as `DOWN`.

**`--mock` fake triggers** flip mock state for `v_lights*`, the landing-gear trio and the shield-power trio, so on-button state can be exercised without the game. Hidden test switch `--fake-fail REGEX` (fake backend only) answers `FAILED` for matching action names, to exercise the dashboard's `UNSUPPORTED` path for `known_issue` groups; the dashboard's Refresh button clears this session's `UNSUPPORTED` verdicts.

### 8.6 Alerts

Rule engine, routing and TTS setup: [alerts.md](alerts.md). Every alert is `{id, rule, severity: info|warn|critical, text, speech|null, sound|null, t, value|null, test, boot}`. `id` restarts at 1 for every bridge process, and `boot` names the process, so `boot` + `id` is unique across restarts.

- **Hub event.** Each alert is also appended to `snapshot.events` as `{id, t, type: "ALERT", detail: text, rule, severity, test?}`. The event's `id` is the event counter, not the alert id.
- **SSE.** `event: alert` sends `data: {seq, kind: "toast", alert}` to every connection for every alert. Audio routed to a browser goes out as `{seq, kind: "play", alert, preempt, play}`, where `play` is `true` only on the newest open `/events?client=ID` connection of the chosen tab. A tab is chosen only while that stream is open. A new connection starts from live data; history is in `GET /api/alerts`.
- **`GET /api/alerts?n=50`** returns `boot`, `recent` (newest first), `rules` (`{id, kind, enabled, severity, scope, verified, description, message, speech}`), `audio` and `config: {user_file, error}`. `user_file` is relative to the repo; `error` is set when the user file was rejected and the defaults are in use. `audio` contains:
  - `mode`
  - `route: {to: browser|bridge|none, reason?}`
  - `browser_clients`
  - `bridge: {sink: bridge|dry-run, player, tts}`
  - `muted`, `muted_until`
  - `queue`
  - `last`
- **`PUT /api/alerts/rules`** takes `{rules: [...]}`, the full list as `GET /api/alerts/rules` returns it. Default ids left out of the list are dropped and saved as `removed`, so they stay dropped after a restart. A field set to `null` unsets it on a default rule. The user file's `settings` are kept.
  - `400 {status: "INVALID", message}` lists the validation errors, and nothing is written.
  - `200 {status: "OK", rules: N, user_file}` means the change is saved. The write is atomic, and the file stores only fields that differ from `scgameapi/data/alerts.default.json` plus new rules.
- **Guards.** All mutating alert endpoints use the `/api/trigger` Host/Origin guard (403). They require `Content-Type: application/json` (415) and a JSON object body (400). The body limit is 1 KiB, or 64 KiB for rules (413). They return 503 when alerts failed to start.
- **CLI / env.**
  - `--alerts PATH` / `$SCGAMEAPI_ALERTS`: user rules patch. Default `.tmp/alerts.json`.
  - `--alerts-audio MODE` / `$SCGAMEAPI_ALERTS_AUDIO`
  - `--alerts-dry-run` / `$SCGAMEAPI_ALERTS_DRY_RUN=1`: log what would play, start nothing.
  - `$SCGAMEAPI_PIPER_VOICE`: piper voice model.

### 8.7 People and org lookup

Players named in Game.log (crew, party, nearby) plus an opt-in RSI org lookup. Sources, privacy model and cache: [people-and-lookup.md](people-and-lookup.md). Both toggles are **off by default**: `--show-names` / `$SCGAMEAPI_SHOW_NAMES=1`, `--lookup-orgs` / `$SCGAMEAPI_LOOKUP_ORGS=1`, or `PUT /api/people/settings` at runtime.

**`snapshot.people` / `GET /api/people` → `people`** (`null` when Game.log is disabled)
```json
{"counts": {"crew": 1, "party": 2, "party_likely": 1, "nearby": 3, "total": 7, "self_known": true},
 "show_names": true, "lookup_orgs": true, "names_blocked": false,
 "self": {"name": "<you>", "label": "you", "valid_handle": true, "url": "https://robertsspaceindustries.com/citizens/<you>", "org": {...}},
 "people": [{"name": "<player>", "relation": "crew", "relations": ["crew", "party"], "label": "crew",
             "sources": ["voice_channel", "party_toast"], "first_seen": 1795300000.1, "last_seen": 1795300300.2,
             "count": 3, "ship": "Drake Cutlass Black", "left": false, "leader": true, "valid_handle": true,
             "url": "https://robertsspaceindustries.com/citizens/<player>",
             "org": {"status": "ok", "checked_at": 1795300310.0, "handle": "<player>", "moniker": "<name>",
                     "enlisted": "Jan 1, 2950", "valid": true, "affiliations": 2, "no_main_org": false,
                     "org": {"name": "<org>", "sid": "<SID>", "rank": "<rank>", "visibility": "public"}}}],
 "lookup": {"enabled": true, "cache_entries": 12, "requests": 30, "in_flight": false, "last_status": "ok",
            "blocked_for_s": 0, "cache_error": null, "min_interval_s": 3.0}}
```
- `people` and `self` are `null` unless Show names is on **and** the request passes the Origin/Host guard. A request from another origin gets counts only and `names_blocked: true`.
- `relation`: `crew` > `party` > `party_likely` > `nearby` (strongest of `relations`). `org.status`: `pending`, `fetching`, `ok`, `not_found` (no such citizen; cached 1 day), `error` (retried after 1 h). `org.org.visibility`: `public`, `redacted` or `hidden`; `org.org` is `null` with `no_main_org: true` when the citizen has no main org. `org` is `null` while Look up orgs is off.
- `PUT /api/people/settings` returns `{status, settings, people}`; 400 on unknown keys or non-bool values, 415 without `Content-Type: application/json`, 403 from another origin.
- Nothing here is written to `--record`, to events or to logs.

