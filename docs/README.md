---
title: scgameapi docs
tags: [docs]
updated: 2026-11-21
---

# scgameapi docs

Reverse-engineered notes on Star Citizen's local client telemetry API.

## Telemetry
- [API spec](telemetry/api-spec.md) — transport, enabling, RPCs, read/write model, semantics, security, versioning
- [API reference (generated)](telemetry/api-reference.md) — every message/field/enum + observed ranges
- [Triggerable actions](telemetry/triggerable-actions.md) — the 112-action write allowlist, with destructive actions flagged
- [Action state map](telemetry/action-state-map.md) — action → telemetry state map: live state on buttons, lit rules, capability gating, shield face layouts
- [Alerts](telemetry/alerts.md) — alert rules, toasts, sound + spoken callouts routed to one place (dashboard tab or bridge), TTS setup
- [People and org lookup](telemetry/people-and-lookup.md) — player handles seen in Game.log behind Show names, opt-in RSI org lookup with cache, privacy model
- [Game.log signals](telemetry/gamelog-signals.md) — what Game.log offers beyond the API: nearby ship classes, QT arrivals, zone/armistice notices
- [Game.log event catalog](telemetry/gamelog-catalog.md) — every useful line from ~290 logs: templates, fields, lifecycles, LIVE 4.x vs PTU 5.0 drift, noise, gaps
- [Game.log parser plan](telemetry/gamelog-parser-plan.md) — ranked parsers to add to the bridge tailer, with privacy handling
- [Game Telemetry API](telemetry/game-telemetry-api.md) — local gRPC `GameTelemetryService`: schema, data fields, enable switches, LIVE vs PTU diff

## Plans
- [Ideas (ice box)](plans/later/ideas.md) — Rust port, StarOrg Codex integration (Rust home), StarOrg shared org telemetry

## Code assets
- [`scgameapi/schema.py`](../scgameapi/schema.py) — runtime schema extraction from the installed `StarCitizen.exe`, cached per build (no CIG-derived files committed)
- [`scripts/gen_api_spec.py`](../scripts/gen_api_spec.py) — regenerate api-reference.md from proto + a recording
- [`scripts/extract_protos.py`](../scripts/extract_protos.py) — manual extraction from any StarCitizen.exe (normally automatic via `scgameapi/schema.py`)
- [`scripts/obsidian_frontmatter.py`](../scripts/obsidian_frontmatter.py) — vault frontmatter generator

## Meta
- [Changelog](changelog.md)
