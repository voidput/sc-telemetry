---
title: People and org lookup
tags: [docs, telemetry]
updated: 2026-11-21
---

# People and org lookup

The dashboard's **People** panel lists players whose handles appear in Game.log, grouped by how they relate to you. An opt-in lookup adds each handle's main org from the public RSI citizen page. Code: `scgameapi/people.py`; API: [api-spec.md §8.7](api-spec.md#87-people-and-org-lookup).

**Privacy rule for this note:** templates only (`<player>`, `<id>`). No real handle, org or id appears here.

## Toggles

| Toggle | Default | Start value | Runtime | Effect |
|---|---|---|---|---|
| **Show names** | off | `--show-names` / `SCGAMEAPI_SHOW_NAMES=1` | `PUT /api/people/settings {"show_names": bool}` | Handles appear in `/api/people` and the same-origin snapshot. Off: counts only. |
| **Look up orgs** | off | `--lookup-orgs` / `SCGAMEAPI_LOOKUP_ORGS=1` | `PUT /api/people/settings {"lookup_orgs": bool}` | The bridge fetches citizen pages for the listed handles. It only fetches while **both** toggles are on. Off: no request is ever sent; handles stay plain links. |

Runtime changes are not persisted; a restart goes back to the CLI/env values. Every handle with a valid RSI charset (`[A-Za-z0-9_-]`) is a link to `https://robertsspaceindustries.com/citizens/<handle>`, opened in **your** browser (`noopener noreferrer`), whatever the lookup toggle says. Names with spaces (some party toasts) are shown but never linked or looked up.

## Sources (verified against 1.3 GB of LIVE/HOTFIX/PTU logs)

| Relation | Line | Notes |
|---|---|---|
| `crew` | `You have joined channel '<ShipDisplayName> : <owner>'.` | Owner of a ship voice channel you joined (not you). Strongest "physically with you" signal. |
| `crew` | `<player> has joined/left the channel '<ship> : <owner>'.` (toast body; title `New Member Joined` / `Member Left`) | Member and owner. `left` marks the leave. Up to 4.10.200 and rare in 5.0. |
| `party` | `New Member Joined` ⏎ `<player> has joined the party.`; `Member Left` ⏎ `<name> has left the party.`; `New Party Leader` ⏎ `<player> is now party leader.` (these three only under their titles); `Party` ⏎ `<player> connected./disconnected.`; `You have joined` ⏎ `<leader>` (in 240 sampled logs the body was always the literal word `party`, which is skipped; only a valid handle counts); `Initiated by [party leader] <player>`; `Join queue canceled by party leader <player>.`; `<party-launch> ... party-launch from leader[<player>]` | Party members by construction, not necessarily nearby. Absent from 5.0 PTU logs so far. |
| `party` | `<player> has started to QT to your current location.` | Incoming party member. |
| `party_likely` (`PARTY?`) | `Quantum Travel Calibration Started/Complete By <player>: ` | Usually a party member's own jump broadcast to you (~95 %); empty and `PU_*` names skipped. |
| `nearby` | `<Vehicle Destroyed> ... (owner player [<id>], ...) ... killer '<player>' [<id>]` | Killer (not self, not NPC). The victim has only an id: it is named only when the same id was earlier seen as a killer. 4.10.200+ and 5.0. |

**Excluded:** `<Too many actions>` (spam, gone in current builds), CommsNotifications `Player:`, `SHUDEvent_OnCommsNotification`, `SpawnCommsEntity`, hangar `LandingArea: <player>'s` (all name only the local player), objective-marker DataBank lines. Self-only lines plus `Handle[...]`, `handle[...]`, `destinations received for player [...]`, `AccountLoginCharacterStatus ... geid <id> ... name <player>` and `ClearDriver: Local client node [<id>]` build the **self** set (every alias in the file, plus player ids); self is shown once as **you** and never as another person.

**NPC filter:** empty, `none`, `PU_*`, `AI_*`, `Citizen-<n>`, or a trailing `_<6+ digits>` entity id. No blanket `ABC_` prefix rule: real handles contain underscores.

**Multi-line toasts:** HUD notifications can span two physical lines (`Added notification "<title>` then `<ts> <body>: " [<n>] to queue`). `people.py` joins them (up to 5 lines, survives a poll boundary) and drops a toast when a tagged or indented queue-dump line interrupts it.

**Lifetime:** reset on a new Game.log file or `Log started`; `nearby`-only people are dropped on shard join/change/leave (crew and party survive). At most 300 people per session.

## Org lookup

- Pages: `/citizens/<handle>` (moniker, enlisted date, main org name + SID + rank + visibility) then `/citizens/<handle>/organizations` (affiliation count). Parsed with stdlib `html.parser` / regex; no new dependency, no JavaScript.
- Politeness: one request in flight, ≥ 3 s between requests, 10 s timeout, `User-Agent: scgameapi/0.1 (local telemetry dashboard)` with no personal data. Lookup order: you, then crew → party → party? → nearby, most recent first.
- Failure handling: 404 → `not_found` (cached 1 day); 403/429/503 → paused 60 s, doubling to 15 min (shown in the panel as "RSI lookups paused"); other errors, a timeout, an unrecognised page (layout change, challenge page) or a page for a different handle → `error`, retried after 1 h; 3 such errors in a row pause all lookups like a 429 ("paused (3 failed lookups in a row ...)"), reset by the next ok/404. A profile whose organizations tab was not read (toggle off mid-lookup, error, block) is kept as `partial` for 1 h only, then retried. The panel shows the last status.
- Robustness: parsing runs in a worker thread; pages deeper than 400 open elements or with more than 500 entries count as unrecognised; parsed fields are cut to 100 characters.
- `--mock` always uses the offline fake fetcher: its synthetic handles never reach RSI.
- Switching Look up orgs off stops new requests immediately, including one waiting for its rate-limit slot.

### Cache `.tmp/rsi-cache.json`

The cache necessarily holds handles and their public org data, so: it lives in `.tmp/` (gitignored), is written atomically with mode `0600`, keys are lower-cased handles, entries expire after 7 days (`not_found` after 1 day, `partial` after 1 h) and expired entries are pruned from memory and file on load and on every save (at most 2000 entries, oldest dropped). **Clear cache** in the panel or `DELETE /api/people/cache` deletes it (memory and file); a lookup in flight at that moment discards its result. Nothing else about people is written to disk.

## Privacy guarantees

- Handles live in memory only, for the current Game.log session.
- Never in `--record` recordings (the session/signals recorders do not see people), never in hub events, never in bridge log lines (stderr says "lookup paused: HTTP 429", never who).
- `/api/people`, `PUT /api/people/settings` and `DELETE /api/people/cache` use the same Origin/Host guard as `/api/session/private`. `/events` and `/api/snapshot` include handles only for a request that passes that guard while Show names is on; any other origin gets counts and `names_blocked: true`.
- Tests and fixtures use synthetic handles (`TestPilot_A`) and hand-written HTML in the public page's layout (`tests/fixtures/rsi/`); no test touches the network (`people.fake_fetch`, or the hidden `--fake-rsi` bridge switch for manual runs).

## Nearby panel: ship info and ×N

Each nearby class shows `manufacturer · role · S<size> · crew <n> · <cargo> SCU` from the local game dataset (`shipcaps.ShipCaps.info`, no network). `×N` counts **distinct ship instances (entity ids) seen this session — not people**; reclaimed or respawned ships may count twice, and parked and AI ships are included (tooltip in the dashboard).
