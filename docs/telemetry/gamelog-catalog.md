---
title: Game.log event catalog
tags: [docs, telemetry]
updated: 2026-11-21
---

# Game.log event catalog

**Scanned:** 2026-11-21. About 290 `Game.log` files and log backups (6.06M lines) from LIVE, HOTFIX and PTU installs, read by six research passes (missions, combat, vehicles, social, economy, longtail), then checked by a completeness critic and recounted over 293 files. Shader-cache-generation logs were skipped.

**What this note is:** the reference list of every Game.log line worth parsing, what it means, which fields can be extracted, and where the log is silent. The ranked build order for the bridge tailer is in [gamelog-parser-plan.md](gamelog-parser-plan.md). The signals the tailer already implements are in [gamelog-signals.md](gamelog-signals.md).

**Privacy rule for this note:** every template uses placeholders (`<player>`, `<id>`, `<uuid>`, `<ip>`, `<xyz>`). No real handle, id, address or coordinate appears here, and none may be added. Ship, item, contract and location *class* names are game data and are fine.

**Counts are corpus counts**, written `lines / files`. They show how common a line is, not how reliable it is. The passes used slightly different file sets (289 to 293 files, and the live `Game.log` keeps growing), so small differences between counts are expected. Counts corrected by the recount use the 293-file set; the ones that matter are listed under [Open questions](#open-questions).

## Builds in the corpus

| Build | FileVersion | Branch header | Channel | Files (approx.) |
|---|---|---|---|---|
| 12545750 | 1.0.191.x | sc-alpha-4.10.0-hotfix | LIVE + HOTFIX | 24 |
| 12572603 | 1.0.191.x | sc-alpha-4.10.0-hotfix | HOTFIX | 37 |
| 12660092 | 4.10.193 | sc-alpha-4.10.x | LIVE | 78 |
| 12881860 | 4.10.196 | sc-alpha-4.10.x | LIVE | 20 |
| 12913470 | 4.10.197 | sc-alpha-4.10.x | LIVE | 77 |
| 13165946 | 4.10.200 | sc-alpha-4.10.0 | LIVE | 30 |
| 13220821 | 4.10.201 | sc-alpha-4.10.0 | LIVE (current) | 13 |
| 13189690 | 5.0.201 | sc-alpha-5.0.0 | PTU | 7 |
| 13248297 | 5.0.202 | sc-alpha-5.0.0 | PTU (current) | 7 |

- **`FileVersion` is not a safe build key.** The hotfix builds report `1.0.191.x`. Key on `Build(<n>)` (in the backup filename and the `BackupNameAttachment` header line) and on the `Branch: sc-alpha-X.Y.Z` header line.
- **PTU 5.0 is 14 short files (about 111k lines).** A line missing from 5.0 is weak evidence that it was removed. This caveat applies to every "not seen in 5.0" statement below.

## Line anatomy

```
<ts> [SPAM <n>]?[<Level>] <Tag> <message> [Team_<X>][<Category>]
```

- **Timestamp:** `<YYYY-MM-DDTHH:MM:SS.mmmZ>`.
- **Level:** tagged lines carry only `Notice` (about 1.56M) or `Error` (about 676k), plus 4 `[Critical]` lines (`<[EAFrontend] ChangeGameMode>`, 4.10.201 and 5.0.201). `Warn` never occurs. `Trace` (about 1.8k) appears only on untagged lines. `[flow] <noname> : ...` (1,136 / 284, flowgraph load errors) looks tagged but is noise.
- **Tag:** the text in the first `<...>` after the level. Casing and punctuation are stable across builds. Anchor parsers on the tag.
- **Team suffix:** varies (`[Team_CGP<n>]`). Match `\[Team_\w+\]` and never pin the digit, or ignore the suffix.
- **`[SPAM <n>]` prefix** (19,194 lines, all builds): a rate limiter. It never marks the first occurrence of a line. After that, about one line is written every 3 s and `<n>` is the number of repeats suppressed in that window (median about 240, max over 100k). Strip the prefix, keep `<n>` as a repeat count, and do not count it as `<n>` events.
- **64% of lines have no tag** (asset loads, particle-limit continuations, indented blocks). Drop any line that does not match the tagged form, with the two exceptions below and the session lines listed under [Session, login and connection](#session-login-and-connection).
- **File header:** each file opens with untagged header lines. `Executable:` holds the install path (which can contain the OS username) and `Host CPU:` names the machine's CPU. Read the `Build(<n>)` and `Branch:` values from the header and drop the rest; never emit or record header lines.

### Multi-line HUD notifications

`<SHUDEvent_OnNotification>` text can contain a newline. This is the single biggest parsing hazard, and it affects missions, crime, party and currency lines.

```
<ts> [Notice] <SHUDEvent_OnNotification> Added notification "<title>
<ts> <body>: " [<seq>] to queue. New queue size: <n>, MissionId: [<uuid>], ObjectiveId: [<uuid>?]
```

- The first line has no closing quote and no `MissionId`.
- The continuation has a timestamp but **no level and no tag**. It can start with ` : "` (when the body is empty) or with body text.
- Join lines from `Added notification "` until `" [<n>] to queue` appears.
- A few bodies are corrupted (garbage bytes). Validate the body before using it.
- Each toast is logged again 3 to 4 times by `<UpdateNotificationItem> ... Action: Next/StartFade/Remove`, and build 12881860 also dumps the whole queue as untagged lines under `<UpdateHUDNotifications>` (about 10k duplicates). **Count `SHUDEvent_OnNotification ... Added` only.**
- The tailer's current notification regex needs the closing quote on the same line, so today it silently skips every multi-line toast.

### Local identity

Several lines only become useful once the tailer knows which handle and player id are the local player's. Both must stay in memory and never be emitted or recorded.

| Source line | Gives | Builds |
|---|---|---|
| `<Legacy login response> [CIG-net] User Login Success - Handle[<player>] - Time[<n>]` | local handle | LIVE, HOTFIX (282 / 269) |
| `<CLoginSession> [CompleteAuthentication] {success} Fully authenticated : handle[<player>] hub[<n>] in <n>s` | local handle | PTU 5.0 (12 / 12) |
| `<CEntityComponentFrontendPUUIProvider::AddAccessibleDestinations> ... destinations received for player [<player>]` | local handle | all, 5.0 included (1,122 / 280). The only handle source present in every build. |
| `<AccountLoginCharacterStatus_Character> Character: createdAt <n> - updatedAt <n> - geid <id> - accountId <id> - name <player> - state STATE_<S>` | local player id (also the account id and handle) | all (283 / 283) |
| `<Vehicle Control Flow> ... Local client node [<id>] ...` | local player id (same value) | all |

Several accounts can share one install, so resolve identity per file.

## Missions and contracts

1,417 distinct mission ids were seen. **`mission_id` (a uuid) is the only reliable join key.** Use it inside the tailer and never emit it.

| Tag / template | Meaning | Fields | Count | Builds | Reliability |
|---|---|---|---|---|---|
| `<SHUDEvent_OnNotification> Added notification "<Kind>: <text>: " [<n>] to queue. New queue size: <n>, MissionId: [<uuid>], ObjectiveId: [<uuid>?]` | HUD toast. The only source of contract **titles**. | kind, title, mission_id, objective_id | see kinds below | all, 5.0 included | High |
| `<ObjectiveUpserted> Received ObjectiveUpserted push message for: mission_id <uuid> - objective_id <oid> - state MISSION_OBJECTIVE_STATE_<S> - created <0/1> - flags=<A\|B\|...>` | Server objective state | mission_id, objective_id, state, created, flags | 13,654 / 63 | 4.x, 5.0 (8 lines) | High |
| `<ObjectiveComplete> Received ObjectiveComplete push message for: mission_id <uuid> - objective_id <oid> - state MISSION_OBJECTIVE_STATE_WITHDRAWN` | Objective removed (always WITHDRAWN) | mission_id, objective_id | 590 / 27 | 12572603 to 13165946 | Gone after 4.10.200 |
| `<EndMission> Ending mission for player. MissionId[<uuid>] Player[<player>] PlayerId[<id>] CompletionType[<T>] Reason[<R>]` | Client-side mission end | mission_id, completion type, reason. **Carries a player name.** | 815 / 56 | 4.x, 5.0 | High |
| `<MissionEnded> Received MissionEnded push message for: mission_id <uuid> - mission_state MISSION_STATE_<S>` | Server-side mission end | mission_id, final state | 718 / 53 | 4.x, 5.0 | High |
| `<MissionShared> Received share push message: ownerId[<id>] - missionId[<uuid>]` | A mission was shared into your log (your own in a party, or a party member's) | mission_id | 1,266 / 28 | 12572603 to 13189690 | Not seen in 13220821 |
| `<MissionSharedOnMessaged> onMessage` | Always pairs 1:1 with `MissionShared` | none | 1,266 | same | Ignore |
| `<CLocalMissionPhaseMarker::CreateMarker> Creating objective marker: missionId [<uuid>], generator name [<gen>], contract [<contract_def>], contractDefinitionId[<uuid>], objectiveId [<oid>], markerEntityId [<id>], zoneHostId...` | **Mission type**, via the contract definition name | mission_id, generator, contract_def, objective_id | 1,374 / 49 | 12572603 to 13220821 | High; absent in 12545750 |
| `<SMarkerHandler_Base::CreateMissionObjectiveMarker> Creating objective marker: ... contract [<contract_def>][<uuid>] ...` | Same information, hauling and recovery variant | same | 48 / 2 | 12881860 (4), 12913470 (44) | Rare |
| `<CMissionLogEntry::UpdateActiveObjective> Objective updated id=<oid>, flags=<...>, hidden=<0/1>, hiddenInUI=<0/1>, markerHidden=<0/1>, uiDisplay[Priority=<n>][Text=<objective text>]` | **Objective display text** | objective_id, text, visibility | 12,149 / 61 | 4.x, 5.0 | High |
| `<CObjectiveMarkerComponent::AddToPlayerDataBank / RemoveFromPlayerDataBank> MissionObjectiveMarker_<id>[<id>] - Added to/Removed from DataBank of Player: <player>[<id>] - ZonePos <xyz>, missionId[<uuid>], objectiveId[<oid>]` | Marker given to or taken from each party member | mission_id, objective_id. **Carries player names and coordinates.** | Add 26,001 / 67, Remove 16,713 / 63 | 4.x, 5.0 | SPAM-heavy. Do not parse. |
| `<CommsNotifications> SendCommsNotification +Missions.Organization.<Org>,Missions.Organization.<Org>.<Giver>,Missions.MissionType.<Type>[,...],Missions.CommsNotifications.MissionAccept - [<uuid>] - Mission: [<uuid>], Player: <player>[<id>]` (38 lines insert ` -<removed tags>` and drop the `- ` before the first uuid) | Mission briefing call sent on accept. **Readable mission type, organisation and giver.** | mission_id (joins to the toast and `EndMission` ids: 443 of 443 distinct ids matched in the same file), org, giver, type. **Carries the local handle.** | 496 / 48 (the tag has 957 lines; the other 461 are `CommsNotification [<uuid>] skipped - Mission [<uuid>] not tracked by Player: <player>[<id>]`) | 12545750 to 13165946, plus 1 line in 13189690. None in 13220821 or 13248297. | High where present |
| `<CSCPlayerMissionLog::MissionStartCommsNotification> ... ContractId: [<uuid>]. MissionId: <uuid>` | Maps contract id to mission id | contract_id, mission_id | 125 / 20 | 4.x | Low value |
| `<GenerateLocationProperty> Generated Locations - variablename: <var>, locations: (<Display Name> [<id>] [<loc_code>])... contract: <contract_def>` | Locations picked for a contract when it is generated. **Location display names** mapped to internal codes. | variable, display name, location code, contract_def. No mission_id, and only 29 of 1,017 distinct contract definitions also appear in a `CreateMarker` line, so tying a location to the accepted mission is weak. | 6,958 / 144 | 12545750 to 13220821. **0 in PTU 5.0** (both builds). | High in 4.x. `locations:` can be `INVALID` or empty, and names can contain `~mission(...)` text. |
| `<SHUDEvent_OnCommsNotification>` / `<SpawnCommsEntity>` / `<Comms Notification Flow> ... StartTransmission: Playing communication <n>` | Mission comms call (a giver hailing you) | none useful. `SHUDEvent_OnCommsNotification` and `SpawnCommsEntity` **carry the local handle**. | 2,873 / 55, 2,758 / 54, 961 / 50 | 12545750 to 13165946 and PTU 5.0 (a few lines); none in 13220821 | Low value |
| `<PlayerJoined>` / `<PlayerLeft> Received PlayerJoined push message for: mission_id <uuid> - player_id <id>` | Party member added to or removed from a shared mission | mission_id, party size per mission (player ids internal) | 6,816 / 34 and 1,276 / 36 | 12545750 to 13165946. Gone in 13220821 and 5.0. | Medium, 4.x only |
| `<RequestShareMissionInteraction ShareMission Fail> Failed to share mission id: (<uuid>) ... Error: INVALID_ARGUMENT(<n>): '...'` | Share failure | mission_id, error | 14 / 5 | 12881860 to 13165946 | Rare |
| `<SMarkerHandler_Hauling::OnItemRegistered> Mission Item Carryable_<n>H_FL_<dims>_Commodity_<category>_<Name>_<n> (<id>) registered with mission id <uuid>, phase id <uuid>, pickup objective id ..., drop off objective id ...` (and `OnItemUnregistered`) | Hauling cargo box tied to a mission | commodity name, box dimensions, mission_id | 48 + 19 / 2 | 4.10.196 to 4.10.197 | Rare |
| `<CContractGenerator::CContractGenerator> Initializing Contract Generator with seed <id>` | Once per session | none | 286 / 286 | all | Session marker only |

**Notification kinds:**

| Kind | Count | Notes |
|---|---|---|
| `Contract Accepted` | 1,048 / 70 | Followed by **two spaces** before the title |
| `Contract Shared` | 1,265 / 28 | `MissionId` is always all-zero. Pair with the `<MissionShared>` line at the same timestamp. |
| `Contract Complete` | 617 / 44 | Equals `EndMission` Complete (617) |
| `Contract Failed` | 80 / 20 | |
| `Contract Available` | 59 / 41 | |
| `Contract Withdrawn` | 19 / 7 | Two spaces before the title |
| `Contract Not Accepted: Contract Limit Exceeded: ` | 37 / 3 | The limit value is not logged |
| `New Objective` | 915 / 69 | Carries `ObjectiveId` |
| `Objective Withdrawn` | 375 / 25 | Carries `ObjectiveId` |
| `Objective Complete` | 385 / 46 | **Split over two lines** |
| `Objective Failed` | 7 / 2 | |
| `Journal Entry Added: <title>` | 697 / 41 | |
| `Received Blueprint: <item name>` | 23 / 5 | 4.10.196 to 4.10.200 only. Arrives in bursts of 3 to 9 after `Contract Complete`. |

**Field values:**

| Field | Values |
|---|---|
| Objective state | INPROGRESS 11,223 (2,024 with `created=1`), COMPLETED 978, FAILED 10, WITHDRAWN 1 |
| Objective flags | RespectInheritedVisibility, ShowInLog, ShowProgress, SilentUpdates, DisplayMarkerDistance, Hidden, HiddenInUI, AutoAddNewPlayers (shared party), MarkerHidden, BountyMarker, HideMarkerWhenNotAtLocation, OverallPhase, IgnoredForInheritedVisibility |
| `EndMission` type / reason | Complete / "Mission Ended" 617; Abandon / "Player left" 98; Fail / "Mission Ended" 80; Deactivate / "Mission Ended" 20 |
| `MissionEnded` state | COMPLETED 618, FAILED 80, WITHDRAWN 10, EXPIRED 10 |
| `objective_id` forms | `<uuid>`, `phase_<uuid>` (hidden overall phase), `pickup_<uuid>_<n>`, `dropoff_<uuid>_<n>` (hauling). A uuid-only regex drops 25% of `ObjectiveUpserted` lines. |
| Contract definition | Codenames such as `<Faction>_Bounty_<System>_<Difficulty>`, `<Event>_HaulCargo_AtoB_*`, `*_DefendShip_<Easy..Hard>`, `*_Salvage_*`, `*_BlackBoxRecovery_*`. Strip the trailing `_<n>`. |
| Title markup | `<EM4>[BP]</EM4>`, `<EM4>[<n> Rep]</EM4>` (also `[<a>/<b>/<c> Rep]` and `[<a>–<b> Rep]` with an en dash), `[H]`, `[E-S<n>]` in blueprint names, `[ DRAFT ]` in journal titles. Every title ends with `: "`. |
| Progress text | `Waves Defeated: %ls`, `Areas Searched: %ls`, `Time Before Target Retreat: %ls`. The `%ls` count is never filled in. |

## Combat, crime, injury and death

There is no shared id across these lines. Correlate by time window and by local identity.

| Tag / template | Meaning | Fields | Count | Builds | Reliability |
|---|---|---|---|---|---|
| `<Vehicle Destroyed> Vehicle '<ship_class>_<id>' [<id>] class '<ship_class>' (owner player [<id>\|0], driver [0]) destroyed: reason [<R>] level [<a>] -> [<b>] flags [<n>] killer '<player\|npc_class\|none>' [<id>] authority [<n>]` | A vehicle in your streaming bubble was disabled or destroyed. **Names the killer.** | class, reason, level change, owner id (0 = AI or no owner), killer. `driver` is always 0. | 538 / 30 | 4.10.200 onward and 5.0. Absent in every earlier build. | High where present |
| `<[ActorState] Dead> [ACTOR STATE][CSCActorControlStateDead::PrePhysicsUpdate] Actor '<player>' [<id>] ejected from zone '<ship_class>_<id>' ... to zone '<zone>' ... due to previous zone being in a destroyed vehicle with detached interior` | An actor died inside a ship that blew up | actor (compare to local, then drop), ship class, destination zone | 139 / 44 | HOTFIX, 4.10.193 to 5.0.201 | Medium |
| `<FatalCollision> Fatal Collision occured for vehicle <ship_class>_<id> [Part: <part>, Pos: <xyz>, Zone: <zone>, PlayerPilot: <n>] after hitting entity: <entity\|player\|UNKNOWN> [Zone: ...]` | Ship crash | class, part, zone, player-pilot flag. **Position and hit entity can be private.** | 55 / 27 | 12572603 to 13220821 (4.10.200: 1, 4.10.201: 3) and PTU 5.0.201 (1) | Rare but still logged |
| `<Boundary Violation> CVehicle::CheckForOceanCollision: '<veh>' ... starting countdown (<f>s)` / `being destroyed due to intersecting with ocean ... zone '<zone>'` / `CEntityComponentPlanetKillVolume ... starting countdown (<f>s)` | Environmental vehicle death. The countdown is a float (`3.0s` form), not an integer. | countdown seconds, zone. **Position and velocity are in the line.** | 419 / 2: 396 `being destroyed`, 23 `starting countdown` | 4.10.196 to 4.10.197 | Rare |
| HUD `Crime Committed: <crime>⏎against <victim>: ` | You committed a crime. **Multi-line.** No-victim forms: `Crime Committed: Property Theft: `, `Crime Committed: Trespassing (First Degree): ` | crime, has-victim flag | 210 / 22 (after the join) | HOTFIX, 4.10.193 to 4.10.201 | High in 4.x |
| HUD `<attacker> committed <crime> against you⏎Press '<key>' to forgive or the crime will be reported: ` | You are the victim. Names the attacker. | crime | about 8 / 3 | 4.10.193 | Rare |
| HUD `CrimeStat Rating Increased: ` | CrimeStat went up. Logged about 13 ms after a crime, and again on a prison spawn. | none (no rating value) | 63 / 33 | to 4.10.201 | High in 4.x |
| HUD `Fined <n> UEC: ` | Fine | amount | 7 / 7 | to 4.10.201 | High in 4.x |
| HUD `<Severity> Injury Detected - <Head\|Torso\|Left arm\|Right arm\|Left leg\|Right leg> - Tier <1-3> Treatment Required : ` | Injury | severity (Minor, Moderate, Severe), body part, tier | 92 / 22 | 12545750 to 12913470, and 1 in 5.0.202. None in 4.10.200 or 4.10.201. | High where present |
| `<MED BED HEAL> Actor: <player> (...) \| [CEntityComponentMedBed::HandleComponentEvent:<n>] \| -> Perform surgery event Success, med bed name: <bed_class>, vehicle name: <vehicle\|none>, head: <bool> torso: <bool> leftArm: <bool> rightArm: <bool> leftLeg: <bool> rightLeg: <bool>` | Med bed treatment | bed class, ship med bay, healed parts | about 16 / 9 | to 4.10.197 | Gone after 4.10.197 |
| HUD `Standby, Local Emergency Services Are En Route: ` | You died and a respawn is pending | none | 92 / 71 | to 4.10.201. One 5.0 death happened without it. | 4.x only |
| `<Adding non kept item [CSCActorCorpseUtils::PopulateItemPortForItemRecoveryEntitlement]> Item '<item_class>_<id> - Class(<item_class>) ...', Recorded data is: Port Name '<port>', Class GUID: '<uuid>'[, KeptId: '<id>']` | **Your corpse loadout** at death. A burst is about 34 lines in one second and always includes `Body_ItemPort` and `mobiglas_attach`. | port → item class | 49.7k lines, about 950 bursts | all | **The only own-death signal in every build** **Caveat (verified 2026-11-21):** no owner in the line, so a burst also fires for other players' corpses near you; it is not an own-death signal. |
| `[CSessionManager::OnClientSpawned] Spawned!` | First in a file: login. Later ones: respawns. | none | 1,279 / 279 | all | High |
| `<AttachmentReceived> Player[<player>] Attachment[uba_prison_<piece>_...] ... Port[...]` plus `<RequestLocationInventory> ... Location[PrisonMine_<loc>]` | Prison spawn | prison location class | 132 / 24 attachments, 24 / 18 locations | 4.10.193 to 4.10.201 | High in 4.x |
| HUD `Entered Klescher Rehabilitation Jurisdiction: ` | Prison spawn, as a single-line toast | none | 25 / 19 | 4.10.193 to 4.10.201 | High in 4.x. The simplest prison signal. |
| HUD `Entering Armistice Zone - Combat Prohibited: ` / `Leaving Armistice Zone - Caution Advised: ` | Armistice state | none | 2,046 / 215 and 1,138 / 177 | all | High (implemented) |
| HUD `Restricted Area - Vehicles Will Be Impounded: `, `Restricted Area⏎For your safety, you've been relocated.: `, `Leaving Restricted Area: `, `Monitored Space Down: ` | Restricted-area warn, teleport and exit; comm array down | none | 14, 14, 53, 3 | to 4.10.200 | Medium |
| `[VEHICLE SPAWN] CPlayerShipRespawnManager::OnVehicleSpawned <id> (<ship_class>_<id>) by player <id>` | Ship spawned or reclaimed | ship class | 63 / 13 | 12572603 to 5.0.201 (not every build) | Medium |
| `<[ActorState] Place> ... '<player>' ... placed '<item_class>' in lootable container '<container>'` | Fuse, chip or blade placed (Contested Zones) | item class, container class | 180 / 22 | to 4.10.200 | Gone after 4.10.200 |
| `<[EASpawn] ScheduleRespawn/PerformRespawn/SpawnStateTransition prev:<S> next:<S>>` | Arena Commander respawn state machine | state | about 900 with `[EALobby]` | 4.10, part of 5.0 | Low value |
| `Logged a start of a status effect! nickname: <player>, status effect: <Dehydrated\|Starved>` | Hunger or thirst status. No tag. **Covers other players too.** | effect (own only) | 118 / 59 | HOTFIX, LIVE, 5.0.202 | Low |

**`Vehicle Destroyed` values:**

| Field | Values |
|---|---|
| reason | Combat 262, SelfDestruct 185, Collision 61, Ejection 18, ShipItemCritical 6, GameRules 6 |
| level | 0→2 (505, destroyed outright), 0→1 (18, soft death: disabled), 1→2 (15, soft then hard) |
| owner | 0 for AI or unowned ships (216); otherwise a player id. Equal to the local player id means "my ship" (7 cases). |
| killer | a player handle (about 490), an NPC or turret class, or the literal `none`. SelfDestruct and Collision also carry a killer: whoever triggered it. |

**Crimes seen:** Destruction of Vehicle 65, Negligent Homicide 42, Armistice Violation 39, Grievous Bodily Harm 35, Homicide 16, Aggravated Assault 6, Battery (First Degree) 1.

## Vehicles and travel

Most of these lines use the ItemNavigation pipe format, which is stable in every build:

```
<Tag> [ItemNavigation][CL][<n>] | NOT AUTH | <ship_class>_<id>[<n>]|<Class::Function>|<body> [Team_<X>][<Category>]
```

| Event | Template | Fields | Count | Builds | Reliability |
|---|---|---|---|---|---|
| QT target selected (own ship) | `<Player Selected Quantum Target - Local> ...\|CSCItemNavigation::OnPlayerSelectedQuantumTarget\|Player has selected point <dest> as their destination, routing locally` | ship class, ship entity id, destination code | 1,062 / 110 | all | High. Logged again on every re-plan. |
| Route calculated | `<Calculate Route> ...CalculateRoute\|Successfully calculated route to <dest> fuel estimate <n>` | destination, **QT fuel estimate** | 1,349 | all | High |
| Route start | `<Calculate Route> ...\|Projected Start Location is <location> for route to destination <dest>` | **current system or body** (`<System> System`, `<System> Gateway`, planet, moon, station) | about 1,350 | all | High |
| Surface leg added | `<Calculate Route!> ...\|Adding surface location <dest> to end of route. New final index <n>` | destination | 299 / 43 | all | Medium |
| Fuel request | `<Player Requested Fuel to Quantum Target - Local\|Server Routing> ...\|Player has requested fuel calculation to destination <dest>` | ship, destination | 563 / 113 | all | Medium |
| QT calibration | HUD `Quantum Travel Calibration Started By <player>: ` / `Quantum Travel Calibration Complete By <player>: ` | phase, own vs party (the name can be empty) | 338 started / 183 complete | all | High |
| Party QT to you | HUD `<player> has started to QT to your current location.: ` | none after redaction | 15 / 13 | all | Low volume |
| QT arrived | `<Quantum Drive Arrived - Arrived at Final Destination> ...<ship_class>_<id>[<n>]\|...OnQuantumDriveArrived\|Quantum Drive has arrived at final destination` | ship. Own when the entity id matches the select line (733 matched flows). | 432 / 134 | all | High (implemented) |
| QT state desync | `<Trying to QT when not in Idle> ... User: <player>, Client: <player>, Current State: <n>...` | none useful. **Names and position.** | 236 / 87 | all | Ignore |
| Nearby ship | `<Local Route Guard - Server Rerouted> ... \| NOT AUTH \| <ship_class>_<id>[<n>]\|CSCItemNavigation::PostInitialize ...` | ship class | 6,531 with a class; 27,971 more are `\| NULL ENTITY\|` | all | Implemented. 81% of lines carry no class. 181 classes seen. |
| Left pilot seat | `<Vehicle Control Flow> CVehicleMovementBase::ClearDriver: Local client node [<id>] releasing control token for '<ship_class>_<id>' [<id>]` | ship class | 472 / 132 | all | High (implemented). **The only Vehicle Control Flow template: there is no enter line.** |
| ASOP list | `<OnRequestFetchVehicles> Fetching player vehicle list` | none | 1,092 / 183 (the tag has 2,984 lines: also `Querying location inventory` 946 and `Querying hangar inventory for player [<id>]` 946) | all | High |
| ASOP list done | `<VehicleListQuery> Fetching vehicle list for player <id> completed. Retrieved <n> entitlements out of <n> vehicules.` | fleet size | 1,037 / 175 | all | High |
| Ship spawn assigned | `<CEntityComponentShipListProvider::SetVehicleSpawningInformations> SetVehicleSpawningInformations - VehicleEntityId: [<id>], LandingArea: <player>'s ...\|Docking Port <n>` | landing area kind (personal hangar or docking port) | 245 / 119 | all | Medium. **Player name in the hangar label.** |
| Hangar ready | HUD `Hangar Request Completed: ` | none | 326 / 116 | all | High |
| Ship stowed | `<LandingArea_UnregisterFromExternalSystems_StowingVehicle> [STOWING ON UNREGISTER] <landing_area> [<id>] - Attempting to stow current vehicle [<id>] ...` | landing-area kind and size | 2,916 / 210 | all | High. `<landing_area>` forms: `LandingArea_ShipElevator_Hangar<Size><Pos>[_<loc>]` 2,353; `[PROC]LandingArea_Pad_<Size>-<n>_{<GUID>}` (outpost pads) 343; other names (`<loc>_Garage_<n>`, `LandingArea_<n>`, `Pad <n>-<n>`, some with spaces) 220. |
| Hangar platform | `<CSCLoadingPlatformManager::LowerLoadingPlatform\|RaiseLoadingPlatform> ... 'LoadingPlatformManager_ShipElevator_Hangar<Size>Top' ...` | hangar size | 105 / 40 | all | Low value |
| Insurance claim | `<Claim request sent> Sending claim request [<uuid>] to server for entitlement [urn:sc:platform:entitlement:uuid:<uuid>]. Expedited: <n>` → `<RmTokenResponseClaim> Received claim response [<uuid>] from server, result: <result>` | expedited flag, result | 1 + 1 | **5.0 only** | New; tiny sample |
| Claim failed | `<Claim Failed> Claim failed with error: FAILED_PRECONDITION(<n>): 'Claim vehicle failed: <PriceMismatch\|ClaimEntitlementRequestFailed>'` | reason | 2 | 4.10.x | Rare |
| Delivery | `Deliver request sent` / `RmTokenResponseDeliver` | result | 1 to 5 | 5.0 only | New |
| Kiosk errors | `<CEntityComponentShipInsuranceProvider::*> [CLAIM FAILED\|VEHICLE STORING FAILED\|NO ATC\|REQUEST FAILED] ShipSelectorKiosk_<loc> ... ATC Location: <loc>` | failure kind, location | about 20 | 4.10.x | Rare |
| Docking tube | `<CSCItemDockingTube::OnSetTubeState> <tube_class> [<id>] - Tube opened: broadcasting OnShipCanDock\|no valid reserved vehicle - NOT sending OnShipCanDock` | tube class | 175 broadcast, 2,391 no-reserve / 213 | all | Noisy |
| Carriage call / travel | `<SCEvtNotify_BuildingBlocksCallCarriage> [TRANSPORT] Player <player>[<id>] triggered the call carriage event` | own flag | 341 / 50 | all LIVE and HOTFIX builds, PTU 5.0.201 (4) | Medium |
| Carriage ride | `<Player entered carriage>` / `<Player exited carriage> [TRANSPORT] Player <player>[<id>] ...`; `<Carriage state changing> ... from (Travelling) to (Present) for reason (Journey complete)` | own vs other, movement kind | 2,221 / 2,217 / 1,235 lines | all | **95% are other players.** No elevator name or destination. |

Destination codes are internal (`MISSION_QT_<type>_<n>`, station and outpost codes, `PartyMemberMarker_<n>`, `<x>.socpak`). The QT lines carry no display name. The only display names in the log are the mission locations in `<GenerateLocationProperty>` (4.x, see [Missions and contracts](#missions-and-contracts)); its location codes never equal a QT destination code (0 of 155 distinct destinations), so it cannot name QT targets.

### Zone and other HUD toasts

All single-line `<SHUDEvent_OnNotification>` toasts with no mission id.

| Toast | Meaning | Count | Builds |
|---|---|---|---|
| `Entered <Name> Jurisdiction: ` | Jurisdiction change (`UEE`, `Ungoverned`, `Rough & Ready`, `Crusader Industries`, `People's Alliance`, `microTech`, `ArcCorp`, `Hurston Dynamics`, `Klescher Rehabilitation`, ...) | 1,547 / 245 | all, 5.0 included. Implemented in the zone classifier. `Journal Entry Added: Jurisdiction: <Name>` (36 lines) is a different toast. |
| `Entered Monitored Space: ` / `Exited Monitored Space: ` | Comm-array coverage | 766 / 204 and 253 / 97 | all |
| `Entering Private Property: ` / `Leaving Private Property: ` | Private land claim or base | 128 / 59 and 93 / 50 | 12572603 to 5.0.201 |
| `Joined hangar queue.` | Waiting for a hangar | 19 / 17 | all channels |
| `RSI Disc. <Category> Tier <n> Reward`, `Orison Relief Rewards - Tier <n>` | Event reward tier reached | 60 / 9 | 12660092, 12881860 |
| `Downloading` | Data download (mission or event) | 12 / 5 | 12660092 to 12913470 |

## Party, social and session

### HUD notifications (two-line: title ⏎ body)

| Title ⏎ body | Meaning | Count | Builds |
|---|---|---|---|
| `New Member Joined` ⏎ `<player> has joined the party.` | Someone joined your party | 559 party toasts (the title has 663 / 108) | 12545750 to 13220821 |
| `New Member Joined` ⏎ `<player> has joined the channel '<ShipDisplayName> : <player>'.` | Someone joined a ship voice channel you are in | 104 | 12572603 to 13165946; 1 in 5.0.201 |
| `Member Left` ⏎ `<name> has left the party.` | Someone left. The name can contain spaces (25 lines), so do not match it with `\S+`. | 558 party toasts (the title has 653 / 99) | same; 0 in PTU |
| `Member Left` ⏎ `<player> has left the channel '<ShipDisplayName> : <player>'.` | Someone left a ship voice channel | 95 | 12572603 to 13165946 |
| `New Party Leader` ⏎ `<player> is now party leader.` | Leader change | 172 / 76 | same |
| `Party Disbanded` ⏎ `The party has been disbanded.` | Party ended | 15 / 14 | HOTFIX, LIVE |
| `You have joined` ⏎ `<player>` | You joined that leader's party | 97 / 95 | HOTFIX, LIVE |
| `You have created` ⏎ `<player>` | You created a party | 39 / 39 | all channels |
| `You have left the party.` | You left | 7 | LIVE |
| `Party` ⏎ `<player> connected.` / `<player> disconnected.` | Party member presence | 1,081 / 45 | **12660092 only** |
| `Party Launch` ⏎ `Notifications sent to party members.` / `Initiated by party leader <player>.` / `Join queue canceled by party leader <player>.` | Group launch into the PU | 173 / 75 | all channels |
| `Initiated by <player>` ⏎ `Party Launching: Join Party In PU?` | Party-launch prompt: a leader is launching and asks you to join (the title carries the name) | 48 / 19 | 12572603 to 13165946 |
| `Party Launch Accepted` ⏎ `<player>` | | 48 / 12 | LIVE |
| `Friend Added` ⏎ `<player>` | New contact | 241 / 63 | all channels |
| `Invitation Declined` ⏎ ` has declined invitation to group.` / `Invite Expired` ⏎ `The invitation you sent to  has timed out.` | The name in the body is blank | 8 / 6 and 12 / 5 | HOTFIX, LIVE |
| `You have joined channel '<ShipDisplayName> : <player>'.` ⏎ (empty body) | Voice channel of a ship. Owner ≠ local handle means you are crew on someone else's ship. The owner can be empty. | 342 / 147 | all |
| `You have left the channel '<ShipDisplayName> : <player>'.` ⏎ (empty body) | Left it. Note `left the channel` against `joined channel` (no "the"). | 104 / 60 | 12545750 to 13220821; none in 5.0 yet |

### Service lines

| Template | Meaning | Count | Builds |
|---|---|---|---|
| `<party-launch> [notification] party-launch from leader[<player>] : pendingId[<n>] gameModeId[<n>]`; `[auto-accept] ...`; `[player] Accept` | Party launch flow | 57, 64, 11 | HOTFIX, LIVE to 4.10.200 |
| `<Leave group> Client <id> leave group <uuid>` | Left a group | 40 / 21 | 4.10 |
| `<Transfer group leadership> Client <id> transfer leadership to member urn:sc:global:player:<player> in group <uuid>` | Leadership change | 37 / 29 | 4.10 |
| `<Set Member Ready Status> Client <id> set ready status: <n> in group <uuid>` | Ready flag | 157 / 15 | 4.10 |
| `<Get DM group> Getting or creating DirectMessage group with player <id>` | A DM was opened. **No message content.** | 14 / 3 | LIVE |
| `<Get player instance info> Request instance info for player <id>` | Client polls friends' locations. The result is not logged. | 2,390 / 146 | HOTFIX, LIVE; 0 in PTU |
| `<CPartyMarkerComponent RWES> Streamed in party marker id <id>...` | Party markers. **Not equal to party size** (median 4, spikes to 61). | 2,900 | all |
| `<[Instancing] Player entered instance>` / `<[Instancing] Player exited instance> [Instancing][CEntityComponentBrokeredInstance::OnPlayerEnter/ExitInstance] Player <player>[<id>] entered/left instance <InstanceClass>[<id>], which is NOT authoritative` (the tag says `exited`, the text says `left`) | Instanced content. Names other players: counts and instance class only. | 579 / 7 entered, 43 / 7 exited | 12545750, 12660092, 12881860, 13165946. Other `[Instancing]` tags (`GetAvailableInstances Success` 259, `Update instance` 76, `New instance` 8) run to 13220821; PTU 5.0 has untagged-prefix `<GetAvailableInstances Success>` and `<OnInstancedJourneyBegin>` instead. |
| `[EALobby]` `OnLobbyMemberJoined` / `OnLobbyMemberLeft` / `SyncSquadMembers ... was <n>, will become <n>` | Arena Commander lobby | 29 / 55 / 25 | 4.10 |

The group `<uuid>` is the party correlation key. Hash it or keep it internal.

### Session, login and connection

| Template | Meaning | Count | Notes |
|---|---|---|---|
| `<LoginCompleted> LoginCompleted message` | Login done | 282 / 282 | Every build. Median 7.5 s after launch. |
| `<AccountLoginCharacterStatus_Character> Character: ... geid <id> - accountId <id> - name <player> ...` | Character status at login; gives the local player id (F3) | 283 / 283 | Every build |
| `[CSessionManager::OnClientConnected] Connected!` | Client connected to a server (frontend or PU). Untagged. | 1,052 / 281 | Every build. Pairs with `Spawned!`. |
| `[CGlobalGameUI::OpenLoadingScreen] ...` / `[CLoadingScreenComponent::Enter] ...` | Loading screen shown. Untagged. This is the loading-screen line `SESSION_SYSTEM` uses. | 2,244 / 282 and 1,062 / 282 | Every build |
| `<AccountLoginQueueStatus> ... queue name <q> - position <n> - eta (seconds) <n>` | Login queue | 189 / 1 | 4.10.200 |
| `<Join PU> address[<ip>] port[<n>] shard[<shard>] locationId[<id>]` | Joined a PU server | 456 / 240 | Implemented. Shard is `pub_<region>_...` or `ptu_<region>_...`. Strip address and port. |
| `<Join Match> address[<ip>] port[<n>] party[<uuid>] type[<n>]` | Arena Commander or other match | 108 / 14 | |
| `<Update Shard Id> New Shard Id: <shard>. Old Shard Id` | Shard change | 360 | Implemented |
| `<Change Server Start>` / `<Change Server End> IsShardPersisted[<n>] IsServer[<n>] IsMultiplayer[<n>] IsOnline[<n>] ...` | Server hop | 1,057 each | Typically 2 per session |
| `<Context Establisher Done> establisher="<CReplicationModel\|Network\|Game>" runningTime=<n> map="megamap" gamerules="<SC_Frontend\|SC_Default\|EA_*>"` | **Loading finished.** `CReplicationModel` + `SC_Default` means you are in the PU. | 3,226 / 281 (1,047 with `CReplicationModel`) | `runningTime` is the load time. Every line also ends with `sessionId="<uuid>"`: never capture it. |
| `<CContextEstablisherTaskLongWait> ... state=eCVS_<State>(<n>) status="Busy" runningTime=<n>` | Stuck-loading warning | 157 / 12 | |
| `<Channel Disconnected> cause=<n> reason="<reason>" ... gamerules="<rules>"` | **Disconnect reason** | 1,291 | Strip address, session and nickname fields |
| `<Error Popup Opened>` / `<Error Popup Closed> errorCode=<n> [Team_UI][Disconnection]` | Disconnect dialog | 17 / 30 | |
| `<SystemQuit> CSystem::Quit invoked with - cause=<n>, reason=<text>, exitCode=<n>` | Clean exit | 248 / 248 | A file with no `SystemQuit` crashed, was killed, or is still live |
| `Cloud Imperium Games public crash handler taking over...` | **Crash.** Untagged, no timestamp, last line. | 17 files | |
| `<GameTelemetry> Client connected\|disconnected (id=<n> total=<n>)` | A local gRPC telemetry client attached or detached, i.e. this bridge | 37 / 4 | **5.0.202 only** |
| `<Population Manager Stats> NPCs <n>/<n> (atCap=<n>) players=<n> fps=<n>/<n> ...` | Server NPC and player load, server fps | 30 / 11 | **5.0 only** |

**Disconnect reasons:**

| Reason | cause | Count | Class |
|---|---|---|---|
| `Nub destroyed` | 30010 | 971 | teardown (normal) |
| `DisconnectLight` | 30076 | 18 | teardown (PTU replacement for the above) |
| `Remote Disconnect - Player requested disconnect` | 30016 | 220 | user |
| `DisconnectCmd: disconnect light ExitToMenu` / `ReturnToFrontendGameMode` / `ConnectToGroupInvite` | 30016 | 71 | user |
| `Remote Disconnect - player inactive` | 30028 | 6 | AFK kick |
| `Remote Disconnect - Dup Login` | 30015 | 3 | duplicate login |
| `DGS disconnecting all channels before game shutdown` | 30020 | 2 | server shutdown |
| `Remote Disconnect - CNetChannel::InactivityTimerCallback connection has timed out` | n/a | rare | connection lost |

Popup codes seen: 30028 (after an AFK kick), 30000 (after the inactivity timeout), 60028, 60011, 19005, with 60016 and 60024 nearby.

Shard regions seen: `use1b`, `euw1b`, `ape1a`, `apse2a`.

## Economy and inventory

| Tag / template | Meaning | Fields | Count | Builds | Reliability |
|---|---|---|---|---|---|
| `<CEntityComponentShopUIProvider::SendShopBuyRequest> Sending SShopBuyRequest - playerId[<id>] shopId[<id>] shopName[<shop>] kioskId[<id>] client_price[<float>] itemClassGUID[<uuid>] itemName[<item_class>] quantity[<n>]` | Item buy at a shop kiosk | shop, price (aUEC), item, quantity | 197 / 51 | 4.x, 5.0.201 (3) | Always logged on a buy |
| `<CEntityComponentShoppingProvider::SendStandardItemBuyRequest> Sending SShopBuyRequest - ... currencyType[UEC]` | Buy through the newer shopping UI | same, plus currency | 16 / 4 | to 4.10.200 | Sometimes |
| `<CEntityComponentShopUIProvider::SendShopSellRequest> Sending SShopSellRequest - ...` | Item sell | shop, price, item, quantity | 17 / 2 | 4.x | Always logged on a sell |
| `<CEntityComponentShopUIProvider::RmShopFlowResponse> Received ShopFlowResponse - ... shopName[<shop>] ... kioskState[<state>] result[<Success\|InsuffientStock\|InventoryItemFail>] type[<Buying\|Selling>]` | Server result. **No price or item**: join to the request. | result, type, shop | 213 / 52 | all, 5.0.201 included | Always pairs |
| `<CEntityComponentShoppingProvider::RmShopFlowResponse> Shop Flow Response - playerId[<id>] result[<result>]` | Result for the shopping-UI flow | result | 17 / 5 | to 4.10.200 | Sometimes |
| `<CEntityComponentCommodityUIProvider::SendCommodityBuyRequest> Sending SShopCommodityBuyRequest - ... price[<float>] shopPricePerCentiSCU[<float>] resourceGUID[<uuid>] autoLoading[<0/1>] quantity[<n> cSCU] Cargo Box Data: boxSize[<n>] \| unitAmount[<n>]` | Commodity buy | total price, unit price, quantity (cSCU; divide by 100 for SCU), box sizes. **No commodity name.** | 3 / 2 | 4.x | Rare |
| `<CEntityComponentCommodityUIProvider::SendCommoditySellRequest> Sending SShopCommoditySellRequest - ... amount[<float>] ... quantity[...] transactionMode[...]` | Commodity sell | amount, quantity | 9 / 2 | 4.10.197 | Rare |
| `<...RmToken_CommodityTransactionResponse> Commodity Transaction Response Error - ... result[<error>] type[<Selling>]` | Commodity failure. No success line exists. | result, type | 5 / 1 | 4.10.197 | Errors only |
| `<...LoadShopInventoryData...> AddingCommodityBox - ... shopName[<shop>] commodityName[ResourceType.<Name>] Available Box Sizes: boxSize[<n>]...` | Commodity kiosk opened. Lists what the terminal trades, **with names**. | shop, commodity, box sizes | 138 / 5 | to 4.10.200 | Sometimes |
| `<CEntityComponentShoppingProvider::SendRentalRequest> Sending SShopRentalRequest - ... itemName[<ship_class>] offering[<n>] currencyType[UEC]` | Ship rental | ship class, price, offering index | 1 / 1 | 4.10.193 | Rare |
| HUD `Transaction Complete: ` / `Transaction Error: Insufficent Stock` | Purchase toast. Only after the shopping-UI flow (about 12 toasts against about 200 buys). | reason | 13 / 4 and 3 / 2 | 4.x | Not a purchase counter |
| HUD `<player> wants to send you <n> UEC⏎Currency Transfer Request: ` then `<player> has sent you:⏎<n> aUEC⏎ : ` | Player-to-player transfer. **Multi-line; names the sender.** | amount | 6 files each | to 4.10.197 | Always when it happens |
| HUD `Awarded <n> aUEC: ` | Payout toast | amount | 81 / 1 | one event session only | Rare |
| HUD `Fined <n> UEC: ` | Fine | amount | 7 / 7 | to 4.10.201 | High in 4.x |
| `<CWallet::RmMulticastCurrencyBalanceChanged_Legac> [UI] ClCurrencyBalanceChanged(<n>, REC)` | Wallet change. Seen for REC only. The tag is truncated in the log. | currency, value | 3 / 2 | 4.10 | Almost never |
| `<Player Inventory Request Complete> Request[<n>] for '<player>' [<id>] Type[<type>] Result[<Succeed\|Failed>] Item[<id>]` | Inventory operation | type, result | 24,877 / 239 | all | Very noisy |
| `<StoreItem> ... Class[<item_class>] ItemsCount[<n>]`, `<EquipItem> ... Class[<item_class>] Port[<port>]` | Item stored or equipped | item class, port, count | 1,302 / 1,803 lines | all | Noisy |
| `<UnstowPendingEntities> Unstow Request[<n>] for '<player>' [<id>] finalized spawn of '<item_class>_<id>' [<id>], <n> remaining` | Item taken out of inventory into the world (hand, backpack, ground) | item class. **Carries the local handle.** | 1,988 / 123 | all, 4.10.201 and 5.0 included (few lines) | Medium. Noisy in bursts. |
| `<Inventory Quick Equip Request> Player [<player>] PIT quick equip event sent for item at [<path>] [<n>]` | Quick-equip from the inner-thought wheel | none useful. **Carries the local handle.** | 1,612 / 103 | all | Low |
| `<AttachmentReceived> Player[<player>] Attachment[<item_class>_<id>, <item_class>, <id>] Status[<s>] Port[<port>] Elapsed[<f>]` | Item attached to your character's ports (every line whose file has a known handle names the local player) | item class, port. **Carries the local handle.** | 79,205 / 280 | all | Noise, except the `uba_prison_` form |
| `<Show Location Inventory> ... location [<loc_code>]` | Readable inventory location code (city, station) | location code | with 5,177 `Update Inventory Location` lines | all | Medium. A second "where am I" source. |
| `<CEntityComponentFreightElevatorUIProvider::FillUnstowRequest> [FreightElevatorKioskUIProvider] <kiosk_class> - Processed bindings into transfer request - Entities: <n>, Location: <id> - RequestId: <n>` | Freight elevator transfer | kiosk class, entity count | 37 / 12 | to 4.10.200 | Sometimes |
| `<ShopBackend> Shop backend resolved - using [Legacy]` | Which shop backend is active | backend | 6 / 6 | **5.0.201 only** | Once per session |
| `Entity : 'MineableRock_<Type>_<Mineral>_<id> - Class(...)'` | Mineable rock streamed in (VFX debug). **Not a yield.** | mineral | 6,414 / 72 | LIVE | Noisy |

The game's own misspellings (`InsuffientStock`, `Insufficent Stock`, `vehicules`, `occured`) must be matched literally.

## Lifecycles

Times are medians, with p10 to p90 in brackets where measured. Lines on one row share a timestamp.

### Mission

```
<MissionShared>                      (party only)
  │ ~3.6 s
Contract Accepted: <title>           (toast, real mission_id)
  │
CreateMarker + UpdateActiveObjective (contract_def, objective text; phase_ + real objectives)
  │ 72 s (0.8–894 s)
ObjectiveUpserted INPROGRESS created=1, then created=0 updates
New Objective                        (toast)
  │
ObjectiveUpserted COMPLETED  |  ObjectiveComplete WITHDRAWN + Objective Withdrawn toast
  │ 281 s from accept (25–1,661 s)
EndMission Complete = MissionEnded COMPLETED = Contract Complete: <title>
  │
Received Blueprint × 3–9             (4.10.196–200 only)
```

| Ending | Sequence | Count | From accept |
|---|---|---|---|
| Complete | `EndMission` Complete → `MissionEnded` COMPLETED → `Contract Complete` | 617 | 281 s |
| Fail | `EndMission` Fail → `MissionEnded` FAILED → `Contract Failed` | 80 | 149 s |
| Abandon or leave server | `EndMission` Abandon / "Player left" only. **No `MissionEnded`, no toast.** | 98 | 1,290 s |
| Withdrawn or expired | `MissionEnded` WITHDRAWN / EXPIRED + `EndMission` Deactivate + `Contract Withdrawn` | about 20 | 6,425 s (max exactly 14,400 s, which suggests a 4 h expiry) |
| Shared, never accepted | `MissionShared` only: a party member's mission | 396 ids | |
| No end in the file | Session ended mid-mission. It can resume in the next log under the same mission_id. | about 130 | |

`EndMission` comes before `MissionEnded` in about 95% of cases, 0 s apart.

### Death and respawn

```
4.10.200+, in a ship:
  FatalCollision (optional, rare; seen up to 4.10.201 and PTU 5.0.201)
  Vehicle Destroyed  level 0→1 (soft) ... 1→2 (hard), or 0→2
    │ ~0.1 s
  [ActorState] Dead ... ejected from zone   (if you were aboard)
  Vehicle Control Flow ClearDriver
  Standby, Local Emergency Services Are En Route   (4.x only)
    │
  corpse burst: Adding non kept item × ~34
    │ 22.6 s after the Emergency toast (p90 27 s)
  [CSessionManager::OnClientSpawned] Spawned!

PTU 5.0, on foot:
  corpse burst  →  ~42 s  →  Spawned!       (no Dead line, no Emergency toast)
```

- ~~Count deaths from the corpse burst.~~ **Corrected 2026-11-21:** the burst has no owner and also fires for other players' corpses near you (a kill you made). Count your own deaths from the telemetry `GAME_EVENT_TYPE_ACTOR_DEAD` event (and `ACTOR_DOWNED`). In Game.log alone, require the burst to be followed by your own `Spawned!` (respawn) to treat it as yours.
- **Prison:** crime → `CrimeStat Rating Increased` → death → `Spawned!` (about 10 s) → in the same second: three `uba_prison_*` attachments, a `PrisonMine_*` inventory location and another `CrimeStat Rating Increased`, plus an `Entered Klescher Rehabilitation Jurisdiction` toast (4.10.193 to 4.10.201; its exact position in the sequence was not measured).
- **Crime:** `Crime Committed` → `CrimeStat Rating Increased` (about 13 ms) → optionally `Fined <n> UEC`.
- **Injury:** `Injury Detected` toasts → later `MED BED HEAL` with the healed-part booleans (4.10.197 and earlier).

### Ship retrieval (ASOP)

```
OnRequestFetchVehicles
  │ 0.5 s (0.3–0.9 s)
VehicleListQuery ... Retrieved <n> entitlements
  │
SetVehicleSpawningInformations       (landing area: personal hangar | docking port)
  │ 97 s (49–207 s)
Hangar Request Completed             (toast)
  │
Lower/RaiseLoadingPlatform
  ...
ClearDriver (leave seat)  →  StowingVehicle (on hangar unregister)
```

Correlation is by order and time only. In 5.0, `Claim request sent` → `RmTokenResponseClaim` took 1.2 s (one sample), joined by the request uuid.

### Quantum travel

```
Player Selected Quantum Target = Calculate Route (route + fuel estimate, start location)
  │ optional: Player Requested Fuel
Quantum Travel Calibration Started By <own>
  │ 2.5 s (2.5–3.0 s)
Quantum Travel Calibration Complete
  │
Quantum Drive Arrived                (same ship entity id)

select → arrived: 77 s (26–414 s), n=733
```

Join on the ship entity id inside `<ship_class>_<id>[<n>]`. The calibration toasts have no id: join them by time window and an own-name check. There is no abort line, so a select with no arrival ends on a timeout or the next select.

### Session

```
header (Build, Branch) → InitiateLogin → login response (handle) → LoginCompleted (~7.5 s)
→ Change Server Start/End → Context Establisher Done gamerules="SC_Frontend"
→ Join PU shard[...] → Change Server Start
→ Context Establisher Done establisher="CReplicationModel" gamerules="SC_Default"   (in the PU)
→ Channel Disconnected reason=... (+ Error Popup Opened) → frontend → SystemQuit
```

- **Load time** from `Join PU`: median 36 s (p25 31 s, p90 48 s, max 199 s).
- **Endings:** `User closed the application` 238, `Quit via console command` 10, no `SystemQuit` 44 (17 with the crash-handler line; the rest killed, hung or still live).

### Shop

`SendShopBuyRequest` → `RmShopFlowResponse` (join on shop name; one request outstanding per kiosk). Commodity trades have no success line: treat "no error within about 10 s" as success.

## Format drift

### LIVE 4.x compared with PTU 5.0

| Area | LIVE 4.10 | PTU 5.0 |
|---|---|---|
| Login handle | `<Legacy login response>`, `CDiffusionCryClient::*`, `Legacy login queue status` | `<CLoginSession>`, `<CStarNetwork>`, `CHubClient`, `RmTokenResponseFetchServerInfos` |
| Frontend teardown | `Nub destroyed` cause 30010 | `DisconnectLight` cause 30076 |
| Shard prefix | `pub_` | `ptu_` |
| Friend location polling | `<Get player instance info>` | gone (0 lines) |
| Insurance | `<Claim Failed>` only | `Claim request sent`, `RmTokenResponseClaim`, `Deliver request sent`, `Vehicle info request sent` |
| Shops | two stacks (`ShopUIProvider`, `ShoppingProvider`) | `ShopUIProvider` only, plus `<ShopBackend> ... [Legacy]`. Expect new templates. |
| Server stats | none | `<Population Manager Stats>`, `<GameTelemetry> Client connected` |
| QT | | new `QT arrive outside zone`, `CMapDisplayStarMap::PopulateQuantumPath/CancelQuantumRoute` |
| Death | `Emergency Services` toast | not seen (one death without it) |
| Mission lines | all tags | 13189690: identical formats (one `CommsNotifications` send line). 13248297: **zero mission lines**, so no evidence. `<GenerateLocationProperty>` is absent from both 5.0 builds. |
| PTU-only tags | | `<Carryable PickUp Request>` 16 / 3, `<InstanceEligibilityIgnoreRequested>` 13 / 12, `<CEntityComponentMissionBoardUIProvider::GetUserPtr>` 12 / 2, `<OnInstancedJourneyBegin>` 5 / 2 |
| Not seen in 5.0 (may be inactivity) | Crime, CrimeStat, Fined, MED BED HEAL, prison, restricted area, party notifications, blueprint toasts, `Boundary Violation`, `Cannot Align`, `CheckAutoDockingRequirmentsMet` | |

### Changes inside 4.10

- **Added in 4.10.200:** `<Vehicle Destroyed>`. Unchanged in 5.0.
- **Gone after 4.10.197:** `<Boundary Violation>`, `<MED BED HEAL>`, the hauling marker. (`<FatalCollision>` is not gone: 1 line in 4.10.200, 3 in 4.10.201, 1 in PTU 5.0.201.)
- **Gone after 4.10.200** (absent in 4.10.201 and 5.0): `<ObjectiveComplete>`, `PlayerJoined` / `PlayerLeft`, `CObjectiveMarkerComponent::UpdateInPlayerDataBank`, `AudioCommsSignal::PlayCommsNotification`, `GetGridItem`, `Inventory Quick Equip Event`, `[ActorState] Place`, `[Instancing] Player entered/exited instance`, `party-launch`, `Accept invitation`, `<CommsNotifications>`, `<SHUDEvent_OnCommsNotification>`, `<SpawnCommsEntity>`, member voice-channel toasts. `<MissionShared>` is also unseen in 4.10.201. (`CommsNotifications`, the comms-call tags and one member voice-channel toast reappear for a few lines in PTU 5.0.)
- **Gone after 4.10.201** (absent in 5.0 so far): `<GenerateLocationProperty>`, `Entered Klescher Rehabilitation Jurisdiction`.
- **4.10.201 already logs like 5.0.** A parser should treat every tag as optional and never depend on one being present.
- **Build 12660092 only:** `Party ⏎ <player> connected/disconnected.` presence toasts.
- **Build 12881860 only:** untagged HUD queue dumps.
- **Stable in every build:** the `SHUDEvent_OnNotification` structure, the ItemNavigation pipe format, `ClearDriver`, carriage lines, `Join PU`, `Change Server *`, `Context Establisher Done`, `Channel Disconnected`, `SystemQuit`, `LoginCompleted`, `AddAccessibleDestinations`, armistice and jurisdiction toasts, corpse lines, `Spawned!`, own voice-channel join text. (Injury toasts are absent from 4.10.200 and 4.10.201.)

## Noise to drop early

Tagged noise is about 29% of lines; untagged lines are another 64%.

- **Untagged:** `Streamable ...` asset loads, `Effect: '...'` particle continuations (the `XT_Damage` effect names are **not** the Xenothreat event), indented `ID:` / `Class:` / `Manager` blocks.
- **High-volume tags not covered elsewhere** (recount, lines containing the text): `Failed to get starmap route data!` (16.3k, ItemNavigation format with a ship class), `[Particle System] Kidnapping Child Effect` (15.8k), `Adding Instanced Destinations To Panel` (12.1k), `RegisterUniverseHierarchy_*` (11.1k), `OnDragInventoryItemModifyTarget` (11.3k), `DuplicatedItemPorts` (10.3k), `ReuseChannel` (8.2k), `[BuildingBlocks] Invalid Url` (7.1k), `CAttachableComponent::GetPortFromId` (6.9k), `Too many actions` (6.3k), `CreationRefusedEntity` (5.5k), `Expire Local Attachment` (4.5k), `WebRTC/Janus` voice errors (3.9k), `Stream started` (3.6k), `ProximitySensorMakingLocalHelper` (2.7k), `CDockingAnimatorComponent::OnSetCurrentState` (2.5k), and the untagged `[SalvageableRepairable_*] DAMAGE_MAP_SNAPSHOT_FILE` lines (4.5k; not a salvage signal).
- **Rendering:** `CPU Particle Limit reached` (306k), `Particle Emitter Rendered from multiple views!`, `StatObjLoad`, `InvalidateAllTerrainCells`, `Zone Host Missing Surface Raindrops`.
- **Loading plumbing:** `InitializeSlotMetadata_*`, `Player-HasFinalizeDependencies-*`, `Planet *` load lines. (`Context Establisher Done` and `CContextEstablisherTaskLongWait` are signals; the other `ContextEstablisher*` lines are noise.)
- **Inventory plumbing:** `Query Inventory`, `Inventory Mgmt*`, `Request Personal Inventory Data`, `Inventory Token Flow`, `Update Inventory Location`, `AttachmentReceived` (except the `uba_prison_` form), `Inventory Quick Equip Request`, `UnstowPendingEntities` unless a parser needs it.
- **Vehicle plumbing:** `ATC_Reset_VehiclesBeingStoredCleared` (58k), `CSCLoadingPlatformManager::*`, `AggregateBindParentOutOfSync`, `Gateway Schedule Unretrievable`, `Cannot Align - No Jump Point` / `No Vehicle` (a bug-spam artefact, not a jump event), `Missing QT Routing Data`, `CheckAutoDockingRequirmentsMet`, the `no valid reserved vehicle` docking-tube form.
- **Mission plumbing:** `CObjectiveMarkerComponent::Add/RemoveFromPlayerDataBank`, `MissionSharedOnMessaged`, `UpdateNotificationItem`, `UpdateHUDNotifications`, `CommsNotification ... skipped`, `SpawnCommsEntity`, `LocalPlayerNotifications::StatusUpdate`, `Comms Notification Flow`.
- **Social plumbing:** `SubscribeToPlayerSocial`, `SubscribeToFriendMessages`, `NotifyPendingInvitations`, `Update group cache *`, `CPartyMarkerComponent`.
- **Misleading names:** `Server Rerouted` and `Server Reset Blocked` are quantum-routing lines, not server recovery. `Connection Flow` is ship comms, not networking.
- **Skip for privacy:** `SetSalvageRepairAmmoCount_NoTarget`, `Trying to QT when not in Idle` and `Too many actions` (`... on entity '<player>'`: 3,630 of 6,310 lines name another player) carry other players' names.
- **Other:** `No Manufacturer`, `Invalid path`, `Failed attachment`, and the EAC, seeding, gRPC and login plumbing.

## Gaps: what the log cannot tell you

| Area | Not in the log |
|---|---|
| Combat | FPS kills and deaths (who, weapon, damage type); damage dealt or taken; shield, hull or component health; downed state, bleed-out, revive, medpen use |
| Law | CrimeStat level, bounty amount, sentence length, impound executed |
| Targets | Locked or selected target, contacts, radar, ping and scan results; owner, distance or hostility of nearby ships |
| Vehicles | Seat-enter event; engine, power or gear state; fuel levels; refuel, repair or rearm; ATC request and grant; pad or hangar number; claim timers |
| Travel | QT spool progress, abort, interdiction; display names for QT destination codes (only mission locations get display names, via `GenerateLocationProperty` in 4.x, and its codes do not match QT codes); **jump-point transit or system-change event** (infer from the route start location and jurisdiction toasts); elevator or tram names and destinations |
| Missions | Payout and split; reputation gained; time limit; failure reason; progress counts; target or cargo detail; which party member finished an objective. Giver, organisation and mission type **are** logged by `<CommsNotifications>` in 12545750 to 13165946 (plus one line in 13189690), not in 13220821 or 13248297. Location display names **are** logged by `<GenerateLocationProperty>` in 12545750 to 13220821, not in PTU 5.0. |
| Economy | aUEC wallet balance or delta; commodity names on trade lines; refinery jobs; crafting; mining yield; salvage amounts; cargo manifest; stock levels |
| Social | Chat text; friend presence; a party roster snapshot (it must be built from join and leave toasts); invites received; member location or status |
| Server | Ping, latency, player count (4.x); server recovery or reconnect events; crash reason |
| World | Weather and hazards; server-event state (only instance names and contract or journal titles hint at it); achievements; org data |

## Privacy map

Every field below must be reduced or dropped inside the tailer before anything reaches the snapshot, SSE, events or `--record`.

| Data | Where it appears | Handling |
|---|---|---|
| Local handle and player id | `Legacy login response`, `CLoginSession`, `AddAccessibleDestinations` (`player [<player>]`), `AccountLoginCharacterStatus_Character` (`geid`, `accountId`, `name`), `ClearDriver`, `owner player [...]`, `EndMission` (`Player[...] PlayerId[...]`), `CommsNotifications` (`Player: <player>[<id>]`, both the send and the `skipped` forms), `SHUDEvent_OnCommsNotification` and `SpawnCommsEntity` (leading `<player>[<id>]`), `LocalPlayerNotifications::StatusUpdate` (`CommunicationFinished[<player>]`), `UnstowPendingEntities` and `Player Inventory Request Complete` (`for '<player>' [<id>]`), `Inventory Quick Equip Request` (`Player [<player>]`), every `AttachmentReceived Player[<player>]` line (79k, not only the prison form), `Channel Disconnected` (`nickname=`, `playerGEID=`), the `SetVehicleSpawningInformations` hangar label (`<player>'s ...`; all 245 lines name the local player), `MED BED HEAL` `Actor:`, `[ActorState] Dead` / `Place` actor, `VehicleListQuery` player id | Memory only, for own-vs-other tests. Never emit. |
| Other players' handles | `Vehicle Destroyed` killer name and id, and the victim as `owner player [<id>]`; crime victim and attacker toasts; party and friend toasts (including the body-only `Initiated by <player>`); party-launch `leader[<player>]`; `Transfer group leadership ... player:<player>`; voice-channel owner in `You have joined/left channel '<ship> : <player>'` and member voice-channel toasts; QT calibration `Started/Complete By <player>` and `<player> has started to QT to your current location`; `Too many actions ... on entity '<player>'` (both own and other); `Trying to QT when not in Idle` `User:` / `Client:`; carriage lines; `[Instancing]`; objective-marker DataBank lines; `PlayerJoined` / `PlayerLeft` and `Get player instance info` player ids; lobby lines; status-effect `nickname:`; currency transfers | Reduce to `self` / `other` / `npc` / `none`, or to a count. An opt-in local-only roster is the one possible exception. |
| Entity ids, mission, objective and group uuids | nearly every line | Internal join keys. Never emit; hash if one must cross a boundary. |
| Server address, port, session ids | `Join PU`, `Join Match` (`party[<uuid>]`), `Channel Disconnected` (`remoteAddr`, `localAddr`, `session`, `node_id`), `Context Establisher Done` (`sessionId="<uuid>"` on every line) | Already confined to `/api/session/private`. The planned `SESSION_READY` regex must not capture `sessionId`. |
| File header | `Executable:` (install path, can contain the OS username), `Host CPU:` | Read `Build` and `Branch` only; never emit or record the header. |
| Coordinates | `FatalCollision` (`Pos:`), `Boundary Violation` (`pos` and `vel`), objective marker DataBank lines (`ZonePos`), QT-not-idle | Drop. |
| Shop, kiosk and item GUIDs | shop lines | Drop after the join. |

## Open questions

The research passes disagreed on a few points. Check these against the corpus before relying on them.

- **Emergency Services timing.** One pass found the toast at 0 s from `[ActorState] Dead` (10 of 12 cases); another measured a median of 11 s after `Vehicle Destroyed`. Do not use a tight window until this is re-measured.
- **Shop response delay.** Measured as a 3.6 s median by one pass and "usually under 1 s" by another.
- **Session length.** 40 min median (first to last timestamp, LIVE) in one pass; 1.2 h median in another, with a different file set.
- **`FatalCollision` file count.** Resolved by the recount: 55 lines in 27 files, builds 12572603 to 13220821 plus one PTU 5.0.201 line.
- **`Vehicle Destroyed` by branch.** One pass grouped files by branch name and reported it absent from the "4.10.2" branch files; by FileVersion it starts at 4.10.200. Both describe the same builds.
- **5.0 removals.** Every "not seen in 5.0" entry needs a re-scan after more PTU play, especially crime, prison, party and mission-share lines.
- **Objective text join.** Joining `UpdateActiveObjective id=` to `ObjectiveUpserted objective_id` was not verified over the whole corpus.
