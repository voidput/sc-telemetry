"""Generate docs/telemetry/api-reference.md from the runtime-extracted schema (scgameapi/schema.py) + an optional recording.

usage:
  uv run python scripts/gen_api_spec.py [--recording .tmp/recordings/X.jsonl] [--out docs/telemetry/api-reference.md]

Message/enum/field tables come straight from the .proto (exact). "Observed" comes from a
recorded session (JSONL {"t","frame"}) and is evidence, not a contract. NOTES holds
hand-curated units/semantics — edit it, not the generated file.
"""
import argparse
import collections
import json
import math
import pathlib
import re
import subprocess
import sys
import tempfile

from google.protobuf import descriptor_pb2 as dpb

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scgameapi import schema  # noqa: E402

PROTO = schema.resolve()
PKG = "sc.game.telemetry.v1"
FLT_MAX = 3.4028234e38
_FAKE_ID = re.compile(r"_\d{9,}$")

# (Message, field) -> units / semantics. Keep terse; "obs" = established from recordings.
NOTES = {
    ("TelemetryStreamResponse", "vehicle"): "Absent while not in/operating a vehicle.",
    ("TelemetryStreamResponse", "weapons"): "Vehicle weapons; absent on foot.",
    ("TelemetryStreamResponse", "events"): "Present only on frames that carry events.",
    ("TelemetryStreamResponse", "actor"): "Local player. Frames stream only once the local player exists (after world load).",
    ("TelemetryStreamResponse", "force_feedback"): "Not seen in PTU 5.0 recordings.",
    ("TelemetryStreamResponse", "turret"): "Expected only while manning a turret; not seen in recordings.",
    ("TriggerGameActionRequest", "action_name"): "Must be a name returned by ListTriggerableActions.",
    ("TriggerGameActionRequest", "hold_ms"): "Hold duration; only for actions with holdable=true, else HOLD_NOT_ALLOWED.",
    ("TriggerableAction", "category"): "Action map/category name.",
    ("TriggerableAction", "holdable"): "Accepts hold_ms (responds to press and release).",
    ("Gauge", "current"): "Current value, same scale as max.",
    ("Gauge", "max"): "Scale maximum (see the owning field for meaning).",
    ("Availability", "enabled"): "Feature switched on by the pilot / available.",
    ("Availability", "active"): "Feature currently acting (e.g. G-safe enabled but not limiting = enabled, !active).",
    ("ActorVitals", "health"): "0..100.",
    ("ActorVitals", "oxygen"): "0..1 (max = 1) — different scale from other vitals.",
    ("ActorVitals", "hunger"): "0..100, 100 = satiated.",
    ("ActorVitals", "thirst"): "0..100, 100 = hydrated.",
    ("ActorVitals", "body_temperature"): "current in °C (36.5 nominal). max reported as 100 — not a meaningful limit.",
    ("ActorVitals", "heart_rate"): "Beats per minute.",
    ("ActorVitals", "gravity"): "G-load on the body (1.0 at rest in 1 g).",
    ("ActorVitals", "pressure"): "Ambient pressure, 1.0 = 1 atm (assumed).",
    ("ActorVitals", "intoxication_level"): "Integer level, 0 = sober.",
    ("ActorEnvironment", "external_temperature"): "°C.",
    ("ActorEnvironment", "ambient_radiation_rate"): "Radiation dose rate; 0 when none.",
    ("ActorState", "seat_name"): "Seat entity name incl. trailing entity id, e.g. RSI_Perseus_Pilot_SeatDashboard_000000000002.",
    ("ActorState", "uec_balance"): "Wallet in aUEC. int64 → JSON string.",
    ("VehicleState", "name"): "Vehicle entity name incl. trailing entity id, e.g. RSI_Perseus_000000000001.",
    ("VehicleState", "class_name"): "Vehicle class, e.g. RSI_Perseus.",
    ("VehicleState", "manufacturer"): "Empty in PTU 5.0 recordings.",
    ("VehicleState", "health"): "Hull health 0..1. PTU 5.0+.",
    ("VehicleState", "shields"): "PTU 5.0+.",
    ("VehicleState", "master_mode"): "PTU 5.0+.",
    ("VehicleShields", "level"): "Overall shield 0..1; 0 when emitters/generators unpowered.",
    ("VehicleShields", "faces"): "Per-face 0..1. 4 entries on Perseus; face order undocumented.",
    ("VehicleFlightInstruments", "speed"): "m/s.",
    ("VehicleFlightInstruments", "hydrogen_fuel"): "0..1 fraction.",
    ("VehicleFlightInstruments", "quantum_fuel"): "0..1 fraction (0 throughout recordings).",
    ("VehicleFlightInstruments", "g_meter"): "current = G now; max = session PEAK G (only rises), not a limit.",
    ("VehicleFlightInstruments", "altitude"): "m. FLT_MAX (3.4028235e38) when no reference body — treat abs(v) > 1e30 as absent.",
    ("VehicleFlightInstruments", "radar_altitude"): "m above terrain. FLT_MAX sentinel as altitude.",
    ("VehicleFlightInstruments", "vertical_speed_indicator"): "m/s, + = climbing.",
    ("VehicleFlightInstruments", "atmospheric_density"): "kg/m³ (≈1.2 at sea level).",
    ("VehicleFlightInstruments", "heading"): "Degrees 0..360.",
    ("VehicleThrusterBoost", "output"): "Current boost output 0..1.",
    ("VehicleThrusterBoost", "reserves"): "Boost reserve 0..1.",
    ("VehicleThrusterBoost", "critical"): "Reserves critically low.",
    ("VehicleFlightState", "gear"): "Landing gear; unset if vehicle has none.",
    ("VehicleFlightState", "vtol"): "VTOL thrusters; unset if none.",
    ("VehicleFlightState", "transformation"): "Transforming vehicles; unset if none.",
    ("VehicleMasterModeState", "spooled"): "Mode switch completed (spool-up finished).",
    ("VehiclePowerState", "powerplants"): "On/off only — no pip/allocation data exists in the API.",
    ("VehicleAnnunciator", "pwr"): "Power", ("VehicleAnnunciator", "wpn"): "Weapons",
    ("VehicleAnnunciator", "thr"): "Thrusters", ("VehicleAnnunciator", "shld"): "Shields",
    ("VehicleAnnunciator", "cool"): "Coolers", ("VehicleAnnunciator", "qdrv"): "Quantum drive",
    ("VehicleAnnunciator", "radr"): "Radar", ("VehicleAnnunciator", "prox"): "Proximity",
    ("VehicleAnnunciator", "hit"): "Hit/damage", ("VehicleAnnunciator", "misl"): "Missile lock/warning",
    ("WeaponsFrame", "preset_name"): "Empty in recordings.",
    ("WeaponState", "name"): "Weapon entity name incl. trailing entity id, e.g. GATS_BallisticGatling_S3_000000000003.",
    ("Countermeasures", "amount"): "Remaining count.",
    ("ForceFeedbackFrame", "gunfire"): "Gunfire rumble intensity (assumed 0..1).",
    ("ForceFeedbackFrame", "stick_shaker"): "Stall/G warning shaker intensity (assumed 0..1).",
    ("AxisFeedback", "resistance"): "Stick spring resistance (assumed 0..1).",
    ("AxisFeedback", "damping"): "Stick damping (assumed 0..1).",
    ("TurretFrame", "light_amplification"): "Night vision active.",
}

