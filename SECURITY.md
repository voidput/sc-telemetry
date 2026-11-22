# Security policy

sc-telemetry is a local tool. It talks to an **undocumented, unauthenticated** gRPC service that the Star Citizen client can host on your own machine, and relays it to a browser dashboard on the same machine. Read this before changing any bind address.

## Threat model

| Component | Default | Exposure |
|---|---|---|
| Game's `GameTelemetryService` (gRPC) | `127.0.0.1:<GRPC_CLIENT_SERVER_PORT>` | **No TLS, no authentication.** Any local process can read your state and, if `server.game.telemetry.trigger.actions.enabled = 1`, press allowlisted game inputs. |
| sc-telemetry bridge (FastAPI, HTTP + SSE) | `127.0.0.1:8765` | **No TLS, no authentication.** Any local process can read the stream. |

What the bridge does to limit this:

- **Loopback only by default.** Both the game service and the bridge bind to `127.0.0.1`. Never set `GRPC_CLIENT_SERVER_EXTERNAL` (the game itself warns that other computers could then read telemetry and trigger actions), and do not run the bridge with `--host 0.0.0.0` on an untrusted network. If you need remote access, put an authenticated reverse proxy in front.
- **Origin/Host guard** on every mutating or sensitive endpoint (`POST /api/trigger`, alert settings, people settings and cache, `GET /api/session/private`, player names): requests must come from the dashboard's own origin. This blocks cross-site requests (CSRF) and DNS rebinding from web pages you visit.
- **Trigger safety:** only actions in the game's own allowlist can be sent; destructive ones (self-destruct, eject, power or thrusters off, quantum engage) need an explicit `"confirm": true`, and the bridge debounces repeated presses.
- **Privacy defaults:** player handles from `Game.log` are hidden unless `--show-names`; RSI org lookup is off unless `--lookup-orgs`; server address and session ids are never written to recordings, logs or events.

## What never leaves your machine

Telemetry frames, recordings (`.tmp/recordings/*.jsonl`, which contain your wallet balance and entity ids), the extracted schema, `Game.log` content and player handles all stay local. The only outbound network traffic is the **opt-in** RSI org lookup (`--lookup-orgs`), which fetches public citizen pages from `robertsspaceindustries.com` with a fixed User-Agent that carries no personal data. `--mock` never contacts RSI.

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting instead: on the [repository page](https://github.com/voidput/sc-telemetry), open the **Security** tab (direct link: <https://github.com/voidput/sc-telemetry/security/advisories/new>) and choose **Report a vulnerability**. Include the affected endpoint or file, steps to reproduce, and the impact. Do not include real player handles, recordings or logs; synthetic data is enough.

Issues in Star Citizen itself (including the game's gRPC service) should go to Cloud Imperium Games, not here.
