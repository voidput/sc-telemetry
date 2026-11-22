---
title: scgameapi vault
tags: [docs]
updated: 2026-11-21
---

# scgameapi vault

## Telemetry
- [[api-spec]] — ([open](telemetry/api-spec.md)) — full API spec: transport, enabling, RPCs, read/write model, semantics, security, versioning
- [[api-reference]] — ([open](telemetry/api-reference.md)) — generated message/field/enum reference with observed ranges
- [[triggerable-actions]] — ([open](telemetry/triggerable-actions.md)) — the 112-action write allowlist, with destructive actions flagged
- [[action-state-map]] — ([open](telemetry/action-state-map.md)) — action → telemetry state map: live state on buttons, lit rules, capability gating, shield face layouts
- [[alerts]] — ([open](telemetry/alerts.md)) — alert engine: rules (boost/fuel/shields/hull/annunciators/vitals/Game.log), toasts, sound + speech routed to one place (dashboard tab or bridge), TTS setup
- [[people-and-lookup]] — ([open](telemetry/people-and-lookup.md)) — People panel: player handles from Game.log (crew/party/nearby), Show names + Look up orgs toggles, RSI cache, privacy
- [[gamelog-signals]] — ([open](telemetry/gamelog-signals.md)) — what Game.log offers beyond the API: nearby ship classes, QT arrivals, zone/armistice notices, vehicle control, death
- [[gamelog-catalog]] — ([open](telemetry/gamelog-catalog.md)) — Game.log event catalog from ~290 logs: per-domain templates and fields, lifecycles, LIVE 4.x vs PTU 5.0 drift, noise, gaps
- [[gamelog-parser-plan]] — ([open](telemetry/gamelog-parser-plan.md)) — ranked parser plan for the bridge tailer: foundations, pass 2, pass 3, privacy rules
- [[game-telemetry-api]] — ([open](telemetry/game-telemetry-api.md)) — SC client's local gRPC telemetry service: schema, data fields, how to enable, LIVE vs PTU

## Plans
- [[ideas]] — ([open](plans/later/ideas.md)) — ice box: Rust port, StarOrg Codex integration (Rust home), StarOrg shared org telemetry

## Meta
- [[changelog]] — ([open](changelog.md)) — dated record of changes
