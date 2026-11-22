---
title: Star Citizen local Game Telemetry API (gRPC)
tags: [docs, telemetry]
updated: 2026-11-21
---

# Star Citizen local Game Telemetry API (gRPC)

**Date:** 2026-11-21
**Builds examined:** PTU `sc-alpha-5.0.0` (P4 CL 13248297), LIVE `sc-alpha-4.10.0` (CL 13220821)
**Status:** undocumented by CIG; off by default; not yet tested live. Everything below comes from the client binary and `Game.log`, not from official docs.

## Summary

`StarCitizen.exe` contains a gRPC server, `sc.game.telemetry.v1.GameTelemetryService`, hosted on a "local services server" inside the client. It streams live HUD/player/ship state and, in PTU 5.0, accepts allowlisted input actions. The schema was rebuilt from the `FileDescriptorProto`s embedded in the exe and compiles cleanly with `protoc`:

- `sc/game/telemetry/v1/api.proto` and `types.proto`. Since 2026-11-21 they are extracted at runtime by [`scgameapi/schema.py`](../../scgameapi/schema.py) into `.tmp/proto/<branch>-<CL>/` and no longer committed. The tables in [api-reference.md](api-reference.md) show the full content.

After a patch, the next start re-extracts the schema automatically into a new per-build cache directory.

Internally it belongs to `Team_GameServices` with the `[Haptic]` tag. The C++ classes are `CGameTelemetryServer`, `CLocalPlayerTelemetryComponent` and `IGameTelemetryServerBackend`. It lives in the same subsystem as the D-BOX haptics output.

## Service

```proto
service GameTelemetryService {
  rpc TelemetryStream(TelemetryStreamRequest) returns (stream TelemetryStreamResponse);
  rpc TriggerGameAction(TriggerGameActionRequest) returns (TriggerGameActionResponse);        // PTU 5.0+
  rpc ListTriggerableActions(ListTriggerableActionsRequest) returns (ListTriggerableActionsResponse); // PTU 5.0+
}
```

