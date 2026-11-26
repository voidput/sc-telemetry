---
title: Install and setup
tags: [docs]
updated: 2026-11-25
---

# Install and setup

A step-by-step guide for Star Citizen players on **Windows** or **Linux** (Wine, LUG Helper, Lutris or Steam Proton). You do not need to be a developer. Every command below goes in a terminal: **PowerShell** on Windows (Start menu → type `PowerShell`), any terminal on Linux.

The tool has two parts that run on the same PC as the game:

1. **The game** hosts a small data service on your own machine (`127.0.0.1`, port `50051`). It is off by default. Step 3 turns it on.
2. **The bridge** (this project) reads that service and serves a dashboard to your browser at <http://127.0.0.1:8765>.

Nothing is sent over the internet, except the optional org lookup (see [Optional extras](#6-optional-extras)).

> Unofficial and not affiliated with Cloud Imperium Games. It uses only the game's own configuration switches and never modifies game files. A patch can change or remove the service at any time.

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Install uv and get the code](#2-install-uv-and-get-the-code)
3. [Enable the game's telemetry](#3-enable-the-games-telemetry)
4. [Check that it worked (Game.log)](#4-check-that-it-worked-gamelog)
5. [Run the bridge and open the dashboard](#5-run-the-bridge-and-open-the-dashboard)
6. [Optional extras](#6-optional-extras)
7. [Troubleshooting](#7-troubleshooting)
8. [Uninstall or disable](#8-uninstall-or-disable)

## 1. Prerequisites

| You need | Notes |
|---|---|
| Star Citizen installed | **PTU 5.0** is fully verified (live data and button presses). **LIVE 4.10** has the data stream only (no button presses), uses different switches, and this setup has not been tested on it. See [API spec §2](telemetry/api-spec.md#2-enabling) and [§7](telemetry/api-spec.md#7-versioning). |
| uv | A small tool that installs Python and this project's libraries for you. You do **not** need to install Python yourself. |
| Internet on first run | uv downloads Python 3.13 and the libraries once. After that, the bridge works offline. |
| git (optional) | Only to clone and update the code. A ZIP download works too. |
| Node.js (optional) | Only for developers who run the dashboard's JavaScript tests. Players do not need it. |

## 2. Install uv and get the code

### Install uv

**Windows**, pick one:

```powershell
winget install astral-sh.uv
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Linux**, pick one:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
sudo pacman -S uv        # Arch / CachyOS / Manjaro; other distros may package it too
```

Close the terminal and open a new one, so it finds `uv`. Check with `uv --version`.

### Get the code

**With git:**

```bash
git clone https://github.com/voidput/sc-telemetry.git
cd sc-telemetry
```

**Without git:** download <https://github.com/voidput/sc-telemetry/archive/refs/heads/main.zip> (or on the [repository page](https://github.com/voidput/sc-telemetry) click **Code → Download ZIP**), extract it (it unpacks as `sc-telemetry-main`; for example to `Documents\sc-telemetry-main`), then open a terminal in that folder. On Windows: open the folder in Explorer, then **File → Open Windows PowerShell**, or right-click inside the folder → **Open in Terminal**.

All later commands run from this folder. The bridge keeps its own files (cached schema, recordings, settings) in a `.tmp` folder inside it.

## 3. Enable the game's telemetry

> **These steps are for PTU 5.0.** On LIVE 4.10 they are expected not to work as written: LIVE has no `server.game.telemetry.enabled` setting and uses a different switch (`grpc-client-server-address`) instead of the port variable, and this project has not been tested there. See [API spec §2](telemetry/api-spec.md#2-enabling). Below, replace `<CHANNEL>` and `PTU` with the channel folder you play.

This is the important part. The game needs **two environment variables** when it starts, plus **one line in `USER.cfg`**.

### 3a. Environment variables

| Variable | Value | Meaning |
|---|---|---|
| `GRPC_CLIENT_SERVER_ENABLED` | `1` | Start the local data service |
| `GRPC_CLIENT_SERVER_PORT` | `50051` | Port it listens on (the bridge expects 50051) |

Never set `GRPC_CLIENT_SERVER_EXTERNAL`: it would expose the service, which has no password, to your whole network.

**Why the launcher restart matters:** the game does not read these variables from the system directly. It inherits them from the program that starts it, which is the RSI Launcher. A launcher that was already running when you set them does not have them, and neither will any game it starts.

#### Windows

Run once in PowerShell or Command Prompt (no administrator needed, saved for your user account):

```powershell
setx GRPC_CLIENT_SERVER_ENABLED 1
setx GRPC_CLIENT_SERVER_PORT 50051
```

Each line should print `SUCCESS: Specified value was saved.`

Then **fully quit the RSI Launcher**: close its window **and** right-click its icon in the system tray (bottom right, near the clock) → **Quit**. Start the launcher again. If in doubt, sign out of Windows and back in, or restart the PC.

Prefer clicking? Start menu → type `environment` → **Edit environment variables for your account** → under *User variables* click **New...** twice and add the two variables above. Then restart the launcher the same way.

To check: open a **new** PowerShell window and run

```powershell
[Environment]::GetEnvironmentVariable("GRPC_CLIENT_SERVER_ENABLED", "User")
[Environment]::GetEnvironmentVariable("GRPC_CLIENT_SERVER_PORT", "User")
```

It should print `1` and `50051`.

#### Linux

Wine passes your Linux environment through to Windows programs, so the variables must be set in whatever starts the RSI Launcher.

- **LUG Helper / plain Wine launch script** (for example `sc-launch.sh` in your game prefix): add these lines near the top, before the line that runs `wine`:

  ```bash
  export GRPC_CLIENT_SERVER_ENABLED=1
  export GRPC_CLIENT_SERVER_PORT=50051
  ```

- **Lutris:** right-click Star Citizen → **Configure** → **System options** tab → **Environment variables** → add both names and values → **Save**.
- **Steam (Proton):** game **Properties → Launch options**:

  ```
  GRPC_CLIENT_SERVER_ENABLED=1 GRPC_CLIENT_SERVER_PORT=50051 %command%
  ```

- **Any other way:** start the launcher from a terminal with the variables in front, for example
  `GRPC_CLIENT_SERVER_ENABLED=1 GRPC_CLIENT_SERVER_PORT=50051 WINEPREFIX="$HOME/Games/star-citizen" wine "C:\Program Files\Roberts Space Industries\RSI Launcher\RSI Launcher.exe"`

Then quit the launcher completely and start it again the way you just changed.

### 3b. USER.cfg

`USER.cfg` is a plain text settings file in the folder of the channel you play (`LIVE`, `PTU`, ...). It often does not exist yet; then create it.

| OS | Default location |
|---|---|
| Windows | `C:\Program Files\Roberts Space Industries\StarCitizen\<CHANNEL>\USER.cfg` (`<CHANNEL>` is `PTU`, `LIVE`, ...; a custom library folder moves it) |
| Linux | `<WINEPREFIX>/drive_c/Program Files/Roberts Space Industries/StarCitizen/<CHANNEL>/USER.cfg` (LUG / Lutris default prefix: `~/Games/star-citizen`) |

Add these lines (keep any lines already in the file):

```ini
server.game.telemetry.enabled = 1
; optional, see below: let local programs press game inputs
; server.game.telemetry.trigger.actions.enabled = 1
```

**Windows tips:**

- Easiest: in PowerShell run
  `notepad "C:\Program Files\Roberts Space Industries\StarCitizen\PTU\USER.cfg"`
  (change `PTU` to your channel). Notepad offers to create the file if it is missing.
- The name must be exactly `USER.cfg`, not `USER.cfg.txt`. In Explorer turn on **View → Show → File name extensions** to check.
- If saving says *access denied*, start Notepad with **Run as administrator** and open the file from there.

**Linux:** any text editor works, for example
`nano "$HOME/Games/star-citizen/drive_c/Program Files/Roberts Space Industries/StarCitizen/PTU/USER.cfg"`.

**About `trigger.actions.enabled`:** off (commented out with `;`) means the dashboard only *shows* data. On means the dashboard's buttons can press an allowlisted set of game inputs (landing gear, lights, shields ...); destructive ones ask for confirmation first. It also means **any program on your PC** could press those inputs, because the game's service has no password. Turn it on only if you want the buttons. The list is in [triggerable actions](telemetry/triggerable-actions.md).

Restart the game after editing `USER.cfg`.

## 4. Check that it worked (Game.log)

Start the game through the restarted launcher and get to the main menu. The game writes `Game.log` next to `USER.cfg`. Search it:

**Windows (PowerShell):**

```powershell
Select-String -Path "C:\Program Files\Roberts Space Industries\StarCitizen\PTU\Game.log" -Pattern "GRPC_CLIENT_SERVER|telemetry.enabled|GameTelemetry"
```

**Linux:**

```bash
grep -E "GRPC_CLIENT_SERVER|telemetry.enabled|GameTelemetry" \
  "$HOME/Games/star-citizen/drive_c/Program Files/Roberts Space Industries/StarCitizen/PTU/Game.log"
```

Replace `PTU` in these paths with your channel, and the start of the path with your library folder or Wine prefix if you installed somewhere else.

Working setup (the `...` parts vary):

```
[Env Var  ] GRPC_CLIENT_SERVER_ENABLED=true
[Env Var  ] GRPC_CLIENT_SERVER_PORT=50051
[User CFG ] server.game.telemetry.enabled=true
<Register Service> Service 'sc.game.telemetry.v1.GameTelemetryService' registered
<GRPC Server> ... [sc.game.telemetry.v1.GameTelemetryService]: 127.0.0.1:50051 [Insecure]
```

`Client connected` lines appear later, once the bridge is running.

| What you see | Meaning |
|---|---|
| No `[Env Var  ] GRPC_CLIENT_SERVER_...` lines | The game did not get the variables. Restart the launcher fully (step 3a). |
| `gRPC telemetry service NOT running` | Same cause. |
| No `[User CFG ] server.game.telemetry.enabled` line | `USER.cfg` is in the wrong folder, has the wrong name, or the line has a typo. |

`[Insecure]` is normal: the service has no encryption and only listens on your own PC.

## 5. Run the bridge and open the dashboard

From the project folder:

| OS | Command |
|---|---|
| Windows | `uv run sctelemetry` (recommended), or `powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1` |
| Linux | `uv run sctelemetry` or `scripts/run.sh` |

On Windows a plain `.\scripts\run.ps1` fails on most PCs, because PowerShell blocks scripts by default; the `-ExecutionPolicy Bypass` form above allows it for that one run. The scripts are thin wrappers: they change to the project folder and pass every option through, so you can start them from anywhere.

The first start takes a little longer:

- uv downloads Python and the libraries (first time only).
- The bridge reads the telemetry schema out of your `StarCitizen.exe` (a few seconds) and caches it per game build in `.tmp/proto/`. After a patch it does this again automatically. The terminal shows `[schema] extracting telemetry schema from ...`.

Open <http://127.0.0.1:8765> in your browser. Live data appears once your character is in the game world (not the main menu). Leave the terminal open; press `Ctrl+C` in it to stop the bridge.

**Sound:** browsers only play audio after a click, so click **Enable sound** in the dashboard header each time you open the page. Without it, alert sounds play from the bridge on your PC instead. Spoken callouts are off by default.

### Finding your install

The bridge looks for the `StarCitizen` folder (the one that holds `LIVE`, `PTU`, ...) in the usual places: `Program Files` and `<drive>:\Roberts Space Industries` / `<drive>:\Games\...` on every Windows drive plus the RSI Launcher's log; on Linux `$WINEPREFIX`, `~/Games/star-citizen`, `~/.wine`, `~/Games/*`, Steam Proton prefixes and Wine prefixes under `/mnt`, `/media` and `/run/media`. The full order is in `sctelemetry/paths.py`.

It picks the channel whose `Game.log` changed last, else `LIVE`. If it guesses wrong, tell it:

```powershell
# Windows
uv run sctelemetry --channel PTU --sc-dir "D:\Games\Roberts Space Industries\StarCitizen"
```

```bash
# Linux
uv run sctelemetry --channel PTU \
  --sc-dir "$HOME/Games/star-citizen/drive_c/Program Files/Roberts Space Industries/StarCitizen"
```

The same works with environment variables `SC_DIR` and `SCTELEMETRY_CHANNEL` (PowerShell: `$env:SC_DIR = "D:\..."`, then run). `SCTELEMETRY_EXE=<path to StarCitizen.exe>` overrides only the schema source.

### Common options

| Option | What it does |
|---|---|
| `--channel PTU` | Which channel to follow: `LIVE`, `PTU`, `HOTFIX`, `EPTU`, `TECH-PREVIEW` |
| `--sc-dir DIR` | Your `StarCitizen` folder, if not found automatically |
| `--record` | Also save the data to `.tmp/recordings/*.jsonl` |
| `--replay FILE.jsonl` | Play back a recording (loops); no game needed |
| `--mock` | Fake data for trying the dashboard; no game needed, but needs one earlier run against a real install (for the cached schema) |
| `--alerts-audio MODE` | Who plays alert sounds: `auto` (default), `browser`, `bridge`, `off` |
| `--show-names` | Show player handles seen in Game.log in the People panel |
| `--lookup-orgs` | Look up those players' orgs on robertsspaceindustries.com (needs `--show-names` on) |
| `--port 8766` | Use another dashboard port |
| `--help` | List every option |

Examples:

```bash
uv run sctelemetry --channel PTU --record
uv run sctelemetry --replay .tmp/recordings/<file>.jsonl
scripts/run.sh --mock            # Linux
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 --mock   # Windows
```

## 6. Optional extras

### Ship capabilities

The dashboard can grey out mining, salvage and refuel controls on ships that cannot use them. That needs a game-data export (`game-ships.json`, and `game-salvage.json` next to it) from an sc-gamedata extractor, a separate tool that is not part of this project. Either:

- put the files in `.tmp/gamedata/<channel>/` inside the project folder, channel in lower case (for example `.tmp/gamedata/ptu/game-ships.json`), or
- point `--gamedata PATH` or the `SCTELEMETRY_GAMEDATA` environment variable at the file or its folder, or
- let the bridge download them: start it with `--gamedata-fetch` (or set `SCTELEMETRY_GAMEDATA_FETCH=1`). It reads your installed build from `<channel>/build_manifest.id` and fetches that exact build's files from the public [sc-gamedata-dumps](https://github.com/voidput/sc-gamedata-dumps) repository into `.tmp/gamedata/<channel>/`. It never uses another build; if the build is not published yet, nothing is downloaded. One-off without starting the bridge: `uv run python -m sctelemetry.gamedata_hub --channel PTU`.

The download is **off by default**: without `--gamedata-fetch` the bridge makes no request for game data. `--gamedata PATH` turns the download off.

Skip it and nothing breaks: capabilities show as unknown and no button is greyed out.

### Custom alerts

Rule toggles in the dashboard's **Alerts** panel are saved to `.tmp/alerts.json`. You can also edit that file by hand (or pass `--alerts PATH`); it holds only your changes to the defaults, for example

```json
{"version": 1, "rules": [{"id": "boost_low", "enabled": false}], "settings": {}}
```

A broken file never turns alerts off: the defaults load and the panel shows the error. Rules, sounds and speech setup: [alerts](telemetry/alerts.md).

### People panel and org lookup (privacy)

The **People** panel lists players whose handles appear in your `Game.log` (crew, party, nearby). Both toggles are **off** by default and can also be switched in the panel:

- **Show names** (`--show-names`): handles are shown. Off: counts only.
- **Look up orgs** (`--lookup-orgs`): the bridge fetches those players' public RSI citizen pages, slowly (one request every 3 seconds at most), and caches the result in `.tmp/rsi-cache.json`. This is the only feature that contacts the internet. Off: no request is ever made.

Names are kept in memory only, never in recordings or logs. Details: [people and lookup](telemetry/people-and-lookup.md).

### Recordings

`--record` writes `.tmp/recordings/<time>.jsonl`. Recordings contain your **aUEC balance** and in-game ids, so do not share them publicly. They are ignored by git.

## 7. Troubleshooting

The dashboard header and the terminal show the bridge's connection status.

| Symptom | Cause and fix |
|---|---|
| Status **connected, waiting for frames** | Working. Data starts once your character is in the world, not at the menu. If it stays like that in the world, `USER.cfg` lacks `server.game.telemetry.enabled = 1` (wrong channel folder?). |
| Status **disconnected (...) - retrying** | The game is not running, still loading, or did not get the environment variables. Check `Game.log` (step 4) and restart the RSI Launcher fully (step 3a). |
| `telemetry schema unavailable ... Searched: ...` | The bridge did not find `StarCitizen.exe`. Pass `--sc-dir` (and `--channel`), or set `SC_DIR` / `SCTELEMETRY_EXE`. |
| `[gamelog] no Star Citizen install found` or `[gamelog] ... not found yet` | Same, for `Game.log` (the first: no install found, pass `--sc-dir`; the second: the game has not written the file yet). The session panel stays empty until the game writes the file. |
| Wrong channel (LIVE vs PTU) | Pass `--channel PTU` (or `LIVE`) or set `SCTELEMETRY_CHANNEL`. `USER.cfg` must be in that same channel folder. |
| Windows: `run.ps1 cannot be loaded because running scripts is disabled` | Use `uv run sctelemetry` instead, or run the script once with `powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1`. For a ZIP download also run `Unblock-File .\scripts\run.ps1`. |
| Windows SmartScreen or antivirus warns about the script | `run.ps1` only runs `uv run sctelemetry`; open it in Notepad to check, or run that command directly. |
| `uv` is not recognized / command not found | Open a new terminal after installing uv. If it persists, sign out and back in. |
| Port in use / address already in use | Another bridge is running (look for another terminal), or something else uses 8765. Use `--port 8766` and open <http://127.0.0.1:8766>. Windows: `netstat -ano \| findstr :8765`; Linux: `ss -ltnp \| grep 8765`. |
| Firewall asks about Python | The bridge only listens on `127.0.0.1` (this PC). Denying network access is fine. |
| No sound | Click **Enable sound** in the dashboard (needed each page load). Check the Alerts panel's audio mode is not `off`, and that the alert is not muted. Use `--alerts-audio browser` to play only in the tab, or check the `[alerts] ... player ...` line in the terminal for bridge playback. |
| Playing on LIVE 4.10: stays **waiting for frames** or **disconnected** after following every step | Expected: the steps are for PTU 5.0, and LIVE 4.10 uses different switches and has no `server.game.telemetry.enabled` ([API spec §2](telemetry/api-spec.md#2-enabling)). Use the PTU, or see the spec for the LIVE switches (untested). |
| Dashboard buttons fail or do nothing | Button presses need `server.game.telemetry.trigger.actions.enabled = 1` in `USER.cfg` and a PTU 5.0 or newer build (LIVE 4.10 has no button presses). |

Still stuck? Open an issue with the terminal output and the `Game.log` lines from step 4. Remove your handle and any paths that contain your user name first.

## 8. Uninstall or disable

1. **Remove the environment variables.**
   - Windows: `setx` cannot delete. Run
     ```powershell
     reg delete HKCU\Environment /F /V GRPC_CLIENT_SERVER_ENABLED
     reg delete HKCU\Environment /F /V GRPC_CLIENT_SERVER_PORT
     ```
     or remove them in **Edit environment variables for your account**. Then fully restart the RSI Launcher.
   - Linux: delete the `export` lines, the Lutris environment entries or the Steam launch options you added, then restart the launcher.
2. **Edit `USER.cfg`:** delete the `server.game.telemetry...` lines (delete the file if you created it only for this).
3. **Delete the project folder.** That removes everything the bridge stored (`.tmp/`: schema cache, recordings, settings, RSI cache).
4. Optional: remove uv and the Python it downloaded (`uv cache clean`, then `uv python uninstall --all`; uv's [uninstall docs](https://docs.astral.sh/uv/getting-started/installation/#uninstallation) list the remaining folders), or `winget uninstall astral-sh.uv` on Windows.
