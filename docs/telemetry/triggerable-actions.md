---
title: Triggerable actions allowlist (PTU 5.0)
tags: [docs, telemetry]
updated: 2026-11-21
---

# Triggerable actions allowlist (PTU 5.0)

**Captured:** 2026-11-21 from `ListTriggerableActions` on PTU 5.0.202.10025 with `server.game.telemetry.trigger.actions.enabled = 1`. The list was empty until the player had loaded into the world.

**Total:** 112 actions. Only `v_invoke_ping` is holdable, so it is the only action that accepts `hold_ms`. Every action is a digital press, as with a key binding. None of them sets a value directly.

> ⚠ Some actions are **destructive**. A client should require a confirmation before sending any of them:

> - `v_self_destruct`: destroys the ship
> - `v_eject`: ejects the pilot
> - `v_emergency_exit`: leaves the seat in an emergency
> - `v_power_set_off`: powers down the whole ship
> - `v_power_toggle`: powers down the whole ship when power is on
> - `v_power_set_thrusters_off` / `v_power_toggle_thrusters`: cut thruster power, so there is no flight control in flight
> - `v_toggle_qdrive_engagement`: engages quantum travel

## lights_controller (3)

| Action | Holdable |
|---|---|
| `v_lights` |  |
| `v_lights_off` |  |
| `v_lights_on` |  |

## player (4)

| Action | Holdable |
|---|---|
| `mobiglas` |  |
| `toggle_flashlight` |  |
| `v_starmap` |  |
| `visor_wipe` |  |

## player_emotes (34)

| Action | Holdable |
|---|---|
| `emote_agree` |  |
| `emote_angry` |  |
| `emote_atease` |  |
| `emote_attention` |  |
| `emote_blah` |  |
| `emote_bored` |  |
| `emote_bow` |  |
| `emote_burp` |  |
| `emote_cheer` |  |
| `emote_chicken` |  |
| `emote_clap` |  |
| `emote_come` |  |
| `emote_cry` |  |
| `emote_dance` |  |
| `emote_disagree` |  |
| `emote_failure` |  |
| `emote_flex` |  |
| `emote_flirt` |  |
| `emote_gasp` |  |
| `emote_gloat` |  |
| `emote_greet` |  |
| `emote_laugh` |  |
| `emote_launch` |  |
| `emote_point` |  |
| `emote_rude` |  |
| `emote_salute` |  |
| `emote_sit` |  |
| `emote_sleep` |  |
| `emote_smell` |  |
| `emote_taunt` |  |
| `emote_threaten` |  |
| `emote_wait` |  |
| `emote_wave` |  |
| `emote_whistle` |  |

## seat_general (17)

| Action | Holdable |
|---|---|
| `v_eject` ⚠ |  |
| `v_emergency_exit` ⚠ |  |
| `v_enter_remote_turret_1` |  |
| `v_enter_remote_turret_2` |  |
| `v_enter_remote_turret_3` |  |
| `v_light_amplification_off` |  |
| `v_light_amplification_on` |  |
| `v_light_amplification_toggle` |  |
| `v_operator_mode_cycle_back` |  |
| `v_operator_mode_cycle_forward` |  |
| `v_set_flight_mode` |  |
| `v_set_guns_mode` |  |
| `v_set_mining_mode` |  |
| `v_set_quantum_mode` |  |
| `v_set_refuel_mode` |  |
| `v_set_salvage_mode` |  |
| `v_set_scan_mode` |  |

## spaceship_docking (1)

| Action | Holdable |
|---|---|
| `v_toggle_docking_request` |  |

## spaceship_general (11)

| Action | Holdable |
|---|---|
| `v_close_all_doors` |  |
| `v_flightready` |  |
| `v_lock_all_doors` |  |
| `v_lock_all_ports` |  |
| `v_open_all_doors` |  |
| `v_self_destruct` ⚠ |  |
| `v_toggle_all_doorlocks` |  |
| `v_toggle_all_doors` |  |
| `v_toggle_all_portlocks` |  |
| `v_unlock_all_doors` |  |
| `v_unlock_all_ports` |  |

## spaceship_movement (27)

| Action | Holdable |
|---|---|
| `v_atc_request` |  |
| `v_toggle_landing_system` |  |
| `v_deploy_landing_system` |  |
| `v_retract_landing_system` |  |
| `v_flight_advanced_hud_off` |  |
| `v_flight_advanced_hud_on` |  |
| `v_flight_advanced_hud_toggle` |  |
| `v_ifcs_toggle_gforce_safety` |  |
| `v_ifcs_gsafe_on` |  |
| `v_ifcs_gsafe_off` |  |
| `v_ifcs_proximity_assist_off` |  |
| `v_ifcs_proximity_assist_on` |  |
| `v_ifcs_proximity_assist_toggle` |  |
| `v_ifcs_reset_gmeter_max` |  |
| `v_ifcs_toggle_esp` |  |
| `v_ifcs_vector_decoupling_off` |  |
| `v_ifcs_vector_decoupling_on` |  |
| `v_ifcs_vector_decoupling_toggle` |  |
| `v_master_mode_cycle` |  |
| `v_master_mode_set_nav` |  |
| `v_master_mode_set_scm` |  |
| `v_transform_cycle` |  |
| `v_transform_deploy` |  |
| `v_transform_retract` |  |
| `v_vtol_off` |  |
| `v_vtol_on` |  |
| `v_vtol_toggle` |  |

## spaceship_power (12)

