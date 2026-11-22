---
title: "Alerts: toasts, sounds and spoken callouts"
tags: [docs, telemetry]
updated: 2026-11-21
---

# Alerts: toasts, sounds and spoken callouts

> **Speech is off by default (2026-11-21, user choice: no TTS for now).** `settings.speech: false` in `sctelemetry/data/alerts.default.json` strips spoken text at emit time, so neither the bridge nor the browser speaks. Sounds and toasts are unchanged. To enable it, set `"settings": {"speech": true}` in your user alerts file (`.tmp/alerts.json`) and install a TTS engine (see below) or rely on browser voices.

The bridge watches every telemetry frame and every bridge event (Game.log session and signals). When a rule trips, it raises an **alert**. The alert always shows up as a toast and in the log. Sound and speech play in **exactly one place**:

- If a dashboard tab is open **and** you clicked **Enable sound** in it, that tab plays a WebAudio tone, then speaks the callout with the Web Speech API. The bridge stays silent.
- Otherwise the bridge plays it locally: a sound file through the OS player (Windows `winsound`, Linux `pw-play`/`paplay`/`aplay`/`ffplay`, macOS `afplay`), then text-to-speech.

Code: `sctelemetry/alerts.py` (engine, scheduler, router, sinks). Rules: `sctelemetry/data/alerts.default.json`. Wiring: `sctelemetry/server.py`. UI: the Alerts panel, toasts and the header **Enable sound** button in `sctelemetry/static/index.html`. Endpoints: [api-spec §8.6](api-spec.md#86-alerts).

## Pipeline

```
frame (≤20 Hz) ──► AlertEngine ──► alert ─┬─► recent list (/api/alerts) + hub event ALERT (snapshot.events, flight log)
hub event ──────►               (rules)  ├─► SSE `event: alert` {kind:"toast"} to every tab
                                          └─► AudioScheduler ─► AudioRouter ─┬─► browser: SSE {kind:"play", play:true} to ONE tab
                                              (1 at a time, ≥2 s apart,     ├─► bridge: LocalAudioSink (player + TTS)  
                                               priority, critical preempts) └─► none (mode off / browser-only with no tab)
```

- **Startup and vehicle changes never spam.** The first frame, the first 1.5 s after the vehicle changes or appears (`settle_s`), the first frame after a stream gap longer than 3 s, and a replay loop restart all count as a *baseline*. A condition that is already true at a baseline is latched and does not fire until it clears.
- **Gaps re-baseline.** Frames reach the engine only as they arrive, so a stalled stream sends nothing; the first frame after a gap longer than `stale_s` (3 s) is a baseline (above). Hub events older than 30 s (a Game.log backlog) are ignored.
- **Alerts never break telemetry.** The engine runs as a hook on the hub. An exception in it is logged once (`[hub] on_frame hook failed (stream continues)`) and the frame stream carries on. A failing audio tick is logged once (`[alerts] tick failed`) and the next tick runs as usual.
- **Mute** silences sound and speech only. Toasts and the log keep coming. **Test** alerts ignore mute and cooldown, but not mode `off`.
- **Rate limits.** At most one spoken alert starts every `speech_gap_s` (2 s). The queue is ordered by severity, then age. It holds `queue_max` (6) entries and drops anything older than `queue_ttl_s` (10 s). A critical alert cuts off a lower-severity one that is still playing, and it skips the gap unless the previous alert was also critical. When a rule fires that `supersedes` another (for example `boost_empty` over `boost_low`), the weaker rule's queued audio is dropped.

## Audio routing

| `--alerts-audio` / `$SCTELEMETRY_ALERTS_AUDIO` / `PUT /api/alerts/audio` | Who plays |
|---|---|
| `auto` (default) | The newest **live** dashboard tab with sound enabled. If there is none, the bridge. |
| `bridge` | Always the bridge. Tabs show toasts only. |
| `browser` | A live sound-enabled tab. If there is none, nobody (`route.to = "none"`). |
| `off` | Nobody. Toasts only. |

- A tab registers with `POST /api/alerts/audio-client {client_id, enabled, speech}` when you click **Enable sound**. It repeats this every 4 s as a heartbeat and sends `enabled:false` on page hide. It also opens `/events?client=<id>`. The client id is made fresh on every page load and kept in memory only. It is not kept in `sessionStorage`, because **Duplicate tab** copies that and two tabs would share one id.
- A tab counts as **live** only while it is enabled **and** its `/events?client=<id>` stream is open. A play job can only reach a tab through that stream, so a tab with a heartbeat but no stream never takes the audio. When its last stream closes, the bridge takes over at once. A job sent during a reconnect would otherwise be heard nowhere.
- A tab claims the audio (`enabled:true`) only while it can make a sound: its AudioContext is `running` (tones) or the browser has speech voices. It re-checks this on every heartbeat and whenever the AudioContext changes state. If the browser blocks audio, the panel says so and the bridge keeps playing. Turning sound off is retried every 4 s until the bridge confirms it.
- If two tabs have sound enabled, only the one that enabled it most recently plays. If two connections share one client id, only the newest open stream gets `play: true`.
- The runtime mode is not saved. The CLI or env value applies again on restart.
- Browsers need a user gesture before they can play audio. That is why sound has to be enabled with a click each time the page loads.
- Some Linux browsers have no speech voices, for example Firefox or Chromium without speech-dispatcher. The tab then plays tones only and the panel says so. In `auto` mode the tab still wins over the bridge.

## Bridge-local playback

- **Sounds**: settings `sounds.{info,warn,critical}` are path lists, and the first file that exists **and that the player can decode** wins. Defaults: the freedesktop `message-new-instant.oga`, `dialog-warning.oga`, `alarm-clock-elapsed.oga` / `bell.oga` from `/usr/share/sounds/freedesktop/stereo/` (Linux with the sound theme), then the bundled tones `sounds/info.wav`, `sounds/warn.wav`, `sounds/critical.wav`. Relative paths resolve against `sctelemetry/data/`. The tones are generated by `scripts/gen_tones.py` (stdlib sine beeps, committed). A rule's `sound` id can name a custom key, which falls back to the severity's sound. `sound: null` means no sound.
- **Player** (`detect_player`):

  | OS | Order | Formats |
  |---|---|---|
  | Windows | `winsound` (Python stdlib, in-process, async + purge on preempt) | WAV only |
  | macOS | `afplay` | WAV only (the `.oga` defaults are skipped) |
  | Linux | `pw-play` > `paplay` > `aplay -q` > `ffplay -nodisp -autoexit` | `aplay`: WAV only; others: any |

- **TTS backend**: auto-detected in this order:
  1. **piper**, either the `piper` binary or the Python module `piper`, plus a voice model file from `$SCTELEMETRY_PIPER_VOICE` or settings `tts.piper_voice`. Without a voice file, piper is skipped. It synthesises to a temporary WAV, which is then played.
  2. `espeak-ng`.
  3. `espeak`.
  4. Linux/macOS: `spd-say`. Windows: **SAPI** through `powershell` (or `pwsh`) and `System.Speech`; the text goes in on stdin, never on the command line. `tts.rate_wpm` maps to SAPI `Rate` (-10..10, 175 wpm = 0).
  5. None. The bridge logs `no TTS available` once and plays sounds only.
- All playback runs as asyncio subprocesses (winsound: async in-process) and never blocks the event loop. Each step times out after 15 s. A preempting alert kills the current process (winsound: purges the sound).
- `--alerts-dry-run` or `$SCTELEMETRY_ALERTS_DRY_RUN=1` swaps in `DryRunSink`. It logs `[alerts] dry-run would play {...}` to stderr and starts nothing.
- `LocalAudioSink` refuses to start under pytest unless `SCTELEMETRY_ALLOW_AUDIO_TEST=1`.

### TTS setup (choose one; nothing is installed automatically)

| Option | Install | Notes |
|---|---|---|
| espeak-ng (simplest) | Debian/Ubuntu `sudo apt install espeak-ng`; Fedora `sudo dnf install espeak-ng`; Arch `sudo pacman -S espeak-ng`; Windows: the espeak-ng `.msi` from its GitHub releases, then add it to PATH | Small and robotic. Picked up on the next bridge start. |
| speech-dispatcher (Linux) | `apt install speech-dispatcher espeak-ng` / `dnf install speech-dispatcher espeak-ng` / `pacman -S speech-dispatcher espeak-ng` | Gives the bridge `spd-say`, and gives Firefox and Chromium speech voices, so browser speech works too. |
| Windows SAPI | nothing to install (Windows PowerShell ships with Windows) | Used automatically on Windows when no espeak/piper is found. |
| piper (natural voice) | `uv add piper-tts` and download one voice (`.onnx` + `.onnx.json`, about 60 MB) | Optional: a new dependency plus a model download. Then set `SCTELEMETRY_PIPER_VOICE=/path/voice.onnx`. Not yet verified. |

On Windows without a working player or TTS, `--alerts-audio browser` keeps all audio in the dashboard tab (WebAudio + Web Speech).

Without any TTS, the bridge still plays the three severity sounds, and a sound-enabled dashboard still speaks if the browser has voices.

## Rules

**Precedence.** `--alerts PATH` > `$SCTELEMETRY_ALERTS` > `.tmp/alerts.json` (if present) > defaults.

**User file format.** The user file is a **patch**, `{"version": 1, "rules": [{"id": "boost_low", "enabled": false}, {...new rule...}], "settings": {...}}`. Fields are merged into the default with the same id; new ids are added.

**Errors.** A broken or invalid user file never disables alerts. The defaults load instead, and the error shows in `/api/alerts` `config.error` and in the panel. This covers invalid JSON, a file that is not an object, invalid rules, and invalid settings:
- `speech_gap_s` 0..60, `queue_max` 1..50, `queue_ttl_s` 0..600, `settle_s` 0..60, `stale_s` 0.1..600, `event_max_age_s` 0..86400 and `max_eval_hz` 0.1..1e6 must be numbers in range.
- `sounds` must be an object of sound id to a list of paths.
- `tts` must be an object, with `rate_wpm` 80..500 or null and `piper_voice` a string or null.

**Saving.** The file is written only by `PUT /api/alerts/rules`. That covers the panel's per-rule toggles. It is an atomic write of the diff against the defaults:
- The file's own `settings` patch is kept unchanged.
- Default ids left out of the PUT are saved as `"removed": [ids]`, so they stay dropped after a restart.
- A field set to `null` on a default rule unsets it, for example `"when": null` removes the guard. It is saved as `null` in the patch.

Common fields: `id` (`^[a-z0-9_]{1,48}$`), `kind`, `enabled`, `severity` (`info|warn|critical`), `scope` (`any|vehicle|pilot|on_foot`), `cooldown_s`, `debounce_s` (the condition must hold this long), `message` (toast template), `speech` (short and speakable, or `null`), `sound` (id or `null`), `verified`, `description`, `when` (a guard condition), `supersedes` (rule ids), `on_entry`.

**Conditions.** A condition is `{path, op, value}`, `{all: [...]}`, `{any: [...]}` or `{not: c}`.
- `path` is a dotted frame path, or a `$derived` value: `$oxygen` (current / max), `$shield_faces_down`, `$quantum_seen`.
- `op` is one of `< <= > >= == != in`.
- `==` also matches enum suffixes, so `"DEPLOYED"` matches `VEHICLE_COMPONENT_DEPLOYMENT_STATE_DEPLOYED`.
- `FLT_MAX` sentinels and NaN read as missing. Missing data never fires a rule.

**Templates.** Templates can use `{value}`, `{pct}` (0..100), `{pct_words}`, and, on event rules, the event's `{detail}`, `{count}`, `{class}`, `{kind}` and `{text}`. Simple format specs work, for example `{value:.0f}` or `{count:>3}`: an optional alignment, a width of at most 2 digits, a precision of at most 2 digits, and one of `d f s %`. Anything else is rejected, including attribute or index access (`{value.x}`, `{value[0]}`), unknown fields, and stray braces. Speech uses `{pct_words}`, so the voice says the same number as the toast. **Test** alerts fill event fields with sample values (`count` 3, `detail` "example").

`detail_re` is limited to 200 characters. Bridge TTS gets the text after `--`, so a speech text that starts with `-` is never read as a command-line option.

| kind | fires when | re-arms when |
|---|---|---|
| `threshold` | `value op threshold` becomes true | value crosses `rearm` (hysteresis), else the condition is false |
| `transition` | value enters the `to` set (optionally coming from a `from` value): `ANNUNCIATOR_*` → `MAJOR_ON`/`MAJOR_FLASHING` | value leaves `to` |
| `edge` | boolean becomes `to` | boolean flips back |
| `composite` | `all`/`any`/`not` becomes true | condition false |
| `event` | hub event `type` matches (string or list), plus optional `detail_re` regex and `match` field equality | cooldown only |

`when` gates a rule. While the guard is false, the rule's current state becomes its baseline, so it never fires on the value it sees when the guard turns true again. For example, shields charging from 0 after power-on is not "shields low".

`on_entry` lets an out-of-scope → in-scope change fire, instead of baselining. For example, stepping out into vacuum. The very first frame is never an entry.

A cooldown never drops an alert. If the condition still holds when the cooldown ends, the alert fires then.

### Default rules

`verified` means the rule's data source was observed changing in a recording. **unverified** rules are reasonable guesses.

| id | trigger | sev | speech | notes |
|---|---|---|---|---|
| boost_low | boost reserves ≤ 25 %, re-arm 35 % | warn | "Boost {pct_words} percent" | verified |
| boost_empty | boost reserves ≤ 1 %, re-arm 10 % | warn | "Boost depleted" | verified; supersedes boost_low. **`boost.critical` is not used**: it turns true at about 25 % on RSI_Perseus and stays true until reserves recover, so it is not "empty" |
| hydrogen_low / hydrogen_critical | ≤ 20 % / ≤ 10 %, debounce 2 s, only while `power.engines` | warn / crit | "Hydrogen {pct_words} percent" / "Hydrogen critical" | verified: hydrogen **reads 0 with engines off** and glitches to 0 for single frames |
| quantum_low | ≤ 15 % once quantum has read > 0 on this vehicle | warn | "Quantum fuel low" | **unverified** (quantum fuel was always 0 on the observed ships) |
| shields_low | level ≤ 25 %, re-arm 40 %, while generators and emitters are on | warn | "Shields low" | verified (combat recording); deliberate shields-off and power-on charging stay silent |
| shield_face_down | a face ≤ 0.01 while level > 5 % and the face's tuning share > 5 % | warn | "Shield face down" | **unverified**; respects tuning: the share is learned at full charge, so a face tuned to 0.6 is not DOWN |
| shield_emitters_lost | emitters off while generators are on, debounce 1 s | crit | "Shield emitters offline" | **unverified**; a shields-off toggle drops both, so it stays silent |
| hull_half / hull_critical | hull ≤ 50 % / ≤ 25 % | warn / crit | "Hull fifty percent" / "Hull critical" | **unverified** |
| missile_warning | `misl` → MAJOR | crit | "Missile warning" | verified (combat) |
| hit_warning | `hit` → MAJOR, cooldown 15 s | warn | "Taking hits" | verified (combat) |
| proximity_warning | `prox` → MAJOR, cooldown 10 s | warn | "Proximity" | verified |
| oxygen_low | oxygen current/max ≤ 25 % | crit | "Oxygen low" | **unverified** |
| health_low | vitals health ≤ 30 while > 0 | crit | "Health low" | verified (health observed changing). Death drops it from about 96 to 0 in one frame in two recordings, so `when: health > 0` keeps the death silent. A death is not "health low" |
| radiation | ambient radiation > 0 for 2 s | warn | "Radiation detected" | **unverified** |
| unbreathable | breathable_atmosphere → false on foot (also on stepping out) | warn | "Unbreathable atmosphere" | **unverified** |
| gear_overspeed | gear DEPLOYED, speed > 120 m/s, not landed, debounce 1 s | warn | "Gear down, overspeed" | verified |
| g_load | g_meter.current ≥ 9 with G-safe inactive | warn | "G load" | verified |
| leaving_armistice | ZONE event "Leaving … Armistice" | info | "Leaving armistice" | verified (Game.log) |
| session_join | SESSION_JOIN / SESSION_SHARD_CHANGE | info | none, no sound | toast only |
| qt_arrived | QT_ARRIVED with `self: true` | info | "Arrived" | verified (Game.log) |
| corpse | CORPSE_ITEMS | crit | "Corpse marked" | **unverified** as a death signal |

## Replay results (default rules)

| Recording | Alerts |
|---|---|
| `20261121-150112` Perseus flight | boost_low 1, boost_empty 2, proximity 3, gear_overspeed 1, g_load 1 |
| `20261121-165507` Paladin shields + tuning | boost_low 1, boost_empty 1; no shields_low and no face DOWN during power-on charge-up or 80/160 and 60/220 tuning |
| `20261121-160738` combat | hit 9, prox 9, missile 6, boost_low 2, shields_low 2, boost_empty 1 (no health_low: the one drop to 0 was the death) |

`tests/test_alerts.py::test_replay_recordings_sensible_counts` pins the first two results. The test is skipped when the local recordings are missing.

## Toasts

- Info toasts clear after 6 s and warnings after 10 s. Critical toasts show **ACK**, are kept when the stack overflows, and clear after 60 s, because the pilot is in the game and may never click them. The Recent list keeps the history.
- When two or more toasts are up, an **ACK ALL** button appears. **Escape** also clears them all, unless it is cancelling an armed control button.
- The stack holds 5 toasts, or 3 at phone width (≤ 520 px, bottom-anchored).
- Tabs dedupe toasts by `boot:id`. `boot` changes with every bridge process, so alert ids that restart at 1 after a bridge restart still show up in a tab left open. A stopped bridge closes its SSE streams after 2 s (`timeout_graceful_shutdown`), so open tabs reconnect to the new process instead of staying on the old one.

## Other clients (button panels, overlays)

Subscribe to `/events` and listen for the named `alert` event. Pass `?client=<id>` to receive `play: true` jobs after registering through `POST /api/alerts/audio-client`. Without a client id, a subscriber gets toasts only (`kind: "toast"`) and the play jobs with `play: false`. Dedupe on `alert.boot` plus `alert.id`. Keep one stream per client id, because only the newest one plays.
