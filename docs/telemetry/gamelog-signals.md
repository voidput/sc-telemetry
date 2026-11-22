---
title: Game.log signals beyond the telemetry API
tags: [docs, telemetry]
updated: 2026-11-21
---

# Game.log signals beyond the telemetry API

**Scanned:** 2026-11-21, PTU 5.0.202.10025. One session of about 50 minutes (31,060 lines): Perseus flight, quantum travel, Prospector, a death and respawn. No combat with other players took place.

**Why this note exists:** the gRPC telemetry API has **no target, contact, radar or scan data** (see [api-spec.md §4](api-spec.md)). This note records what `Game.log` offers instead. All of it is unofficial debug logging, so formats can change in any patch.

**Wider scan:** a later pass over about 290 LIVE, HOTFIX and PTU logs is written up in [gamelog-catalog.md](gamelog-catalog.md) (every useful line, lifecycles, format drift, gaps) and [gamelog-parser-plan.md](gamelog-parser-plan.md) (ranked parsers to add to the tailer). It corrects one point below: ship kill attribution **is** logged, as `<Vehicle Destroyed>` (4.10.200 onward and PTU 5.0), not `<Vehicle Destruction>`.

## Useful signals

| Signal | Example line (ids/IPs stripped) | What it gives you |
|---|---|---|
| **Ships streamed in nearby** | `<Local Route Guard - Server Rerouted> ... NOT AUTH \| DRAK_Corsair\|CSCItemNavigation::PostInitialize ... FinalStop=0` | The **class name of other ships** whose navigation initialises in your client, i.e. ships within your streaming bubble. This session showed 17 classes (Reclaimer, Redeemer, Liberator, Corsair, Galaxy, Hyun…). There is **no owner, position, distance or hostility**, and parked ships at stations and hangars appear too. |
| **Quantum arrivals** | `<Quantum Drive Arrived - Arrived at Final Destination> ... NOT AUTH \| ANVL_Lightning_F8C_Exec ...` | A ship finished quantum travel within your bubble. Your own ship appears in this event too (`RSI_Perseus`). |
| **Jurisdiction / zone changes** | `<SHUDEvent_OnNotification> Added notification "Entered Ungoverned Jurisdiction: "` | HUD zone notices: Armistice enter/leave, Monitored Space, the faction or jurisdiction name (e.g. "People's Alliance"). This is a good "where am I / is combat allowed" signal. |
| **Vehicle control** | `<Vehicle Control Flow> CVehicleMovementBase::ClearDriver: Local client node [...] releasing control token for 'RSI_Perseus_...'` | You gave up piloting a specific ship. It is a more reliable "left pilot seat" signal than `locomotion`. |
| **Death / item recovery** | `<Adding non kept item [CSCActorCorpseUtils::PopulateItemPortForItemRecoveryEntitlement]> Item 'rsi_odyssey_undersuit_...'` | **A corpse's item list near you. The line names no owner, so it is NOT your death:** it fires just the same for a player you kill nearby (2026-11-21: 39 items logged while the player sat in their ship at 63 health). Own death/downed comes from the telemetry events `GAME_EVENT_TYPE_ACTOR_DEAD` / `ACTOR_DOWNED`. |
| **Hangar / ATC / docking** | `Hangar Request Completed`, `<ATC_...>`, `<CSCItemDockingTube::OnSetTubeState>` | Hangar and docking flow. |

## Not found in this session
- Kill and death attribution lines (attacker, victim, weapon): no player-vs-player combat happened. **To do:** re-scan after combat to see whether PTU 5.0 still logs `<Actor Death>` / `<Vehicle Destruction>`.
- Crime stat or hostile flags.
- Anything about your locked or selected target, contacts, radar returns, ping results or scan results.

## Implemented
The bridge's Game.log tailer (`sctelemetry/gamelog.py`: `parse_signal`, `classify_zone`, `SignalsState`) turns these lines into state and events. The HTTP side is documented in [api-spec.md §8.4](api-spec.md).

- **Nearby ships.** `Local Route Guard - <any> ... | NOT <AUTH|INIT> | <CLASS>_<id>[<id>]|CSCItemNavigation::PostInitialize`. De-duplicated per entity id *inside the bridge*; exposed as `{class, display, count, first_seen, last_seen}` per class (most recently seen first, at most 50 classes; at most 1000 entities remembered). Event `NEARBY_SHIP` on first sighting only, at most one per second: a burst (including the initial full read) becomes one event, "N new ships: A, B, C…".
- **Own ship.** Every entity named in a `Vehicle Control Flow ... releasing control token for '<CLASS>_<id>'` line this session, plus the telemetry `vehicle.state.name` (`<CLASS>_<id>`), is yours: excluded from nearby (also retroactively, and still after you switch ships) and shown as `own_ship`. `own_ship` comes from whichever source changed most recently (a new telemetry vehicle or a later seat release). The UI mutes the zone badge and Nearby panel while the session is idle (Game.log not written for 2 min and no telemetry). A reclaimed ship gets a new entity id, so it can show up as nearby until you fly it.
- **Quantum arrivals.** `QT_ARRIVED {class, self}`; another ship's arrival also counts as a sighting.
- **Zone.** Only zone notices count (`Entered/Entering/Leaving/Left ...` + Armistice / Monitored Space / Jurisdiction); hangar, injury and QT-calibration notices are ignored. `zone = {kind: armistice|monitored|ungoverned|jurisdiction|other, text, since}`; `armistice` is set by "Entering Armistice Zone" and cleared only by "Leaving Armistice Zone"; `jurisdiction` keeps the last jurisdiction name. Event `ZONE {kind, text}` when the text changes.
- **Vehicle control.** `VEHICLE_RELEASED {class}`.
- **Death.** `CORPSE_ITEMS {count}`: corpse item lines within 5 s (log time) are one death; item names and ids are not kept.
- **Reset.** All signal state resets on a new Game.log (rotation), `Log started`, and `SESSION_JOIN` / `SESSION_SHARD_CHANGE` / `SESSION_LEAVE`.
- **Privacy.** Entity ids never leave `SignalsState`: snapshot, SSE, `/api/signals`, events and `--record` carry class names only. Tests use synthetic lines with fake ids (`tests/test_signals.py`).

First run against the 2026-11-21 PTU log: 25 ships in 19 classes nearby, own ship Prospector (last piloted), 10 zone notices (Ungoverned, Monitored, Armistice in/out, People's Alliance), 7 QT arrivals, 7 seat releases, one death with 17 corpse items.