# leaf field names whose observed values are private (wallet balance): shown as <redacted>
SENSITIVE = frozenset({"uec_balance"})

SCALAR = {1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32", 6: "fixed64", 7: "fixed32", 8: "bool",
          9: "string", 12: "bytes", 13: "uint32", 15: "sfixed32", 16: "sfixed64", 17: "sint32", 18: "sint64"}


def load_descriptors():
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "set.pb"
        protos = [p.relative_to(PROTO).as_posix() for p in PROTO.rglob("*.proto")]
        subprocess.run([sys.executable, "-m", "grpc_tools.protoc", f"-I{PROTO}", f"--descriptor_set_out={out}",
                        "--include_imports", *protos], check=True)
        fds = dpb.FileDescriptorSet.FromString(out.read_bytes())
    msgs, enums, services = {}, {}, []

    def reg_msg(m, prefix):
        full = f"{prefix}.{m.name}"
        msgs[full] = m
        for e in m.enum_type:
            enums[f"{full}.{e.name}"] = e
        for n in m.nested_type:
            if not n.options.map_entry:
                reg_msg(n, full)

    order = []
    for f in fds.file:
        for e in f.enum_type:
            enums[f".{f.package}.{e.name}"] = e
        for m in f.message_type:
            reg_msg(m, f".{f.package}")
            order.append((f.name, f".{f.package}.{m.name}"))
        services.extend(f.service)
    return msgs, enums, services, fds