| Action | Holdable |
|---|---|
| `v_power_set_off` ⚠ |  |
| `v_power_set_on` |  |
| `v_power_set_shields_off` |  |
| `v_power_set_shields_on` |  |
| `v_power_set_thrusters_off` ⚠ |  |
| `v_power_set_thrusters_on` |  |
| `v_power_set_weapons_off` |  |
| `v_power_set_weapons_on` |  |
| `v_power_toggle` ⚠ |  |
| `v_power_toggle_shields` |  |
| `v_power_toggle_thrusters` ⚠ |  |
| `v_power_toggle_weapons` |  |

## spaceship_quantum (1)

| Action | Holdable |
|---|---|
| `v_toggle_qdrive_engagement` ⚠ |  |

## spaceship_radar (1)

| Action | Holdable |
|---|---|
| `v_invoke_ping` | yes |

## spaceship_target_hailing (1)

| Action | Holdable |
|---|---|
| `v_target_hail` |  |

## Notably absent

- Power **pip allocation** and power-triangle controls. Power actions are on/off per group only: shields, thrusters, weapons, whole ship.
- Weapons fire, missiles and countermeasure launch (no combat actions).
- Every axis/analog action (throttle, strafe, pitch, yaw, roll). The exe explicitly prevents these from being added.
- Speed limiter, cruise control and coupled mode. Decoupling is present as `v_ifcs_vector_decoupling_*`.

## Known issues

### Door and port actions return `FAILED` (PTU 5.0, RSI Perseus)
**Observed:** 2026-11-21, PTU 5.0.202.10025 (CL 13248297), RSI Perseus, player seated as pilot.

All 11 door and port actions return `TRIGGER_GAME_ACTION_STATUS_FAILED`, every time:
`v_open_all_doors`, `v_close_all_doors`, `v_toggle_all_doors`, `v_lock_all_doors`, `v_unlock_all_doors`, `v_toggle_all_doorlocks`, `v_lock_all_ports`, `v_unlock_all_ports`, `v_toggle_all_portlocks`.

- **Keyboard works.** The same actions bound to the keyboard do work from the pilot seat, so the ship supports them.
- **Other actions work.** In the same seconds, `v_set_*_mode`, `v_flightready`, `v_lights*` and `v_power_*` all returned `OK`.
- **No door telemetry.** `vehicle.state.doors` is absent from every frame for this ship, even though it has working doors.

**Conclusion:** it is a CIG-side defect in the telemetry trigger path for door and port actions, likely together with door state not being populated. It is not a client bug: the status comes from the game.

**Client handling:** show door buttons as `N/A` when `vehicle.state.doors` is absent, and treat `FAILED` on these actions as "unsupported via remote trigger".

**Reproduced on a second ship:** on a MISC Prospector (2026-11-21 16:08–16:10, pilot seat) `v_open_all_doors`, `v_close_all_doors`, `v_lock_all_ports` and `v_unlock_all_ports` also returned `FAILED`, and `vehicle.state.doors` was absent there too. So the problem is not specific to one ship: door and port remote triggers are broken generally, and door telemetry is not populated.

### Issue Council draft (not submitted)
> **Title:** [5.0 PTU] Local Game Telemetry `TriggerGameAction`: door and port actions always return FAILED (keyboard binding works)
>
> **Steps:**
> 1. Enable local game telemetry with `GRPC_CLIENT_SERVER_ENABLED=1`, `GRPC_CLIENT_SERVER_PORT=50051` and USER.cfg `server.game.telemetry.enabled = 1` + `server.game.telemetry.trigger.actions.enabled = 1`.
> 2. Sit in the pilot seat of an RSI Perseus.
> 3. Call `sc.game.telemetry.v1.GameTelemetryService/TriggerGameAction` with `action_name = v_open_all_doors` (also close, toggle, lock, unlock and the port variants).
>
> **Expected:** status `OK` and the doors open, as with the keyboard binding.
>
> **Actual:** status `TRIGGER_GAME_ACTION_STATUS_FAILED` and nothing happens. Other allowlisted actions (`v_lights`, `v_power_*`, `v_set_*_mode`, `v_flightready`) return `OK` and work. The `TelemetryStream` frames for this ship also never include `vehicle.state.doors`.
>
> Reproduced on RSI Perseus and MISC Prospector.
>
> **Build:** PTU 5.0.202.10025 (13248297), Linux/Wine.

### Operator modes: `OK` does not mean supported, and current mode is only partly visible
**Observed:** 2026-11-21, PTU 5.0, MISC Prospector, pilot seat.

- **Every `OK`:** `v_set_mining_mode`, `v_set_refuel_mode`, `v_set_salvage_mode`, `v_set_quantum_mode`, `v_set_flight_mode` and `v_set_guns_mode` all returned `OK`, including refuel and salvage on a ship that has neither. The status only confirms that the input was injected.
- **No mode field:** the telemetry has no "current operator mode" field, so the mode has to be inferred.
- **Guns vs not guns:** switching to mining mode set `ready_to_fire=false` on both guns, and `v_set_guns_mode` set it back to `true` (16:10:07 and 16:10:15). Guns mode can therefore be inferred as "vehicle guns ready_to_fire", but non-guns modes (mining, salvage, scan, quantum, refuel, flight) can't be told apart.
- **Separate from master mode:** quantum operator mode did not change `master_mode` (it stayed SCM). Operator modes and NAV/SCM are independent.
- **Mining laser not listed:** `weapons.weapons[]` lists only the guns (2× `KLWE_LaserRepeater_S1`). Mining capability can't be detected from the stream and needs game data.
