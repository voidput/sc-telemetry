# scgameapi

Unofficial client, documentation and live dashboard for **Star Citizen's local Game Telemetry API**, the gRPC service `sc.game.telemetry.v1.GameTelemetryService` that the game client can host on localhost. It is present in LIVE 4.10 and PTU 5.0, and CIG has not documented it.

> Not affiliated with or endorsed by Cloud Imperium Games. Uses only the game's own configuration switches; no binaries are modified. Behaviour may change with any patch.

## What you get

- **The schema:** extracted at runtime from your own `StarCitizen.exe` and cached per build in `.tmp/proto/<branch>-<CL>/` (`scgameapi/schema.py`). No CIG-derived files are committed.
- **Live data at about 20 Hz:**
  - Player vitals: heart rate, health, oxygen, G-load, temperature, radiation.
  - Ship: speed, heading, fuel, boost, hull, shield faces, master mode, power state, annunciators, landing gear, IFCS assists.
  - Weapons and countermeasures, plus game events.
- **Limited write access:** pressing or holding allowlisted input actions through `TriggerGameAction`.
- **A dashboard:** a FastAPI bridge that relays the gRPC stream to a browser over SSE, with recording and replay.

## Docs

- [API spec](docs/telemetry/api-spec.md): transport, enabling, RPCs, the read/write model, data gotchas, security and versioning.
- [API reference](docs/telemetry/api-reference.md): generated tables of every message, field and enum, with observed ranges.
- [Discovery notes](docs/telemetry/game-telemetry-api.md): how it was found and the test log.

## Setup

Works on **Windows** and **Linux** (Wine / LUG Helper / Lutris / Steam Proton prefixes). The bridge runs on the same machine as the game. Both OSes are tested in CI (`.github/workflows/ci.yml`); Windows has not yet been verified against a live game, so reports are welcome.

### 1. Install uv (Python 3.13 is fetched by uv)