def short(n):
    return n.removeprefix(f".{PKG}.")


def ftype(f):
    if f.type in (11, 14):
        return short(f.type_name)
    return SCALAR[f.type]


def observe(path):
    """path stats keyed by dotted snake_case JSON path (lists as [])."""
    st = collections.defaultdict(lambda: {"n": 0, "min": math.inf, "max": -math.inf, "vals": collections.Counter(), "sent": 0})
    present = collections.Counter()
    lens = collections.defaultdict(set)
    frames = 0
    t0 = t1 = None

    def walk(o, p):
        if isinstance(o, dict):
            present[p] += 1
            for k, v in o.items():
                walk(v, f"{p}.{k}" if p else k)
        elif isinstance(o, list):
            lens[p].add(len(o))
            for v in o:
                walk(v, f"{p}[]")
        else:
            s = st[p]
            s["n"] += 1
            if p.rsplit(".", 1)[-1].removesuffix("[]") in SENSITIVE:  # private values never reach the docs
                s["vals"]["<redacted>"] += 1
                return
            if isinstance(o, (int, float)) and not isinstance(o, bool):
                if abs(o) > 1e30:
                    s["sent"] += 1
                else:
                    s["min"] = min(s["min"], o)
                    s["max"] = max(s["max"], o)
            v = _FAKE_ID.sub("_000000000001", str(o))  # never publish real entity ids from a recording
            if len(s["vals"]) < 8 or v in s["vals"]:
                s["vals"][v[:48]] += 1

    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        frames += 1
        t0 = t0 or r["t"]
        t1 = r["t"]
        walk(r["frame"], "")
    return {"frames": frames, "span": (t1 or 0) - (t0 or 0), "st": st, "present": present, "lens": lens}


def fmt_obs(obs, path, f):
    if not obs:
        return ""
    st, lens = obs["st"], obs["lens"]
    frames = obs["frames"]
    if f.type == 11:  # message: presence ratio
        p = obs["present"].get(path, 0) + sum(v for k, v in obs["present"].items() if k == path + "[]")
        if f.label == 3:
            l = sorted(lens.get(path, []))
            return f"len {', '.join(map(str, l))}" if l else "not seen"
        return f"{100 * p / frames:.0f}% of frames" if p else "not seen"
    key = path + ("[]" if f.label == 3 else "")
    s = st.get(key)
    if not s or not s["n"]:
        return "not seen"
    parts = []
    if s["min"] != math.inf:
        parts.append(f"{s['min']:.4g}" if s["min"] == s["max"] else f"{s['min']:.4g} … {s['max']:.4g}")
    if s["sent"]:
        parts.append(f"FLT_MAX ×{s['sent']}")
    if s["min"] == math.inf and not s["sent"]:
        vals = [v.removeprefix(ftype(f).upper()) for v, _ in s["vals"].most_common(4)]
        parts.append(", ".join(f"`{v}`" if v else "`\"\"`" for v in vals))
    return "; ".join(parts)


