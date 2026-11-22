---
title: Game.log parser plan
tags: [docs, telemetry]
updated: 2026-11-21
---

# Game.log parser plan

**Status:** plan only, written 2026-11-21. Nothing below is implemented unless it is listed under "Already in the tailer".

**What this note is:** the ranked order in which to add parsers to the bridge's Game.log tailer (`scgameapi/gamelog.py`), with a regex sketch, the fields, the privacy handling, the value to the player, the effort and the dependencies for each. The lines themselves are described in [gamelog-catalog.md](gamelog-catalog.md).

**Regexes are sketches.** They were written from templates and counts, then run once against the 293-file corpus (2026-11-21); the match counts are under [Regex check against the corpus](#regex-check-against-the-corpus). They are not yet in the tailer's test suite. Each needs synthetic-line tests (fake ids, fake handles) before it ships, as `tests/test_signals.py` does today.

## Already in the tailer

| Event | Source |
|---|---|
| `SESSION_JOIN`, `SESSION_SHARD_CHANGE`, `SESSION_SYSTEM`, `SESSION_LEAVE` | header lines, `Join PU`, `Update Shard Id`, loading-screen line |
| `NEARBY_SHIP` | `Local Route Guard ... PostInitialize` |
| `QT_ARRIVED {class, self}` | `Quantum Drive Arrived` |
| `ZONE {kind, text}` | single-line HUD zone notices |
| `VEHICLE_RELEASED {class}` | `ClearDriver` |
| `CORPSE_ITEMS {count}` | corpse item lines, 5 s window |

See [gamelog-signals.md](gamelog-signals.md) for the details.

## Foundations (do these first)

Every pass-2 parser depends on at least one of these. They emit nothing by themselves.

### F1. Line pre-filter and SPAM prefix

```python
LINE = r"^<(?P<ts>[^>]+)> (?:\[SPAM (?P<rep>\d+)\])?(?:\[(?P<lvl>Notice|Error|Critical)\] )?<(?P<tag>[^<>]{1,120})> ?(?P<msg>.*)$"
```

- Dispatch on `tag` through an allow-list dictionary instead of trying each rule's substring in turn.
- Keep `rep` as a repeat count. A `[SPAM <n>]` line is one aggregated event, not `<n>` events.
- Tagged lines only carry `Notice`, `Error` or (4 lines) `Critical`; `Warn` and `Trace` never occur on tagged lines. The only tagged-looking lines `LINE` drops are `[flow] <noname> : ...` (1,136, flowgraph noise), which is intended.
- `LINE` runs on physical lines, before F2 joins a toast (`.` does not cross the newline).
- The current rules use `search` on the body, so a SPAM prefix does not break them today. The change is about speed and about new rules that anchor on the level.
- **Effort:** small.

### F2. Notification joiner

The current rule (`Added notification "(?P<text>[^"]*)"`) needs the closing quote on the same line, so it drops every multi-line toast: crime, party, currency transfer, restricted-area relocation and about 380 `Objective Complete` toasts.

- Start a buffer on `<SHUDEvent_OnNotification> Added notification "` when the line has no `" [<n>] to queue`.
- Append following lines that have a timestamp but no `[Level] <Tag>`, until the buffer matches `" \[\d+\] to queue`.
- Give up after 5 lines or when a tagged line arrives, and emit nothing for that toast.
- Output one record: `{text (newlines → single space or kept as a separator), seq, mission_id, objective_id}`.
- Reject a body that contains characters outside printable text (a few are corrupted in the log).
- Ignore `UpdateNotificationItem` and untagged queue-dump lines, so each toast counts once.
- The tailer is partial-line safe already; the joiner must also survive a poll boundary between the two lines.
- **Effort:** medium. This is the highest-leverage change in the plan.

### F3. Local identity

```python
r"<Legacy login response> \[CIG-net\] User Login Success - Handle\[(?P<handle>[^\]]+)\]"
r"<CLoginSession> .*Fully authenticated : handle\[(?P<handle>[^\]]+)\]"          # PTU 5.0
r"<CEntityComponentFrontendPUUIProvider::AddAccessibleDestinations> \[\d+\] destinations received for player \[(?P<handle>[^\]]+)\]"   # every build
r"<AccountLoginCharacterStatus_Character> Character: .*?geid (?P<geid>\d+)"       # line also has accountId and name: never capture
r"ClearDriver: Local client node \[(?P<geid>\d+)\]"                               # same value, fallback
```

- `AddAccessibleDestinations` is the only handle source seen in every build (1,122 / 280, 5.0 included). Use it as the fallback when neither login line is present.
- Hold `handle` and `geid` in a private object that has no `public()` method. They are used for equality tests only.
- Reset per file and on `Log started`: several accounts can share one install.
- When identity is unknown, own-vs-other fields are `null`, not guessed.
- **Effort:** small. Add a test that neither value can appear in the snapshot, SSE, `/api/signals` or `--record`.

## Pass 2: high value and robust

Ranked. "Robust" means seen in every build family, or cheap to make optional.

### 1. Ship destroyed and kill feed: `SHIP_DESTROYED`

```python
r"<Vehicle Destroyed> Vehicle '(?P<veh>[\w-]+?)_(?P<vid>\d+)' \[\d+\] class '(?P<cls>[\w-]+)' "
r"\(owner player \[(?P<owner>\d+)\], driver \[\d+\]\) destroyed: reason \[(?P<reason>\w+)\] "
r"level \[(?P<l0>-?\d+)\] -> \[(?P<l1>-?\d+)\] flags \[\d+\] killer '(?P<killer>[^']*)' \[(?P<kid>\d+)\]"
```

| | |
|---|---|
| **Fields** | `{class, reason, soft: l1 == 1, mine: owner == geid, by_me: killer == handle, killer_kind: self \| player \| npc \| none}` |
| **Privacy** | The killer handle, owner id, vehicle id and killer id are compared and dropped. Only `killer_kind` leaves the tailer. `npc` is decided by the class-name shape of the killer string, never by listing it. |
| **Value** | Very high. A kill and loss feed ("you destroyed a `<class>`", "your ship was destroyed: Collision"), which the telemetry API cannot give. Soft death (0→1) is a "ship disabled" alert. |
| **Effort** | Small. |
| **Depends on** | F3. |
| **Builds** | 4.10.200 onward and 5.0; absent earlier. The line covers every vehicle in the streaming bubble, so filter to `mine or by_me` for alerts and keep the rest as a low-priority "nearby destruction" count. |

A `1→2` line for a vehicle id already seen at `0→1` is the same ship: update the event, do not double count. Keep the vehicle id in the same internal table as nearby entities.

### 2. Own death and respawn: `DIED`, `RESPAWNED`

```python
r"CSCActorCorpseUtils::PopulateItemPortForItemRecoveryEntitlement\]> Item '\S+ - Class\((?P<cls>[^)]+)\).*?Port Name '(?P<port>[^']+)'"
r"\[CSessionManager::OnClientSpawned\] Spawned!"
r'Added notification "Standby, Local Emergency Services Are En Route'        # 4.x confirmation only
r"<\[ActorState\] Dead> .*?Actor '(?P<actor>[^']+)' .*?ejected from zone '(?P<ship>[\w-]+?)_\d+'"
```

| | |
|---|---|
| **Fields** | `DIED {items: n, loadout: {port: item_class}, in_ship: class \| null, cause: ship_destroyed \| unknown}`; `RESPAWNED {after_s}` |
| **Privacy** | Item entity ids and class GUIDs dropped. `actor` compared to the handle, then dropped. |
| **Value** | High. A death counter and "what you lost" list that works in every build. With parser 1 it explains ship deaths. |
| **Effort** | Small to medium. It extends the existing `CORPSE_ITEMS` burst logic. |
| **Depends on** | F3 for the `Dead` line; none for the burst. |
| **Caveat** | The corpse burst has no owner and fires for nearby corpses of other players too (verified 2026-11-21). Treat a burst as your death only if your own `Spawned!` follows, or use telemetry `ACTOR_DEAD`. |
| **Builds** | Corpse burst and `Spawned!`: all. `Dead`: 4.x and 5.0.201. Emergency toast: 4.x only. |

- A burst needs `Body_ItemPort` and `mobiglas_attach` to count as a death.
- The first `Spawned!` in a file is the login, not a respawn. A `Spawned!` within 60 s after a burst is a respawn.
- Whether to expose the loadout (it is the player's own gear, class names only) is a product decision: default to the count, loadout behind a flag.

### 3. Contract lifecycle: `CONTRACT`

```python
r'Added notification "(?P<kind>Contract (?:Accepted|Complete|Failed|Shared|Available|Withdrawn|Not Accepted)):\s+(?P<title>.*?): " \[\d+\] to queue\. New queue size: \d+, MissionId: \[(?P<mid>[0-9a-f-]{36})\]'
r"<EndMission> Ending mission for player\. MissionId\[(?P<mid>[0-9a-f-]{36})\] Player\[[^\]]*\] PlayerId\[\d+\] CompletionType\[(?P<type>\w+)\] Reason\[(?P<reason>[^\]]*)\]"
r"<MissionEnded> Received MissionEnded push message for: mission_id (?P<mid>[0-9a-f-]{36}) - mission_state MISSION_STATE_(?P<state>\w+)"
# mission type, organisation and giver (12545750 to 13165946, one line in 13189690):
r"<CommsNotifications> SendCommsNotification \+(?P<add>\S+)(?: -(?P<rm>\S+))? (?:- )?\[[0-9a-f-]{36}\] - Mission: \[(?P<mid>[0-9a-f-]{36})\]"
# applied to add:
r"Missions\.Organization\.(?P<org>\w+)\.(?P<giver>\w+)"
r"Missions\.MissionType\.(?P<type>\w+)"                                        # drop Generic
```

| | |
|---|---|
| **Fields** | `CONTRACT {state: accepted \| complete \| failed \| abandoned \| withdrawn \| expired \| limit_exceeded, title, rep_hint, blueprint: bool, duration_s, type, org, giver}` and an `active_contracts` list in the snapshot. `type`, `org` and `giver` are `null` when no `CommsNotifications` line arrived. |
| **Privacy** | `mission_id` is an internal key (a small per-session index is emitted in its place). The `Player[...]` group in `EndMission` and the trailing `Player: <player>[<id>]` in `CommsNotifications` are never captured. Org and giver are game data (`BountyHunterGuild`, `FoxwellEnforcement`, NPC giver codenames), not player names. |
| **Value** | High. Active contract list, completion and failure alerts, time per contract, session tally. |
| **Effort** | Medium. |
| **Depends on** | F2. |
| **Builds** | All, 5.0.201 included. |

- Title clean-up: `tags = re.findall(r"\[([^\]]+)\]", title)`; strip `</?EM\d>`; `rep = re.search(r"([\d/–-]+) Rep", ...)`; `blueprint = "BP" in tags`.
- `Contract Shared` has an all-zero mission id: pair it with `<MissionShared>` at the same timestamp, or drop it.
- `CommsNotifications` joins on `Mission: [<uuid>]` (443 of 443 distinct ids matched another mission line in the same file). Ignore the `CommsNotification [<uuid>] skipped - Mission ... not tracked` form (461 lines). Mission types seen: DefendShip, Bounty, Patrol_Mission, Refueling, RBM, Events, FacilityDelving, Hauling, DefendItems; `Generic` is a filler tag. The line is missing from 4.10.201 and 5.0.202, so fall back to the contract-definition category from parser 4.
- Dedupe ends per mission id. `EndMission` Abandon has no toast and no `MissionEnded`; it is the only source for "abandoned".
- A mission can resume in the next log under the same id, so an "accepted" with no end is `unknown`, not `failed`, at reset.

### 4. Objective tracker and mission type: `OBJECTIVE`

```python
OID = r"(?P<oid>(?:phase_|pickup_|dropoff_)?[0-9a-f-]{36}(?:_\d+)?)"
r"<Objective(?:Upserted|Complete)> Received Objective\w+ push message for: mission_id (?P<mid>[0-9a-f-]{36}) - objective_id " + OID + r" - state MISSION_OBJECTIVE_STATE_(?P<state>\w+)(?: - created (?P<created>\d) - flags=(?P<flags>[\w|]*))?"
r"<CMissionLogEntry::UpdateActiveObjective> Objective updated id=" + OID + r", flags=(?P<flags>[\w|]*), hidden=(?P<h>\d), hiddenInUI=(?P<hu>\d), markerHidden=\d, uiDisplay\[Priority=-?\d+\]\[Text=(?P<text>[^\]]*)\]"
r"<(?:CLocalMissionPhaseMarker::CreateMarker|SMarkerHandler_Base::CreateMissionObjectiveMarker)> Creating objective marker: missionId \[(?P<mid>[0-9a-f-]{36})\], generator name \[(?P<gen>[^\]]*)\], contract \[(?P<cdef>[^\]]*)\]"
# location display names per contract definition (12545750 to 13220821; absent in PTU 5.0):
r"<GenerateLocationProperty> Generated Locations - variablename: (?P<var>[^,]+), locations: (?P<locs>.*?) contract: (?P<cdef>\S+)"
LOC = r"\((?P<name>[^\[\]]+?) \[\d+\] \[(?P<code>[^\]]+)\]\)"                        # findall over locs
```

| | |
|---|---|
| **Fields** | `OBJECTIVE {contract: index, text, state}`; on the contract: `{generator, contract_def, category, locations: [{role, name}]}` |
| **Privacy** | Mission and objective ids internal. Marker entity ids and zone host ids not captured. Location entity ids in `GenerateLocationProperty` are not captured; names and codes are game data. |
| **Value** | High. A live objective list for the active contract and a mission category (bounty, haul, salvage, defend, recovery) from the contract definition name. |
| **Effort** | Medium. |
| **Depends on** | Parser 3. F1 matters here (13.6k and 12.1k lines). |
| **Builds** | 4.x and 5.0.201. `ObjectiveComplete` is gone after 4.10.200; the regex treats it as optional. |

- Skip `phase_` ids and any objective whose flags contain `Hidden` for the UI.
- Ignore `Text=<= UNINITIALIZED =>`. Progress counts are never filled in (`%ls`).
- Normalise `cdef` with `re.sub(r"_\d+$", "", cdef)`; map to a category with a small keyword table.
- The text-to-state join on objective id is unverified corpus-wide. Test it on recorded sessions first.
- `GenerateLocationProperty` has no mission id, and the join is weak: only 29 of 1,017 distinct normalised contract definitions in it also appear in a `CreateMarker` line of the same file (the generator runs for offers, not only accepted contracts). Attach locations to a contract only when the normalised `contract_def` matches exactly, and otherwise leave `locations` empty. Its location codes are not QT destination codes (0 of 155 distinct QT destinations matched), so it cannot name QT targets. `var` gives the role: `SubLocationType_BP` (4,880), `DropoffLocation_BP` (1,614), `LocationsToScan_BP` (387), `DerelictShips_BP` (77). `locs` can be `INVALID` or empty, and a name can contain `~mission(...)`.

### 5. Session state machine and disconnect classifier: `SESSION_READY`, `DISCONNECT`, `SESSION_END`

```python
r'<Context Establisher Done> establisher="CReplicationModel" runningTime=(?P<secs>[\d.]+) .*gamerules="(?P<rules>\w+)"'
r'<Channel Disconnected> cause=(?P<cause>\d+) reason="(?P<reason>[^"]*)".*?gamerules="(?P<rules>\w+)"'
r"<Error Popup Opened> errorCode=(?P<code>\d+)"
r"<SystemQuit> CSystem::Quit invoked with - cause=(?P<cause>\d+), reason=(?P<reason>[^,]+), exitCode=(?P<code>-?\d+)"
r"^Cloud Imperium Games public crash handler taking over"
r"<AccountLoginQueueStatus> .*?position (?P<pos>\d+) - eta \(seconds\) (?P<eta>\d+)"
```

| | |
|---|---|
| **Fields** | `SESSION_READY {mode: pu \| frontend \| arena, load_s}`; `DISCONNECT {kind: user \| afk \| dup_login \| timeout \| teardown \| server_shutdown, code}`; `SESSION_END {how: quit \| crash}`; `LOGIN_QUEUE {position, eta_s}` |
| **Privacy** | Address, port, session, node id, nickname and `playerGEID` fields in `Channel Disconnected` are never captured. Every `Context Establisher Done` line ends with `sessionId="<uuid>"`; the regex stops at `gamerules` and must stay that way. |
| **Value** | High. "In the PU" becomes exact (today the tailer infers it), load time is shown, and a 30k or AFK kick is labelled. Crash detection closes sessions cleanly. |
| **Effort** | Small to medium. It extends `SessionState`. |
| **Depends on** | None. |
| **Builds** | All. The reason table needs both `Nub destroyed` (4.x) and `DisconnectLight` (5.0) as `teardown`. |

`teardown` disconnects are normal and should not raise an event. The crash-handler line is untagged with no timestamp, so it is one of two exceptions to F1 (the other is notification continuations).

### 6. Quantum route: `QT_ROUTE`, `QT_CALIBRATION`, current location

```python
NAV = r"\[ItemNavigation\]\[CL\]\[\d+\] \| NOT AUTH \| (?P<ship>[A-Za-z0-9_]+?)_(?P<eid>\d{6,})\[\d+\]\|CSCItemNavigation::(?P<fn>\w+)\|(?P<body>.*?) \[Team_\w+\]"
r"<(?P<tag>Player Selected Quantum Target - Local|Calculate Route!?|Player Requested Fuel to Quantum Target - (?:Local|Server Routing))> " + NAV
# body:
r"Player has selected point (?P<dest>\S+) as their destination"
r"Successfully calculated route to (?P<dest>\S+) fuel estimate (?P<fuel>[\d.]+)"
r"Projected Start Location is (?P<start>.+?) for route to destination (?P<dest>\S+)"
# toasts:
r'Added notification "Quantum Travel Calibration (?P<phase>Started|Complete) By (?P<who>[^:"]*): "'
```

| | |
|---|---|
| **Fields** | `QT_ROUTE {class, dest_code, dest_kind, fuel_estimate, start}`; `QT_CALIBRATION {phase, own}`; `location.start` in the snapshot; `qt = {state: idle \| routed \| calibrating \| travelling, since}` |
| **Privacy** | The select line's entity id marks the ship as own (same table as seat releases) and is not emitted. `who` is compared to the handle and dropped. `PartyMemberMarker_<n>` destinations become `dest_kind: party_member` with no number. |
| **Value** | High. A quantum state for a Stream Deck key, the fuel estimate, and the best available "which system or body am I at" signal (there is no system-change event). It also makes own-ship detection earlier than the first seat release. |
| **Effort** | Medium. |
| **Depends on** | F3 for calibration ownership (an empty name is treated as unknown). The existing `QT_ARRIVED` closes the flow. |
| **Builds** | All. |

Select is logged again on every re-plan: debounce on `(eid, dest)`. There is no abort line, so `routed` or `travelling` times out (suggested: 10 min) or ends on the next select.

### 7. Crime and law: `CRIME`, `CRIMESTAT_UP`, `FINED`, `IMPRISONED`

```python
# after F2 joins the lines:
r'Added notification "Crime Committed: (?P<crime>[^\n"]+?)(?:\s+against (?P<victim>[^:]+))?: " \[\d+\]'
r'Added notification "(?P<attacker>\S+) committed (?P<crime>.+?) against you\s+Press \''
r'Added notification "CrimeStat Rating Increased: "'
r'Added notification "Fined (?P<amt>[\d,]+) UEC: "'
r"<AttachmentReceived> Player\[[^\]]+\] Attachment\[uba_prison_"
r"Location\[(?P<prison>PrisonMine_\w+)\]"
r'Added notification "Entered Klescher Rehabilitation Jurisdiction: "'          # single-line prison toast
```

| | |
|---|---|
| **Fields** | `CRIME {crime, has_victim}`; `CRIME_AGAINST_ME {crime}`; `CRIMESTAT_UP`; `FINED {amount}`; `IMPRISONED {location_class}` and `law.imprisoned` in the snapshot |
| **Privacy** | `victim` and `attacker` are matched and discarded; only `has_victim` survives. The prison attachment line names the local player: test the prefix only. |
| **Value** | High in 4.x: a crime log, fine totals, a prison state. |
| **Effort** | Small once F2 exists. |
| **Depends on** | F2. |
| **Builds** | 4.x. Not yet seen in 5.0 (tiny sample). Keep it, and never treat silence as "no crime". |

`Entered Klescher Rehabilitation Jurisdiction` (25 / 19, 4.10.193 to 4.10.201) is the simplest `IMPRISONED` trigger and needs neither F2 nor F3; keep the attachment and location lines as confirmation. `CrimeStat Rating Increased` fires again on a prison spawn: suppress it when `IMPRISONED` is raised in the same second. There is no rating value, so do not show a number.

### 8. Injury map: `INJURY`, `HEALED`

```python
r'Added notification "(?P<sev>Minor|Moderate|Severe) Injury Detected - (?P<part>[A-Za-z ]+?) - Tier (?P<tier>\d) Treatment Required'
r"<MED BED HEAL> .*?med bed name: (?P<bed>[^,]+), vehicle name: (?P<veh>[^,]+), head: (?P<head>\w+) torso: (?P<torso>\w+) leftArm: (?P<la>\w+) rightArm: (?P<ra>\w+) leftLeg: (?P<ll>\w+) rightLeg: (?P<rl>\w+)"
```

| | |
|---|---|
| **Fields** | `INJURY {part, severity, tier}`; `HEALED {parts, bed_class}`; `injuries = {part: {severity, tier, since}}` in the snapshot |
| **Privacy** | The `Actor:` field in the heal line is not captured. |
| **Value** | Medium-high. Per-limb injury tier is not in the telemetry vitals. |
| **Effort** | Small. Single-line toast; no F2 needed. |
| **Depends on** | Parser 2 (a death clears the map). |
| **Builds** | Injury toast: 92 / 22, 12545750 to 12913470 and 1 line in 5.0.202; none in 4.10.200 or 4.10.201. Heal line: gone after 4.10.197. |

Because the heal line is gone, the map needs other clear rules: clear on `DIED`, on `RESPAWNED`, and on session reset; mark entries stale after a fixed time. The dashboard must label it "last reported", not "current".

## Pass 3: valuable, but narrower or less certain

### 9. Ship retrieval and hangar: `SHIP_RETRIEVE`, `HANGAR_READY`, `SHIP_STOWED`, `CLAIM_RESULT`

```python
r"<VehicleListQuery> .*Retrieved (?P<ent>\d+) entitlements out of (?P<tot>\d+)"
r"<CEntityComponentShipListProvider::SetVehicleSpawningInformations> .*LandingArea: (?P<area>Docking Port \d+|.+?'s)"
r'Added notification "Hangar Request Completed: "'
r"<LandingArea_UnregisterFromExternalSystems_StowingVehicle> \[STOWING ON UNREGISTER\] (?P<area>.+?) \[\d+\] - Attempting to stow"
r"ShipElevator_(?P<hangar>Hangar[A-Za-z]+)"                                       # applied to area; else Pad_ or other
r"<(?:Claim request sent|RmTokenResponseClaim|Claim Failed)> .*?(?:Expedited: (?P<exp>\d)|result: (?P<res>\w+)|Claim vehicle failed: (?P<why>\w+))"
```

| | |
|---|---|
| **Fields** | `SHIP_RETRIEVE {area: personal_hangar \| docking_port}`; `HANGAR_READY {wait_s}`; `SHIP_STOWED {hangar_size}`; `CLAIM_RESULT {ok, reason, expedited}`; `fleet_size` |
| **Privacy** | The `<player>'s` hangar label maps to `personal_hangar`; the name is never stored. Vehicle entity id and request uuids internal. |
| **Value** | Medium. "Your ship is ready" after a wait of about 97 s is a good notification. |
| **Effort** | Medium. Correlation is by order and time only. |
| **Depends on** | None. The existing zone classifier already sees and ignores the hangar toast. |
| **Builds** | All; claim request and response are 5.0 only. |

`area` forms: `LandingArea_ShipElevator_Hangar<Size><Pos>[_<loc>]` (2,353), `[PROC]LandingArea_Pad_<Size>-<n>_{<GUID>}` outpost pads (343), and other names such as `<loc>_Garage_<n>`, `LandingArea_<n>` or `Pad <n>-<n>` (220; some contain spaces). `SetVehicleSpawningInformations` matches 243 of 245; the other two are an `[Error] ... Invalid landingAreaLocStr` line and an empty `LandingArea:`.

`StowingVehicle` fires 2,916 times in 210 files: it is logged for hangars streaming out too. Only raise `SHIP_STOWED` within a short window of an own `ClearDriver`, and validate that rule before shipping.

### 10. Party and crew: `PARTY`, `VOICE_CHANNEL`

```python
# after F2; applied to the body:
r"^(?P<p>.+?) has joined the party\.$"                    # names can contain spaces
r"^(?P<p>.*?) has left the party\.$"                      # the name can also be empty
r"^(?P<p>.+?) is now party leader\.$"
r"^Initiated by party leader (?P<p>.+?)\.$"
r"^(?P<p>.+?) has (?P<act>joined|left) the channel '(?P<ship>.+?) : (?P<owner>[^']*)'\.$"   # member voice-channel toasts
# applied to the joined toast text:
r"^Initiated by (?P<p>[^\n]+)\nParty Launching: Join Party In PU\?"              # launch prompt to you
# applied to the line:
r"Added notification \"You have (?P<act>joined|left the) channel '(?P<ship>.+?) : (?P<owner>[^']*)'\."
```

| | |
|---|---|
| **Fields** | `PARTY {event: created \| joined \| member_joined \| member_left \| leader_changed \| disbanded \| left \| launch, size}`; `party = {in_party, size, i_am_leader}`; `VOICE_CHANNEL {event: joined \| left, ship_display, is_own_ship}` |
| **Privacy** | Member handles live in an in-memory set used only to keep `size` correct. They are never emitted. `owner` is compared to the handle and dropped (it can be empty: treat that as unknown). The voice text is `joined channel` but `left the channel`; there is no doubled "the". A named roster would need an explicit opt-in flag and must stay out of `--record`. (Done: opt-in People panel, see [people-and-lookup.md](people-and-lookup.md).) |
| **Value** | Medium-high. Party size and "crew on someone else's ship" context. |
| **Effort** | Medium. |
| **Depends on** | F2, F3. |
| **Builds** | Party toasts: 12545750 to 13220821 (none in 5.0 so far). Own voice join: all; own voice leave: to 4.10.201. Member voice-channel toasts and the launch prompt: 12572603 to 13165946. |

There is no roster snapshot, so `size` is only right if the tailer saw the party form. Mark it `approximate` when the first party event in a file is not `created` or `joined`.

### 11. Shop ledger: `SHOP_TXN`

```python
r"<CEntityComponent(?:ShopUI|Shopping|CommodityUI)Provider::Send\w+Request> Sending SShop(?P<kind>Buy|Sell|Rental|CommodityBuy|CommoditySell)Request - .*?shopName\[(?P<shop>[^\]]+)\].*?(?:client_price|price|amount)\[(?P<price>[\d.]+)\](?:.*?shopPricePerCentiSCU\[(?P<unit>[\d.]+)\])?(?:.*?itemName\[(?P<item>[^\]]+)\])?(?:.*?quantity\[(?P<qty>[\d.]+)(?: cSCU)?\]|.*?offering\[(?P<offer>\d+)\])"
r"(?:Received ShopFlowResponse|Shop Flow Response|Commodity Transaction Response Error) - .*?(?:shopName\[(?P<shop>[^\]]+)\].*?)?result\[(?P<result>\w+)\](?:\s*type\[(?P<type>\w+)\])?"
r"AddingCommodityBox - .*?shopName\[(?P<shop>[^\]]+)\] commodityName\[ResourceType\.(?P<commodity>\w+)\]"
```

| | |
|---|---|
| **Fields** | `SHOP_TXN {kind, shop, item, quantity, price, unit_price, result}`; a session spend and earn total |
| **Privacy** | `playerId`, `shopId`, `kioskId` and all GUIDs are not captured. |
| **Value** | Medium-high. The only spend and earn ledger available; there is no wallet balance. |
| **Effort** | Medium. Request and result are joined by order per shop. |
| **Depends on** | None. |
| **Builds** | 4.x mostly. 5.0.201 has the `ShopUIProvider` lines and a new `<ShopBackend> [Legacy]` marker, which suggests the templates will change. That is why this is pass 3. |

- Emit on the result, not the request. A request with no result in 15 s is `unknown`.
- `SShopRentalRequest` has `offering[<n>]` and no `quantity`; the request regex accepts either.
- Commodity trades have no success line and no commodity name. Use the kiosk listing for names only when the terminal lists exactly one candidate; otherwise leave the name empty.
- Whether `price` is a total or a per-unit figure when `quantity > 1` was not established. Check before summing.

### 12. Economy toasts: `BLUEPRINT`, `CURRENCY_RECEIVED`, `AWARDED`

```python
r'Added notification "(?P<kind>Transaction Complete|Transaction Error|Received Blueprint|Awarded)[: ]\s*(?P<text>[^"]*?)(?::\s*)?" \[(?P<seq>\d+)\] to queue.*?MissionId: \[(?P<mission>[^\]]*)\]'
r"(?:wants to send you|has sent you:)\s*(?P<amt>[\d,]+) a?UEC"      # after F2
```

| | |
|---|---|
| **Fields** | `BLUEPRINT {name, contract: index}`; `CURRENCY_RECEIVED {amount}`; `AWARDED {amount}` |
| **Privacy** | The sender name leads the transfer toast: match the amount only and drop the rest. |
| **Value** | Medium. A blueprint collection log; rare but real wallet deltas. |
| **Effort** | Small once F2 and parser 3 exist. |
| **Builds** | Blueprint: 4.10.196 to 4.10.200 only so far. Transfers: to 4.10.197. |

### 13. Crash detail and environmental death: `SHIP_CRASH`, `BOUNDARY_COUNTDOWN`

```python
r"<FatalCollision> Fatal Collision occured for vehicle (?P<cls>[\w-]+?)_\d+ \[Part: (?P<part>[^,]+), .*?Zone: (?P<zone>[^,]+), PlayerPilot: (?P<pp>\d)\] after hitting entity: (?P<hit>[^\[]+?) \["
r"<Boundary Violation> .*?starting countdown \((?P<secs>[\d.]+)s\)"                   # a float, e.g. 3.0
r"<Boundary Violation> CVehicle::CheckForOceanCollision: .*?being destroyed due to intersecting with ocean"
```

| | |
|---|---|
| **Fields** | `SHIP_CRASH {class, part, zone_class, player_pilot, hit_kind: ship \| terrain \| unknown}`; `BOUNDARY_COUNTDOWN {seconds}`; `BOUNDARY_DESTROYED` |
| **Privacy** | `Pos`, and the `pos` and `vel` blocks of `Boundary Violation`, are not captured. `hit` is reduced to a kind; a value that is not an entity class is treated as a player and dropped. |
| **Value** | Medium. It explains a death in builds before `Vehicle Destroyed`. |
| **Effort** | Small. |
| **Builds** | `FatalCollision`: 55 / 27, 12572603 to 13220821 (1 line in 4.10.200, 3 in 4.10.201) and PTU 5.0.201 (1). `Boundary Violation`: 419 / 2, 4.10.196 to 4.10.197 only (396 `being destroyed`, 23 countdown). Low priority because both are rare. |

### 14. Restricted area and prison state in the zone model

Extend `classify_zone` with `Restricted Area - Vehicles Will Be Impounded`, `Restricted Area⏎For your safety, you've been relocated.` (needs F2), `Leaving Restricted Area` and `Monitored Space Down`. Fields: `zone.restricted: bool`, `zone.comms_down: bool`. Small effort; medium value for a Stream Deck status key.

## Later or skip

| Candidate | Why it waits |
|---|---|
| Carriage rides (`ELEVATOR_RIDE`) | 95% of lines are other players; no elevator name or destination. Little to show. |
| `Player Inventory Request Complete`, `StoreItem`, `EquipItem` | Very noisy; needs rate limiting and a clear use first. |
| `Show Location Inventory` location code | A possible second location source. Worth a look once parser 6 is in. |
| Hauling cargo marker | Two files, 4.10.196 to 4.10.197 only. |
| `Population Manager Stats`, `GameTelemetry Client connected` | 5.0 only, 30 and 36 lines. Cheap, and the second confirms the bridge's own connection, but wait for more PTU data. |
| `[Instancing] Player entered/exited instance`, `party-launch`, `[ActorState] Place`, `PlayerJoined` / `PlayerLeft` (mission party) | Gone after 4.10.200. |
| Mission comms calls (`SHUDEvent_OnCommsNotification`, `SpawnCommsEntity`, `Comms Notification Flow`) | No useful field beyond `CommsNotifications`; they carry the local handle. |
| `UnstowPendingEntities` | Item class taken out of inventory (1,988 / 123, every build). Possible "equipped" feed later; carries the local handle. |
| Arena Commander lobby and spawn lines | Different game mode; out of scope for the PU dashboard. |
| Status effects (`Dehydrated`, `Starved`) | Untagged, mostly other players. The telemetry vitals cover hunger and thirst. |
| `SetSalvageRepairAmmoCount_NoTarget`, `Trying to QT when not in Idle`, `Too many actions`, objective marker DataBank lines | Skip permanently: other players' names or coordinates, and no useful field. |

## Rules that apply to every parser

- **Every tag is optional.** 4.10.201 already dropped lines that 4.10.200 had. A parser that sees nothing must leave its state `unknown`, not "none".
- **Identity is compared, never carried.** Handles and ids exist only inside the tailer. The existing guarantee stays: snapshot, SSE, `/api/signals`, events and `--record` carry class names, titles, kinds and counts only.
- **Join keys stay inside.** Mission, objective, group and request uuids and entity ids are dictionary keys. What is emitted is a small per-session index.
- **Reset** all new state where `SignalsState` resets today (new file, `Log started`, session join, shard change, leave). Contract state is the exception: keep it across a shard change, because a mission can continue.
- **Tests use synthetic lines** with fake ids and fake handles. No real log line is committed.
- **Rate-limit** any event that can burst (nearby destruction, objectives, inventory), as `NEARBY_SHIP` does.

## Suggested order of work

1. F1, F2, F3 with tests. F2 also fixes the existing zone parser for multi-line toasts.
2. Parsers 1 and 2 together (kill feed and death; they share the vehicle id table).
3. Parser 5 (session), which is independent.
4. Parsers 3 and 4 (contracts, then objectives).
5. Parsers 6, 7, 8.
6. Pass 3 as needed, after a re-scan of PTU 5.0 logs.

Before step 5, re-check the items under "Open questions" in [gamelog-catalog.md](gamelog-catalog.md).

## Regex check against the corpus

Every regex in the code blocks above was run on 2026-11-21 over the 293-file corpus (LIVE, HOTFIX and PTU `Game.log` plus log backups). Toast regexes ran on toasts joined as F2 describes; body regexes ran on the body with the trailing `: ` removed. No regex matches zero lines. "Of" is the number of lines that carry the tag or text the regex targets.

| Parser | Regex | Matches (lines / files) | Of |
|---|---|---|---|
| F1 | `LINE` (physical tagged lines) | 2,198,357 / 293 | all tagged lines except 1,136 `[flow] <noname>` |
| F3 | `Legacy login response` | 282 / 269 | 282 |
| F3 | `CLoginSession` | 13 / 13 | 13 (5.0 only) |
| F3 | `AddAccessibleDestinations` | 1,122 / 280 | 1,122 |
| F3 | `AccountLoginCharacterStatus_Character` | 283 / 283 | 283 (the earlier pattern without `>` matched 0) |
| F3 | `ClearDriver` | 473 / 133 | 473 |
| 1 | `Vehicle Destroyed` | 538 / 30 | 538 |
| 2 | corpse item | 49,707 / 102 | all |
| 2 | `Spawned!` | 1,281 / 280 | all |
| 2 | Emergency toast | 92 / 71 | 92 |
| 2 | `[ActorState] Dead` | 139 / 44 | 139 |
| 3 | contract toast | 3,125 / 86 | 3,125 |
| 3 | `EndMission` | 815 / 56 | 815 |
| 3 | `MissionEnded` | 718 / 53 | 718 |
| 3 | `CommsNotifications` send | 496 / 48 | 496 (the first version matched 458: it missed the ` -<removed>` form) |
| 3 | organisation and giver | 496 / 48 | 496 |
| 3 | mission type | 480 / 43 (321 lines with a type other than `Generic`) | 496 |
| 4 | `ObjectiveUpserted` / `ObjectiveComplete` | 14,244 / 63 | 13,654 + 590 |
| 4 | `UpdateActiveObjective` | 12,149 / 61 | 12,149 |
| 4 | `CreateMarker` and `SMarkerHandler_Base` | 1,422 / 49 | 1,374 + 48 |
| 4 | `GenerateLocationProperty` | 6,958 / 144 | 6,958 |
| 4 | `LOC` (findall) | 11,592 locations / 144 | |
| 5 | `Context Establisher Done` (`CReplicationModel`) | 1,048 / 280 | 3,226 tag lines in all |
| 5 | `Channel Disconnected` | 1,293 / 252 | all |
| 5 | `Error Popup Opened` | 17 / 16 | 17 |
| 5 | `SystemQuit` | 249 / 249 | 249 |
| 5 | crash handler | 17 / 17 | 17 |
| 5 | `AccountLoginQueueStatus` | 189 / 1 | 189 |
| 6 | `NAV` with the QT tags | 4,626 / 113 | all five tags |
| 6 | selected point | 1,062 / 110 | 1,062 |
| 6 | route and fuel | 1,349 / 112 | 1,349 |
| 6 | projected start | 1,353 / 112 | 1,353 |
| 6 | calibration toast | 521 / 52 | 521 |
| 7 | `Crime Committed` | 210 / 22 | all |
| 7 | crime against you | 7 / 3 | 7 |
| 7 | `CrimeStat Rating Increased` | 63 / 33 | 63 |
| 7 | `Fined` | 7 / 7 | 7 |
| 7 | prison attachment | 132 / 24 | 132 |
| 7 | `PrisonMine_` location | 24 / 18 | 24 |
| 7 | Klescher toast | 25 / 19 | 25 |
| 8 | injury toast | 92 / 22 | 92 |
| 8 | `MED BED HEAL` | 17 / 10 | 17 |
| 9 | `VehicleListQuery` | 1,037 / 175 | 1,037 |
| 9 | `SetVehicleSpawningInformations` | 243 / 119 | 245 |
| 9 | `Hangar Request Completed` | 326 / 116 | 326 |
| 9 | `StowingVehicle` area | 2,916 / 210 | 2,916 (the first version matched 2,352) |
| 9 | `ShipElevator_Hangar*` | 2,353 / 206 | 2,916 |
| 9 | claim request, response, failure | 4 / 3 | 4 |
| 10 | member joined the party | 559 / 97 | 559 |
| 10 | member left the party | 558 / 91 | 558 (the `\S+` version matched 533) |
| 10 | new party leader | 172 / 76 | 172 |
| 10 | launch by party leader | 75 / 31 | 75 |
| 10 | member voice channel | 199 / 67 | 104 joined + 95 left |
| 10 | launch prompt (`Initiated by <player>`) | 48 / 19 | 48 |
| 10 | own voice channel | 446 / 147 | 342 joined + 104 left (the `left the the` version matched 0 leaves) |
| 11 | shop request | 243 / 56 | 243 (the `quantity`-only version missed the rental) |
| 11 | shop result | 235 / 54 | 235 |
| 11 | `AddingCommodityBox` | 138 / 5 | 138 |
| 12 | economy toasts | 120 / 10 | 120 |
| 12 | currency transfer amount | 36 / 6 | 36 |
| 13 | `FatalCollision` | 55 / 27 | 55 |
| 13 | boundary countdown | 23 / 2 | 23 (the integer version matched 0) |
| 13 | boundary destroyed | 396 / 1 | 396 |