| RPC | Request | Response | Notes |
|---|---|---|---|
| `TelemetryStream` | empty | server stream of `TelemetryStreamResponse` | Each message carries the `vehicle`, `weapons`, `events`, `actor`, `force_feedback` and `turret` frames. The rate comes from `stream-interval-ms` (50 ms is about 20 Hz). |
| `ListTriggerableActions` | empty | `repeated TriggerableAction {action_name, category, holdable}` | Returns the runtime allowlist. The action names themselves are not in the exe as a plain list. |
| `TriggerGameAction` | `action_name`, optional `hold_ms` | `status` enum + `message` | Status values: `OK`, `UNKNOWN_ACTION`, `MAP_INACTIVE` (that action map isn't active right now), `RATE_LIMITED`, `FAILED`, `HOLD_NOT_ALLOWED`, `FILTERED`. A unit test asserts that axis/analog actions are never allowlisted, so only digital actions can be triggered. |

## Data available (`TelemetryStreamResponse`)

### `actor` (`ActorFrame`): the player

| Field | Type | Meaning |
|---|---|---|
| `vitals.health` / `oxygen` / `hunger` / `thirst` / `body_temperature` | `Gauge{current,max}` | Player vitals |
| `vitals.heart_rate` | float | Heartbeat (BPM) |
| `vitals.gravity`, `vitals.pressure` | float | G-load and pressure felt by the body |
| `vitals.intoxication_level` | int32 | |
| `environment.external_temperature` | float | |
| `environment.ambient_radiation_rate` | float | Radiation |
| `environment.breathable_atmosphere` | bool | |
| `state.locomotion` | enum | `ON_FOOT` / `EVA` / `SEATED` |
| `state.crew_role` | enum | `PILOT` / `GUNNER` / `CREW` |
| `state.seat_name` | string | |
| `state.uec_balance` | int64 | aUEC wallet |

### `vehicle` (`VehicleFrame.state` → `VehicleState`): the current ship or vehicle

| Field | Type | Meaning |
|---|---|---|
| `name`, `class_name`, `manufacturer` | string | Ship identity |
| `type` | enum | `SPACESHIP` / `GROUND` / `GRAVLEV` / `BOAT` |
| `health` | float | Hull health (PTU 5.0+) |
| `lights_enabled` | bool | |
| `doors.open_state` / `doors.lock_state` | enum | Opened/closed and locked/unlocked |
| `shields.level`, `shields.faces[]` | float | Overall shield level plus each face (PTU 5.0+) |
| `master_mode.mode` / `.spooled` | enum/bool | `NAVIGATION` / `SCM` / `STEALTH` (PTU 5.0+) |
| `power.*` | bool ×9 | Power on/off for powerplants, engines, weapons, coolers, life_support, gravity_generators, sensors, shield_generators, shield_emitters |
| `annunciators.*` | enum ×10 | Warning lights: `pwr wpn thr shld cool qdrv radr prox hit misl`. States: `NOMINAL/OFF/MINOR_FLASHING/MINOR_ON/MAJOR_FLASHING/MAJOR_ON` |
| `flight.state` | | `is_landed`, plus gear/vtol/transformation as `RETRACTED/DEPLOYING/DEPLOYED/RETRACTING` |
| `flight.instruments` | float | `speed`, `hydrogen_fuel`, `quantum_fuel`, `g_meter{current,max}`, `altitude`, `radar_altitude`, `vertical_speed_indicator`, `atmospheric_density`, `heading`, `boost{engaged,output,reserves,critical}` |
| `flight.settings` | `Availability{enabled,active}` / bool | IFCS toggles: coupled, ESP, gravity compensation, G-safe, cruise, speed limiter, proximity assist (+landing), stability, core, wind compensation, command behaviour. Also advanced HUD (SCM/NAV/labels), course prediction, light amplification, spacebrake-enables-boost, G-safe-disabled-by-boost |

### `weapons` (`WeaponsFrame`)

`preset_index` and `preset_name` give the active weapon group. `weapons[]` holds `{name, is_powered, ready_to_fire, contained_in_preset}`. `countermeasures[]` holds `{type: DECOY|NOISE, amount}`. `settings` covers convergence fallback, magnified UI, precision lines, staggered fire, lag/single/fading pips and gimbals locked.

> The stream has no personal-weapon (FPS) magazine or ammo field. The "ammo counts" in the datamine appear to correspond only to countermeasure counts and weapon ready state.

### `events` (`EventsFrame`)

`events[].type` is one of `ACTOR_SPAWN`, `ACTOR_DEAD`, `ACTOR_ENTER_SEAT`, `ACTOR_LEAVE_SEAT`, `ACTOR_RESPAWN`, `ACTOR_DOWNED`.

### `force_feedback` (`ForceFeedbackFrame`)

`angular` and `linear` each contain `{x,y,z}`, and each axis is an `AxisFeedback{resistance, damping}`. There are also `gunfire` and `stick_shaker` floats. This is aimed at FFB sticks and motion rigs.

### `turret` (`TurretFrame`)

`light_amplification` (bool).

Gaps in the field numbers (for example `VehicleFrame` starting at 2, `ActorVitals` skipping 6, and the response starting at 3) suggest fields that were removed or reserved.

## Enabling it

### Switches (PTU 5.0)

Each setting has three spellings: a CLI flag (`--kebab-case`), an env var (`UPPER_SNAKE`) and a dotted key (`a.b.c`). The dotted keys look like CIG's services-config keys (`--services-config-enabled 1` is set by the launcher), which are probably delivered remotely and are not user-editable.

| CLI flag | Env var | Help text in the exe |
|---|---|---|
| `--grpc-client-server-enabled` | `GRPC_CLIENT_SERVER_ENABLED` | "Boolean telling whether grpc services are hosted" |
| `--grpc-client-server-port` | `GRPC_CLIENT_SERVER_PORT` | "Port the client-hosted gRPC server binds to, 1024 to 65535" (default not found) |
| `--grpc-client-server-external` | `GRPC_CLIENT_SERVER_EXTERNAL` | "Bind to 0.0.0.0 to accept connections from other computers, instead of 127.0.0.1 for this computer only" |
| `--grpc-client-server-channel-args` | `GRPC_CLIENT_SERVER_CHANNEL_ARGS` | gRPC channel args |
| `--server-game-telemetry-enabled` | `SERVER_GAME_TELEMETRY_ENABLED` | "Serve game telemetry on the local services server. Turning it on starts that server; turning it off stops it if telemetry started it." |
| `--server-game-telemetry-stream-interval-ms` | `SERVER_GAME_TELEMETRY_STREAM_INTERVAL_MS` | "Per-client stream write interval in milliseconds (50 = ~20Hz). Clamped to >= 1." |
| `--server-game-telemetry-test-data` | `SERVER_GAME_TELEMETRY_TEST_DATA` | "Force telemetry to use synthetic test data instead of real game state" |
| `--server-game-telemetry-generate-without-clients` | `SERVER_GAME_TELEMETRY_GENERATE_WITHOUT_CLIENTS` | "Generate telemetry frames even without connected clients" |
| `--server-game-telemetry-trigger-actions-enabled` | `SERVER_GAME_TELEMETRY_TRIGGER_ACTIONS_ENABLED` | "Allow external clients to trigger whitelisted game actions via the telemetry RPC" |
| `--server-game-telemetry-trigger-debounce-ms` | `SERVER_GAME_TELEMETRY_TRIGGER_DEBOUNCE_MS` | "Minimum interval (ms) between repeats of the same triggered action" |

`server-game-telemetry-enabled` alone should be enough, because it starts the local services server itself. Messages to look for in `Game.log`:
- `Local services server is running on <addr>` means it is up.
- `Local services server listens on <addr>: other computers on the network can read telemetry and trigger game actions` appears when bound externally.
- `gRPC telemetry service NOT running (set grpc-client-server-enabled true).` means it is not up.

### Where to set them

- **CLI flags: not practical.** The RSI Launcher builds the `StarCitizen.exe` command line itself (`-envtag`, `--grpc-client-endpoint-override`, and so on) and has no field for extra arguments. Launching `Bin64/StarCitizen.exe` directly would mean replicating the launcher's login handoff.
- **Env vars: the route to use.** On Linux, env vars exported in the Wine/LUG launch script already reach the game: the script's `DXVK_*` vars were confirmed in `/proc/<pid>/environ` of `StarCitizen.exe`. They pass from Wine to the RSI Launcher to `StarCitizen_Launcher.exe` to the game. On Windows, set them as user env vars with `setx` and restart the RSI Launcher (see the [README](../../README.md)). First attempt (historical, **did not work**; see Test 1):
  ```bash
  # historical first attempt: SERVER_GAME_TELEMETRY_ENABLED is ignored by the game
  export SERVER_GAME_TELEMETRY_ENABLED=1
  export GRPC_CLIENT_SERVER_PORT=50051
  ```
  Working pair (Test 2): `GRPC_CLIENT_SERVER_ENABLED=1` + `GRPC_CLIENT_SERVER_PORT=50051`, plus `server.game.telemetry.enabled = 1` in `USER.cfg` (Test 3). Leave `GRPC_CLIENT_SERVER_EXTERNAL` unset (loopback only).
  Applied 2026-11-21 in the test machine's launch script (a Wine/LUG prefix); the vars apply to every channel launched from that prefix.
  **Test 1 (2026-11-21, PTU 5.0.202.10025):** env vars do work. `Game.log` "Startup Settings" listed `[Env Var] GRPC_CLIENT_SERVER_PORT=50051`. However, `SERVER_GAME_TELEMETRY_ENABLED=1` was not listed and had no effect: the telemetry component initialized at world load, but nothing listened on 50051. Next step: also set `GRPC_CLIENT_SERVER_ENABLED=1` so the server starts at launch.
  **Test 2 (2026-11-21): working.** With `GRPC_CLIENT_SERVER_ENABLED=1` and `GRPC_CLIENT_SERVER_PORT=50051` set, the log shows `[Env Var] GRPC_CLIENT_SERVER_ENABLED=true` and then `Service 'sc.game.telemetry.v1.GameTelemetryService' registered` → `127.0.0.1:50051 [Insecure]` (plaintext, no TLS or auth). It binds at launch, before login. `ListTriggerableActions` returns `{}` while trigger actions are off. `TelemetryStream` sent no frames during the loading screen. Client: `uv run python scripts/telemetry_probe.py stream|actions`.
  **But no frames arrive.** This held on foot and seated in a ship, with streams of up to 60 s and several clients connected (`<GameTelemetry> Client connected (id=N total=N)`; disconnects are never logged). Every `SERVER_GAME_TELEMETRY_*` env var was silently dropped: they do not appear in "Startup Settings" or "Arguments Ignored", while the `GRPC_CLIENT_SERVER_*` ones were accepted. The `server.game.telemetry.*` names are not console cvars (no tab-complete). The startup parser's sources are `CFG File`, `Env Var`, `Cmdline`, `Service`, `User CFG` and `CVAR`, and settings can be flagged `DEV_ONLY`. Working theory: the frame generator is gated by `server.game.telemetry.enabled`, which is either `DEV_ONLY` in public builds or delivered through the remote `Service` config, meaning CIG has not turned it on. **Test 3:** added `server.game.telemetry.enabled = 1` to PTU `USER.cfg` (a backup was kept); not yet tested. LIVE 4.10 uses different switches; see below.
- **`USER.cfg`: only the `g_telemetry.*` cvars** listed below. The server switches are not cvars.

It is not yet confirmed whether a value of `1` or `true` is accepted for booleans. The launcher passes `1` for its own flags, for example `--services-config-enabled 1`.

### `g_telemetry.*` cvars (USER.cfg / console)

These cover audio-to-haptic output and debugging. They are present in LIVE and PTU. The pairing of help text to cvar is inferred from string order in the exe.

| Cvar | Purpose |
|---|---|
| `g_telemetry.audioHapticBandEdge0Hz` … `Edge8Hz` | Octave band edges in Hz, giving 8 bands |
| `g_telemetry.audioHapticDownmixMode` | 0=auto (infer layout from channel count), 1=equal weight, 2=channel 0 only |
| `g_telemetry.audioHapticRelease` | Per-band peak-hold decay coefficient [0,1); 0 = instantaneous |
| `g_telemetry.audioHapticDebugLog` | When non-zero, logs the 8 normalized band values each batch |
| `g_telemetry.audioHapticSelfTest [Hz]` | DSP self-test with a synthetic sine (default 60 Hz) |
| `g_telemetry.audioBufferInstanceID` | Audio buffer subscription |
| `g_telemetry.DebugExecuteImpact` / `DebugExecuteExplosion` / `DebugExecuteThrow` | Fire a test vibration or DBOX pulse |

The audio-haptic band data does not appear in the `TelemetryStream` schema, so it most likely goes only to the D-BOX path.

## LIVE 4.10 vs PTU 5.0

- **LIVE 4.10** has stream-only `TelemetryStream` with no triggers, no `shields`, no hull `health` and no `master_mode`. Its switches are `grpc-client-server-enabled`, `grpc-client-server-address` (in place of port/external), `grpc-client-server-channel-args`, `server-game-telemetry-{generate-without-clients,stream-interval-ms,test-data}`. There is no `server-game-telemetry-enabled` switch.
- **PTU 5.0** adds `TriggerGameAction`, `ListTriggerableActions`, `VehicleShields`, `VehicleState.health`, `VehicleMasterMode(State)`, the port/external split and the `enabled`, `trigger-*` switches.

## Security

- The server binds to loopback by default. `external` exposes reads and (if enabled) input triggering to the whole LAN. The exe itself warns about this. Keep it off.
- No authentication was seen for this server. `server-auth` and `grpc-server-authz` strings exist, but there is no evidence they apply here.
- These are CIG's own switches, so no binary is modified. EAC should be unaffected, but that is not confirmed.

## Unknowns / next checks

- Default port, and whether the env var route actually works. To find out, relaunch PTU with the env block above and grep `Game.log` for `Local services server`.
- Whether gRPC server reflection is on (`grpcurl -plaintext 127.0.0.1:50051 list`). Without reflection, pass `-import-path proto -proto sc/game/telemetry/v1/api.proto`.
- Whether the transport is plaintext or TLS.
- The contents of the trigger allowlist (`ListTriggerableActions`).
- Unexplained listener: TCP `0.0.0.0:31318` is owned by `StarCitizen.exe` in a session where telemetry is off. It accepts connections but stays silent for raw, HTTP/1.1, h2c, TLS and WebSocket probes. It is probably unrelated.

## Related

- Community D-BOX bridge (a different channel: `dbxLive64.dll` acting as a gRPC client to `127.0.0.1:61666`): https://github.com/m1ndgames/sc-api
- Spectrum: [telemetry endpoint thread](https://robertsspaceindustries.com/spectrum/community/SC/forum/3/thread/the-addition-of-telemetry-endpoint-is-a-great-step), [motion rig request](https://robertsspaceindustries.com/spectrum/community/SC/forum/50259/thread/request-broader-telemetry-access-for-motion-rigs-b)