def paths_by_message(msgs, root):
    """Map message full name -> list of JSON paths where it occurs (walk from root)."""
    out = collections.defaultdict(list)

    def go(mname, path, depth):
        out[mname].append(path)
        if depth > 8:
            return
        for f in msgs[mname].field:
            if f.type == 11 and f.type_name in msgs:
                go(f.type_name, f"{path}.{f.name}" if path else f.name, depth + 1)

    go(root, "", 0)
    return out


def anchor(name):
    return name.lower().replace(".", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording")
    ap.add_argument("--out", default=str(ROOT / "docs/telemetry/api-reference.md"))
    a = ap.parse_args()

    msgs, enums, services, fds = load_descriptors()
    obs = observe(a.recording) if a.recording else None
    root = f".{PKG}.TelemetryStreamResponse"
    where = paths_by_message(msgs, root)

    L = ["# Game Telemetry API reference (generated)", ""]
    L += ["> Generated by `scripts/gen_api_spec.py` from the schema extracted from the installed game (`scgameapi/schema.py`) — do not edit by hand; edit `NOTES` in the script.",
          "> Narrative spec (transport, enabling, semantics): [api-spec.md](api-spec.md).", ""]
    if obs:
        L += [f"Observed column: {obs['frames']} frames over {obs['span']:.0f} s from "
              f"`{pathlib.Path(a.recording).name}` (PTU 5.0, Perseus flight). Evidence, not contract.", ""]
    files = ", ".join(f"`{f.name}`" for f in fds.file)
    L += [f"Package `{PKG}` · files {files}", ""]

    L += ["## Service", ""]
    for s in services:
        L += [f"### `{s.name}`", "", "| RPC | Request | Response | Kind |", "|---|---|---|---|"]
        for m in s.method:
            kind = ("bidi stream" if m.client_streaming and m.server_streaming else
                    "server stream" if m.server_streaming else "client stream" if m.client_streaming else "unary")
            L.append(f"| `{m.name}` | [{short(m.input_type)}](#{anchor(short(m.input_type))}) | "
                     f"[{short(m.output_type)}](#{anchor(short(m.output_type))}) | {kind} |")
        L += ["", f"Full method paths: " + ", ".join(f"`/{PKG}.{s.name}/{m.name}`" for m in s.method), ""]

    L += ["## Messages", ""]
    for full, m in msgs.items():
        name = short(full)
        L += [f"### {name}", ""]
        occ = [p for p in where.get(full, []) if p]
        if occ:
            L += ["JSON path: " + ", ".join(f"`{p}`" for p in occ[:4]), ""]
        if not m.field:
            L += ["_Empty message._", ""]
            continue
        cols = "| # | Field | Type | Label | Notes |" + (" Observed |" if obs else "")
        L += [cols, "|---|---|---|---|---|" + ("---|" if obs else "")]
        for f in sorted(m.field, key=lambda x: x.number):
            t = ftype(f)
            tl = f"[{t}](#{anchor(t)})" if f.type in (11, 14) else f"`{t}`"
            label = "repeated" if f.label == 3 else ("optional" if f.proto3_optional else "")
            note = NOTES.get((name.split(".")[-1], f.name), "").replace("|", "\\|")
            row = f"| {f.number} | `{f.name}` | {tl} | {label} | {note} |"
            if obs:
                ob = " · ".join(dict.fromkeys(filter(None, (fmt_obs(obs, f"{p}.{f.name}" if p else f.name, f) for p in where.get(full, [])))))
                row += f" {ob or '—'} |"
            L.append(row)
        nums = sorted(f.number for f in m.field)
        gaps = [n for n in range(1, nums[-1]) if n not in nums]
        if gaps:
            L += ["", f"Unused field numbers: {', '.join(map(str, gaps))} (likely removed/reserved)."]
        L.append("")

    L += ["## Enums", ""]
    for full, e in enums.items():
        L += [f"### {short(full)}", "", "| Value | Number |", "|---|---|"]
        L += [f"| `{v.name}` | {v.number} |" for v in e.value]
        L.append("")

    pathlib.Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {a.out}: {len(msgs)} messages, {len(enums)} enums, {sum(len(s.method) for s in services)} rpcs")


main()