| OS | Command |
|---|---|
| Windows | `winget install astral-sh.uv` or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Linux | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (or your distro's `uv` package) |

Then in a clone of this repo: `uv sync`.

### 2. Enable the API in the game

The game needs two environment variables at launch plus one `USER.cfg` line.

**Windows** (PowerShell or cmd, user env vars):

```powershell
setx GRPC_CLIENT_SERVER_ENABLED 1
setx GRPC_CLIENT_SERVER_PORT 50051
```

`setx` only affects processes started afterwards: fully quit the RSI Launcher (including its tray icon) and start it again so the game inherits the variables.

**Linux (Wine / LUG / Lutris):** export them in whatever starts the RSI Launcher under Wine, before `wine` runs. For example the LUG Helper launch script (`sc-launch.sh` in your prefix), or the environment-variables section of the Lutris / Steam game settings:

```bash
export GRPC_CLIENT_SERVER_ENABLED=1
export GRPC_CLIENT_SERVER_PORT=50051
```

**USER.cfg** (create the file if it does not exist) in the channel folder you play, e.g. `LIVE` or `PTU`:

| OS | Path |
|---|---|
| Windows | `C:\Program Files\Roberts Space Industries\StarCitizen\<CHANNEL>\USER.cfg` (or your library folder) |
| Linux | `<WINEPREFIX>/drive_c/Program Files/Roberts Space Industries/StarCitizen/<CHANNEL>/USER.cfg` |

```ini
server.game.telemetry.enabled = 1
; optional: allow external clients to press allowlisted input actions
; server.game.telemetry.trigger.actions.enabled = 1
```

Launch the game. `Game.log` should show `GameTelemetryService ... 127.0.0.1:50051 [Insecure]`. Frames start once your character is in the world. Verified on PTU 5.0; on LIVE 4.10 the switches differ (see the spec).

### 3. Run the dashboard

```bash
uv run scgameapi-dashboard          # -> http://127.0.0.1:8765
```

Or use the thin wrappers `scripts/run.sh` (Linux) / `scripts/run.ps1` (Windows), which pass all arguments through.

**Install discovery.** The bridge needs `StarCitizen.exe` (to extract the schema) and `Game.log` (session context). It finds the install dir (the `StarCitizen` folder holding `LIVE/`, `PTU/` ...) in this order (`scgameapi/paths.py`):

1. `--sc-dir DIR` or `$SC_DIR`
2. Windows: `%ProgramFiles%\Roberts Space Industries\StarCitizen`, then `<drive>:\Program Files\Roberts Space Industries\StarCitizen`, `<drive>:\Roberts Space Industries\StarCitizen`, `<drive>:\Games\Roberts Space Industries\StarCitizen` on every drive, plus paths named in the RSI Launcher log (best effort)
3. Linux: `$WINEPREFIX`, `~/Games/star-citizen` (LUG / Lutris default), `~/.wine`, `~/Games/*`, Steam Proton prefixes (`steamapps/compatdata/*/pfx` under `~/.steam/steam`, `~/.local/share/Steam` or Flatpak Steam; a Steam library on another drive needs `SC_DIR` or `WINEPREFIX`), then `/mnt/*/star-citizen*`, `/mnt/*/*/star-citizen*`, `/media/<user>/*/star-citizen*`, `/run/media/<user>/*/star-citizen*` (each + `drive_c/Program Files/Roberts Space Industries/StarCitizen`)

The channel is `--channel` / `$SCGAMEAPI_CHANNEL`, else the channel whose `Game.log` was written last, else `LIVE`. If the install is elsewhere, set it explicitly:

```powershell
# Windows PowerShell
$env:SC_DIR = "D:\Games\Roberts Space Industries\StarCitizen"
$env:SCGAMEAPI_CHANNEL = "PTU"
uv run scgameapi-dashboard
```
```bash
# Linux
SC_DIR="$HOME/Games/star-citizen/drive_c/Program Files/Roberts Space Industries/StarCitizen" \
SCGAMEAPI_CHANNEL=PTU uv run scgameapi-dashboard
```

`SCGAMEAPI_EXE=<path to StarCitizen.exe>` overrides the schema source alone. Without an install (e.g. `--mock`), the newest cached schema in `.tmp/proto/` is used, so run the bridge once against a real install first.

### More commands

```bash
# quick probe
uv run python scripts/telemetry_probe.py stream --n 3 --timeout 60
uv run python scripts/telemetry_probe.py actions

uv run scgameapi-dashboard --record             # also save frames to .tmp/recordings/*.jsonl
uv run scgameapi-dashboard --replay .tmp/recordings/<file>.jsonl
uv run scgameapi-dashboard --mock               # synthetic data (needs a cached schema)
uv run scgameapi-dashboard --game-log PATH      # explicit Game.log; --no-game-log to disable
uv run scgameapi-dashboard --alerts-audio browser   # play alert audio only in the dashboard tab
```

The schema and Python stubs are generated at runtime (`.tmp/proto/`, `.tmp/gen/`); nothing generated or CIG-derived is committed. Run from a clone with `uv run`: all state lives in the repo's `.tmp/`.

### Alert audio

Audio plays in the newest dashboard tab with **Enable sound** clicked (WebAudio + Web Speech, any OS), otherwise on the bridge machine:

| OS | Sound player | Speech (off by default, `settings.speech`) |
|---|---|---|
| Windows | `winsound` (stdlib, WAV) | Windows SAPI via PowerShell; espeak-ng / piper if installed |
| Linux | `pw-play` > `paplay` > `aplay` (WAV) > `ffplay` | piper > espeak-ng > espeak > spd-say |

Bundled tones (`scgameapi/data/sounds/*.wav`, regenerate with `scripts/gen_tones.py`) are the fallback when the freedesktop sound theme is missing. See [alerts](docs/telemetry/alerts.md).

### Ship capabilities (optional)

Mining / salvage / refuel gating uses a game-data export (`game-ships.json`, plus `game-salvage.json` beside it) produced by an sc-gamedata extractor (a separate tool, not part of this repo). Put it at `.tmp/gamedata/<channel>/game-ships.json` (e.g. `.tmp/gamedata/live/`) or point `--gamedata PATH` / `$SCGAMEAPI_GAMEDATA` at the file or its directory. Without it, capabilities are simply unknown and nothing is gated.

### Tests

```bash
uv sync            # dev group (pytest, httpx) is installed by default
uv run pytest -q
```

The dashboard JS tests need `node` on PATH and are skipped without it. The replay regression test needs local recordings in `.tmp/recordings/` and is skipped on a fresh clone. CI runs on Ubuntu and Windows (`.github/workflows/ci.yml`).

### Troubleshooting

- `Game.log` shows `gRPC telemetry service NOT running`: the env vars did not reach the game. On Windows, restart the RSI Launcher completely after `setx`; on Linux, export them in the script that starts Wine. Check the "Startup Settings" block for `[Env Var] GRPC_CLIENT_SERVER_ENABLED=true`.
- Connected but no frames: `USER.cfg` lacks `server.game.telemetry.enabled = 1` (wrong channel folder?), or your character is not in the world yet.
- `telemetry schema unavailable ... Searched: ...`: the install was not found; set `SC_DIR` (and `SCGAMEAPI_CHANNEL`) or `SCGAMEAPI_EXE`.
- `[gamelog] ... not found`: same, for `Game.log`; the session panel stays empty until the game writes it.
- Port in use: another bridge is running; pick `--port 8766`. The game's port is `GRPC_CLIENT_SERVER_PORT` (bridge `--grpc 127.0.0.1:<port>`).
- No bridge sound: check the `[alerts] ... player ...` startup line, or use `--alerts-audio browser`.

## After a game patch

Nothing to do: the new build gets a new cache key and the schema is re-extracted automatically on the next start. To refresh the reference docs:

```bash
uv run python scripts/gen_api_spec.py --recording .tmp/recordings/<latest>.jsonl
git diff docs/telemetry/api-reference.md   # the generated reference shows schema changes
```

## Layout

```
scgameapi/    bridge server (FastAPI + gRPC aio), install discovery (paths.py), schema + stub loader,
              Game.log tailer, alerts, mock generator, static dashboard, data/ (JSON maps, bundled tones)
scripts/      extract_protos.py, gen_api_spec.py, gen_tones.py, telemetry_probe.py, obsidian_frontmatter.py, run.sh, run.ps1
tests/        pytest suite (synthetic fixtures only)
docs/         Obsidian-compatible vault (Home.md / README.md indexes)
```

## Security

The API has **no authentication and no TLS**. Keep it bound to loopback, which is the default; never set `GRPC_CLIENT_SERVER_EXTERNAL`. With trigger actions enabled, any local process can send inputs to your game. The stream includes your aUEC balance. Recordings (`*.jsonl`) are gitignored for that reason, and the generated reference redacts it.

Privacy: player handles from `Game.log` are shown only with `--show-names`; RSI org lookups only with `--lookup-orgs` (cached in `.tmp/rsi-cache.json`, mode 0600 on Linux; on Windows protected by your user-profile ACL). Server address and session ids are never written to recordings.
