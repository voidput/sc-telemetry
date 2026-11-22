"""SC telemetry bridge: gRPC TelemetryStream -> SSE -> browser dashboard, plus guarded
TriggerGameAction (POST /api/trigger). --mock/--replay use a fake trigger backend.

usage:
  uv run python -m scgameapi.server [--grpc 127.0.0.1:50051] [--port 8765] [--mock] [--record]
  uv run python -m scgameapi.server --replay .tmp/recordings/X.jsonl [--replay-speed 1.0]
session context (shard/region/system) comes from Game.log: --game-log PATH | --channel PTU [--sc-dir DIR]
| --no-game-log. --mock uses a synthetic session; --replay reads Game.log only with --game-log.
alerts (docs/telemetry/alerts.md): --alerts PATH (user rules patch) --alerts-audio auto|bridge|browser|off
--alerts-dry-run (log what would play, never start a player).
people (docs/telemetry/people-and-lookup.md): --show-names ($SCGAMEAPI_SHOW_NAMES=1) shows player handles seen
in Game.log; --lookup-orgs ($SCGAMEAPI_LOOKUP_ORGS=1) looks their orgs up on robertsspaceindustries.com. Both off
by default and switchable at runtime (PUT /api/people/settings).
"""
import argparse
import asyncio
import collections
import contextlib
import json
import math
import os
import pathlib
import re
import sys
import time
import traceback

import grpc
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from google.protobuf import json_format

from . import alerts, gamelog, mock, paths, people, shipcaps, stubs

STATIC = pathlib.Path(__file__).parent / "static"
DATA = pathlib.Path(__file__).parent / "data"
REC_DIR = stubs.ROOT / ".tmp" / "recordings"
ALLOWLIST_JSON = stubs.ROOT / ".tmp" / "triggerable-actions.json"
ALERTS_USER_JSON = stubs.ROOT / ".tmp" / "alerts.json"   # default user rules patch (only written by PUT)
RSI_CACHE_JSON = stubs.ROOT / ".tmp" / "rsi-cache.json"  # opt-in org lookup cache (handles + orgs; gitignored)
ALERTS_TICK_S = 0.1
JSON_BODY_MAX = 64 * 1024
PUSH_HZ = 20
SAMPLE_HZ = 4
HISTORY_S = 180
STALE_S = 3.0          # no frame for longer than this = stale (sampler records nulls)
TRIGGER_DEBOUNCE_S = 1.0  # game debounce is < 1 s: two presses 1 s apart both fire (toggle cancels)
TRIGGER_BODY_MAX = 1024   # bytes; a trigger body is ~60
TRIGGER_TIMEOUT_S = 5
ACTIONS_CACHE_S = 30
ACTION_RE = re.compile(r"^[a-z0-9_]{1,64}$")
STATUS_PREFIX = "TRIGGER_GAME_ACTION_STATUS_"
# irreversible / ship-losing actions: bridge refuses them unless the request carries "confirm": true.
# v_power_toggle powers the whole ship off when it is on (same as v_power_set_off); the thruster-off
# pair kills flight control in flight, so all of them need the same two-step confirm.
DANGEROUS = frozenset({"v_self_destruct", "v_eject", "v_emergency_exit", "v_power_set_off",
                       "v_power_toggle", "v_power_set_thrusters_off", "v_power_toggle_thrusters",
                       "v_toggle_qdrive_engagement"})

# series name -> path into the frame dict
SERIES = {
    "speed": ("vehicle", "state", "flight", "instruments", "speed"),
    "g": ("vehicle", "state", "flight", "instruments", "g_meter", "current"),
    "heart_rate": ("actor", "vitals", "heart_rate"),
    "shield": ("vehicle", "state", "shields", "level"),
    "boost": ("vehicle", "state", "flight", "instruments", "boost", "reserves"),
    "vsi": ("vehicle", "state", "flight", "instruments", "vertical_speed_indicator"),
    "hydrogen": ("vehicle", "state", "flight", "instruments", "hydrogen_fuel"),
    "hull": ("vehicle", "state", "health"),
}


def _num(d, path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    if isinstance(d, bool) or not isinstance(d, (int, float)) or abs(d) > 1e30 or d != d:
        return None  # absent, non-numeric, FLT_MAX sentinel or NaN
    return round(float(d), 4)


def extract_sample(frame: dict | None) -> dict:
    return {k: _num(frame, p) if frame else None for k, p in SERIES.items()}


class Hub:
    def __init__(self):
        self.frame: dict | None = None
        self.seq = 0
        self.status = "starting"
        self.source = ""
        self.frames_total = 0
        self.last_frame_at = 0.0
        self.events = collections.deque(maxlen=50)
        self._rate = collections.deque(maxlen=100)
        self._ev_prev: collections.Counter = collections.Counter()  # event types in the previous frame
        self._ev_id = 0
        self.session: dict | None = None  # Game.log session context (None = disabled); holds private fields
        self.on_session = None            # optional hook(session) after each session change
        self.log_age = None               # optional callable -> seconds since Game.log was last written
        self.signals: gamelog.SignalsState | None = None  # Game.log signals (None = disabled)
        self.on_signals = None            # optional hook(public signals) after each session/signals change
        self.shipcaps: shipcaps.ShipCaps | None = None  # game-data capability table (None = not loaded)
        self.on_frame = None              # optional hook(frame) after each pushed frame (alert engine)
        self.on_event = None              # optional hook(event) for each new event except ALERT
        self.people: people.PeopleState | None = None  # handles seen in Game.log (None = disabled)
        self.people_settings = {"show_names": False, "lookup_orgs": False}  # runtime toggles (not persisted)
        self.orgs: people.OrgLookup | None = None       # opt-in RSI lookup (None = not started)
        self._hook_errors: set[str] = set()  # hook failures already logged (once per hook + error type)

    def _call_hook(self, name: str, fn, arg):
        """Side-output hooks (alert engine) never break the telemetry stream: errors are logged once."""
        try:
            fn(arg)
        except Exception as e:  # noqa: BLE001
            key = f"{name}:{type(e).__name__}"
            if key not in self._hook_errors:
                self._hook_errors.add(key)
                print(f"[hub] {name} hook failed (stream continues): {type(e).__name__}: {e}", file=sys.stderr,
                      flush=True)

    def add_event(self, typ: str, t: float | None = None, detail: str | None = None, /, **extra):
        """Bridge-side event (not from a frame), e.g. SESSION_JOIN / ZONE from Game.log. `extra` holds
        the typed fields of signal events (class, self, count, kind, text, classes): never ids."""
        self._ev_id += 1
        ev = {"id": self._ev_id, "t": t if t is not None else time.time(), "type": typ}
        if detail:
            ev["detail"] = detail
        ev.update(extra)
        self.events.appendleft(ev)
        if self.on_event and typ != "ALERT":
            self._call_hook("on_event", self.on_event, ev)

    def set_session(self, session: dict | None, events=()):
        self.session = session
        for e in events:
            self.add_event(e["type"], e.get("t"), e.get("detail"),
                           **{k: v for k, v in e.items() if k not in ("type", "t", "detail")})
        self.seq += 1  # wake SSE so the change reaches clients without waiting for a frame
        if self.on_session:
            self.on_session(session)
        if self.on_signals and self.signals is not None:
            self.on_signals(self.public_signals())

    def public_signals(self) -> dict | None:
        """Nearby ships / zone for snapshot, SSE, /api/signals and --record: classes only, no ids."""
        return self.signals.public() if self.signals is not None else None

    def signals_view(self) -> dict | None:
        """public_signals() + per-class game-data lookup info (`info`: manufacturer, role, size, crew, cargo)
        for snapshot / SSE / /api/signals. Local dataset only, no network; --record keeps the plain view."""
        sig = self.public_signals()
        if sig is None:
            return None
        info = self.shipcaps.info if self.shipcaps is not None else (lambda _c: None)
        for n in sig.get("nearby") or []:
            n["info"] = info(n.get("class"))
        if sig.get("own_ship"):
            sig["own_ship"]["info"] = info(sig["own_ship"].get("class"))
        return sig

    def people_view(self, names_ok: bool = False) -> dict | None:
        """People block. Counts always; the roster (handles) only when Show names is on AND the caller
        passed the Origin/Host guard (names_ok). Org info only while Look up orgs is on."""
        if self.people is None:
            return None
        st = self.people_settings
        names = bool(st["show_names"] and names_ok)
        orgs = self.orgs.get if (self.orgs is not None and st["lookup_orgs"]) else None
        return {**self.people.public(names=names, orgs=orgs), "show_names": st["show_names"],
                "lookup_orgs": st["lookup_orgs"], "names_blocked": bool(st["show_names"] and not names_ok),
                "lookup": self.orgs.status() if self.orgs is not None else None}

    def lookup_handles(self) -> list[str]:
        """Handles the org lookup may fetch: none unless both Show names and Look up orgs are on."""
        st = self.people_settings
        if self.people is None or not (st["show_names"] and st["lookup_orgs"]):
            return []
        return self.people.handles()

    def push(self, d: dict):
        now = time.time()
        evs = d.get("events") if isinstance(d.get("events"), dict) else {}
        lst = evs.get("events")
        cur = collections.Counter()
        for ev in lst if isinstance(lst, list) else []:
            typ = ev.get("type") if isinstance(ev, dict) else None
            cur[typ if isinstance(typ, str) and typ else "?"] += 1
        # the game repeats one event on ~3 consecutive frames: an occurrence counts as new only
        # beyond what the previous frame already carried (same type, back-to-back frames = one event)
        for typ, n in cur.items():
            for _ in range(n - self._ev_prev.get(typ, 0)):
                self._ev_id += 1
                self.events.appendleft({"id": self._ev_id, "t": now, "type": typ})
                if self.on_event:
                    self._call_hook("on_event", self.on_event, self.events[0])
        self._ev_prev = cur
        if self.signals is not None:  # own ship for nearby exclusion / QT "self"
            vs = d.get("vehicle")
            vs = vs.get("state") if isinstance(vs, dict) else None
            if isinstance(vs, dict):
                self.signals.set_telemetry_vehicle(vs.get("name"), vs.get("class_name"))
        self.frame = d
        self.seq += 1
        self.frames_total += 1
        self.last_frame_at = now
        self._rate.append(now)
        if self.on_frame:
            self._call_hook("on_frame", self.on_frame, d)

    def hz(self) -> float:
        r = [x for x in self._rate if x > time.time() - 2]
        return round(len(r) / 2, 1)

    def age(self) -> float | None:
        return time.time() - self.last_frame_at if self.last_frame_at else None

    def live_frame(self) -> dict | None:
        """Current frame only while the stream is healthy; None when ended/errored or stale."""
        a = self.age()
        if self.frame is None or a is None or a > STALE_S or not self.status.startswith("streaming"):
            return None
        return self.frame

    def public_session(self) -> dict | None:
        """Session for snapshot/SSE//api/session: never the server address or session ids."""
        pub = gamelog.public_view(self.session)
        if pub is not None:
            la = self.log_age() if self.log_age else None
            pub["log_age_s"] = round(la, 1) if la is not None else None
        return pub

    def vehicle_class(self) -> str | None:
        vs = (self.frame or {}).get("vehicle")
        vs = vs.get("state") if isinstance(vs, dict) else None
        c = vs.get("class_name") if isinstance(vs, dict) else None
        return c if isinstance(c, str) and c else None

    def capabilities(self) -> dict | None:
        """Game-data capabilities of the current vehicle; None with no vehicle or an unknown class."""
        return self.shipcaps.get(self.vehicle_class()) if self.shipcaps is not None else None

    def snapshot(self, names_ok: bool = False) -> dict:
        """names_ok: the requesting client passed the Origin/Host guard (handles may be included)."""
        a = self.age()
        return {
            "status": self.status, "source": self.source, "hz": self.hz(),
            "frames_total": self.frames_total,
            "age_s": round(a, 1) if a is not None else None,
            "stale": a is None or a > STALE_S or not self.status.startswith("streaming"),
            "events": list(self.events), "frame": self.frame, "session": self.public_session(),
            "signals": self.signals_view(),  # last frame kept for display; check "stale"
            "people": self.people_view(names_ok),
            "capabilities": self.capabilities(),
            "sample": {"t": round(time.time(), 3), **extract_sample(self.live_frame())},
        }


hub = Hub()
CFG = argparse.Namespace()
history = collections.deque(maxlen=SAMPLE_HZ * HISTORY_S)  # (t, sample dict)


async def run_sampler():
    while True:
        if hub.last_frame_at:  # once anything has streamed, record every tick: nulls while stale/ended
            history.append((round(time.time(), 3), extract_sample(hub.live_frame())))
        await asyncio.sleep(1 / SAMPLE_HZ)


def to_dict(msg) -> dict:
    return json_format.MessageToDict(msg, preserving_proto_field_name=True,
                                     always_print_fields_with_no_presence=True)


async def run_mock(api, types):
    hub.status, hub.source = "streaming", "mock"
    t0 = time.monotonic()
    while True:
        hub.push(to_dict(mock.frame(api, types, time.monotonic() - t0)))
        await asyncio.sleep(1 / PUSH_HZ)


def replay_records(lines, warn=None):
    """--record JSONL lines -> (t, frame) for valid frame records; {t, session} and junk are skipped."""
    for line in lines:
        try:
            rec = json.loads(line)
            t, frame = float(rec["t"]), rec["frame"]
        except (ValueError, KeyError, TypeError):
            continue  # includes {t, session} records (no "frame" key)
        if not math.isfinite(t):
            continue  # NaN/inf timestamp would poison the pacing sleep
        if not isinstance(frame, dict):
            if warn:
                warn(frame)
            continue
        yield t, frame


async def run_replay():
    """Stream a --record JSONL file at recorded pacing (scaled by --replay-speed), looping."""
    path = pathlib.Path(CFG.replay)
    hub.status, hub.source = "streaming", f"replay {path.name}"
    speed = max(CFG.replay_speed, 1e-3)
    loops = 0
    warned = False

    def warn(frame):
        nonlocal warned
        if not warned:
            warned = True
            print(f"[replay] skipping record(s) whose frame is not an object "
                  f"(first: {type(frame).__name__})", file=sys.stderr, flush=True)

    while True:
        prev_t = None
        with path.open(encoding="utf-8", errors="replace") as f:
            for t, frame in replay_records(f, warn):  # streamed line by line, never fully loaded
                if prev_t is not None:
                    await asyncio.sleep(min(max(t - prev_t, 0), 5) / speed)  # cap gaps at 5 s
                prev_t = t
                hub.push(frame)
        loops += 1  # status stays "streaming" (frontend keys on it); loop count goes in source
        hub.source = f"replay {path.name} · loop {loops + 1}"
        await asyncio.sleep(0.5)


def session_recorder(rec):
    """-> hook(session) writing {t, session} records with IP/session ids stripped; identical
    consecutive sessions are written once. Replay skips these records."""
    last = [object()]

    def rec_session(session):
        pub = gamelog.public(session)
        if pub == last[0]:
            return
        last[0] = pub
        rec.write(json.dumps({"t": time.time(), "session": pub}) + "\n")
        rec.flush()

    return rec_session


def signals_recorder(rec):
    """-> hook(public signals) writing {t, signals} records (class names only, never entity ids);
    identical consecutive views are written once. Replay skips these records."""
    last = [object()]

    def rec_signals(sig):
        if sig == last[0]:
            return
        last[0] = sig
        rec.write(json.dumps({"t": time.time(), "signals": sig}) + "\n")
        rec.flush()

    return rec_signals


async def run_grpc(api, apig):
    hub.source = f"grpc {CFG.grpc}"
    rec = None
    if CFG.record:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        rec = open(REC_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl", "a", encoding="utf-8", newline="\n")
        hub.on_session = session_recorder(rec)
        hub.on_signals = signals_recorder(rec)
        # the pre-first-poll placeholder (log_found False) is not worth a record; the tailer's first
        # notify follows within one poll
        if hub.session is not None and hub.session.get("log_found", True):
            hub.on_session(hub.session)
    backoff = 1
    while True:
        try:
            async with grpc.aio.insecure_channel(CFG.grpc) as ch:
                stub = apig.GameTelemetryServiceStub(ch)
                hub.status = "connecting"
                await asyncio.wait_for(ch.channel_ready(), timeout=5)
                hub.status = "connected, waiting for frames"
                async for msg in stub.TelemetryStream(api.TelemetryStreamRequest()):
                    d = to_dict(msg)
                    hub.status = "streaming"
                    hub.push(d)
                    backoff = 1
                    if rec:
                        rec.write(json.dumps({"t": time.time(), "frame": d}) + "\n")
                hub.status = "stream ended"
        except (grpc.aio.AioRpcError, asyncio.TimeoutError, OSError) as e:
            code = getattr(e, "code", lambda: type(e).__name__)()
            hub.status = f"disconnected ({code}) - retrying"
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 10)


def normalise_actions(raw) -> list[dict]:
    """Accept proto (action_name) or probe-JSON (actionName) shapes -> [{action_name, category, holdable}]."""
    out, seen = [], set()
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        name = a.get("action_name", a.get("actionName"))
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        cat = a.get("category")
        out.append({"action_name": name, "category": cat if isinstance(cat, str) and cat else "other",
                    "holdable": a.get("holdable") is True})
    return out


FAKE_FALLBACK = [
    {"action_name": "v_lights", "category": "lights_controller"},
    {"action_name": "v_lights_on", "category": "lights_controller"},
    {"action_name": "v_lights_off", "category": "lights_controller"},
    {"action_name": "v_toggle_landing_system", "category": "spaceship_movement"},
    {"action_name": "v_power_toggle_shields", "category": "spaceship_power"},
    {"action_name": "v_power_set_off", "category": "spaceship_power"},
    {"action_name": "v_open_all_doors", "category": "spaceship_general"},
    {"action_name": "v_self_destruct", "category": "spaceship_general"},
    {"action_name": "v_eject", "category": "seat_general"},
    {"action_name": "v_invoke_ping", "category": "spaceship_radar", "holdable": True},
    {"action_name": "toggle_flashlight", "category": "player"},
    {"action_name": "emote_wave", "category": "player_emotes"},
]


# --mock: fake triggers flip the mock vehicle state so the dashboard's on-button state can be exercised
MOCK_TOGGLES = {
    "lights_enabled": ("v_lights", "v_lights_on", "v_lights_off"),
    "gear_deployed": ("v_toggle_landing_system", "v_deploy_landing_system", "v_retract_landing_system"),
    "shield_generators": ("v_power_toggle_shields", "v_power_set_shields_on", "v_power_set_shields_off"),
}


class FakeTrigger:
    """Stand-in for TriggerGameAction in --mock/--replay: nothing ever reaches the game."""

    def __init__(self, mutate_mock: bool):
        raw = None
        with contextlib.suppress(OSError, ValueError):
            raw = json.loads(ALLOWLIST_JSON.read_text(encoding="utf-8")).get("actions")
        self.actions = normalise_actions(raw) or normalise_actions(FAKE_FALLBACK)
        self.by_name = {a["action_name"]: a for a in self.actions}
        self.mutate_mock = mutate_mock
        self._last: dict[str, float] = {}

    def trigger(self, name: str, hold_ms: int | None) -> tuple[str, str]:
        a = self.by_name.get(name)
        if a is None:
            return STATUS_PREFIX + "UNKNOWN_ACTION", f"fake: {name} not in allowlist"
        if hold_ms is not None and not a["holdable"]:
            return STATUS_PREFIX + "HOLD_NOT_ALLOWED", f"fake: {name} is not holdable"
        now = time.monotonic()
        if now - self._last.get(name, -1e9) < TRIGGER_DEBOUNCE_S:
            return STATUS_PREFIX + "RATE_LIMITED", "fake: too soon"
        self._last[name] = now
        if self.mutate_mock:
            for key, (tog, on, off) in MOCK_TOGGLES.items():
                if name in (tog, on, off):
                    mock.STATE[key] = not mock.STATE[key] if name == tog else name == on
        return STATUS_PREFIX + "OK", "fake: accepted" + (f" (hold {hold_ms} ms)" if hold_ms else "")


async def _guard(name, coro, status=True):
    """Run a background task; an unexpected crash is logged and (status=True) surfaces in hub.status.
    The Game.log tailer passes status=False: it is side context and must never mark telemetry stale."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - last-resort guard, the task would otherwise die silently
        if status:
            hub.status = f"error ({name}: {type(e).__name__})"
        print(f"[{name}] crashed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()


_data_cache: dict[str, object] = {}


def load_data_json(name: str):
    """scgameapi/data/<name> parsed once and cached; raises OSError / ValueError when missing or invalid."""
    if name not in _data_cache:
        _data_cache[name] = json.loads((DATA / name).read_text(encoding="utf-8"))
    return _data_cache[name]


def _data_response(name: str):
    try:
        return JSONResponse(load_data_json(name))
    except (OSError, ValueError) as e:
        return JSONResponse({"status": "ERROR", "message": f"{name} unavailable: {type(e).__name__}: {e}"},
                            status_code=500)


@contextlib.asynccontextmanager
async def lifespan(app):
    try:  # game-data capabilities: a missing/broken dataset only disables gating, never the bridge
        hub.shipcaps = await asyncio.to_thread(shipcaps.default, getattr(CFG, "channel", "PTU"),
                                               getattr(CFG, "gamedata", None))
        st = hub.shipcaps.status()
        print(f"[shipcaps] {st['ships']} classes from {hub.shipcaps.source} (build {st['source_build']})"
              if st["loaded"] else f"[shipcaps] disabled: {st['error']}", file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[shipcaps] failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    api, apig, types = stubs.load()
    app.state.api, app.state.apig = api, apig
    app.state.channel = None
    app.state.fake = None
    if CFG.replay:
        src = run_replay()
    elif CFG.mock:
        src = run_mock(api, types)
    else:
        src = run_grpc(api, apig)
    if CFG.replay or CFG.mock:
        app.state.fake = FakeTrigger(mutate_mock=bool(CFG.mock and not CFG.replay))
    else:  # one shared channel for unary calls (trigger + list); the stream keeps its own
        app.state.channel = grpc.aio.insecure_channel(CFG.grpc)
    tasks = [asyncio.create_task(_guard("source", src)), asyncio.create_task(_guard("sampler", run_sampler()))]
    global ALERTS
    try:  # alerts are side output: a broken rules file or audio setup never stops the bridge
        ALERTS = build_alerts()
        hub.on_frame = lambda d: ALERTS.on_frame(d)
        hub.on_event = lambda ev: ALERTS.on_event(ev)
        tasks.append(asyncio.create_task(_guard("alerts", run_alerts(), status=False)))
    except Exception as e:  # noqa: BLE001
        print(f"[alerts] disabled: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    if CFG.mock and not CFG.replay:
        hub.signals, sig_evs = gamelog.mock_signals()
        hub.people = people.mock_people()
        sess, sess_evs = gamelog.mock_session()
        hub.set_session(sess, sess_evs + sig_evs)
    elif CFG.game_log_path:
        tailer = gamelog.GameLogTailer(CFG.game_log_path, channel=CFG.channel, on_change=hub.set_session)
        hub.session = tailer.session()
        hub.signals = tailer.signals
        hub.people = tailer.people
        hub.log_age = tailer.log_age
        tasks.append(asyncio.create_task(_guard("gamelog", tailer.run(), status=False)))
    hub.people_settings = {"show_names": bool(getattr(CFG, "show_names", False)),
                           "lookup_orgs": bool(getattr(CFG, "lookup_orgs", False))}
    # --mock handles are synthetic (TestPilot_*): never send them to RSI, whatever the flags say
    fake_rsi = bool(getattr(CFG, "fake_rsi", False) or CFG.mock)
    hub.orgs = people.OrgLookup(pathlib.Path(getattr(CFG, "rsi_cache", None) or RSI_CACHE_JSON),
                                fetch=people.fake_fetch if fake_rsi else None,
                                enabled=hub.people_settings["lookup_orgs"])
    tasks.append(asyncio.create_task(_guard("people", hub.orgs.run(hub.lookup_handles), status=False)))
    print(f"[people] show names {'on' if hub.people_settings['show_names'] else 'off'}, org lookup "
          f"{'on' if hub.people_settings['lookup_orgs'] else 'off'}{' (fake RSI)' if fake_rsi else ''}",
          file=sys.stderr, flush=True)
    yield
    for t in tasks:
        t.cancel()
    if ALERTS is not None:
        with contextlib.suppress(Exception):
            await ALERTS.sink.stop()
    if app.state.channel is not None:
        await app.state.channel.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


_FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            b'<path d="M4 1h8l3 3v8l-3 3H4l-3-3V4z" fill="#0f172a" stroke="#3b82f6"/>'
            b'<path d="M5 8h6M8 5v6" stroke="#3b82f6" stroke-width="1.5"/></svg>')


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # browsers request this regardless of <link rel=icon>; answer instead of 404 noise
    return Response(_FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/snapshot")
async def snapshot(request: Request):
    return hub.snapshot(names_ok=_origin_error(request) is None)


@app.get("/api/session")
async def get_session():
    """Game.log session context without IP/session ids; {"session": null} when disabled (--no-game-log,
    or --replay without --game-log)."""
    return {"session": hub.public_session()}


@app.get("/api/session/private")
async def get_session_private(request: Request):
    """Server address/port + session ids, only for this dashboard's own origin (UI "Reveal")."""
    err = _origin_error(request)
    if err:
        return JSONResponse({"status": "FORBIDDEN", "message": err}, status_code=403)
    return JSONResponse({"session": gamelog.private(hub.session) if hub.session is not None else None},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/signals")
async def get_signals():
    """Game.log signals (zone, armistice, nearby ship classes + game-data info): no entity ids;
    {"signals": null} when Game.log is disabled."""
    return {"signals": hub.signals_view()}


# ---- people (scgameapi/people.py, docs/telemetry/people-and-lookup.md) -------------------------------

def _forbidden(err: str):
    return JSONResponse({"status": "FORBIDDEN", "message": err}, status_code=403)


@app.get("/api/people")
async def get_people(request: Request):
    """Players seen in Game.log. Same Origin/Host guard as /api/session/private; handles only while Show
    names is on, org info only while Look up orgs is on. {"people": null} when Game.log is disabled."""
    err = _origin_error(request)
    if err:
        return _forbidden(err)
    return JSONResponse({"people": hub.people_view(names_ok=True), "settings": dict(hub.people_settings)},
                        headers={"Cache-Control": "no-store"})


@app.put("/api/people/settings")
async def put_people_settings(request: Request):
    """{show_names?: bool, lookup_orgs?: bool} at runtime (not persisted; CLI/env set the start value)."""
    body, err = await _json_body(request, 1024)
    if err:
        return err
    bad = set(body) - {"show_names", "lookup_orgs"}
    if bad or not body or any(not isinstance(v, bool) for v in body.values()):
        return JSONResponse({"status": "BAD_REQUEST", "message": "body: {show_names?: bool, lookup_orgs?: bool}"},
                            status_code=400)
    hub.people_settings.update(body)
    if hub.orgs is not None:
        hub.orgs.enabled = hub.people_settings["lookup_orgs"]
    print(f"[people] settings: show names {'on' if hub.people_settings['show_names'] else 'off'}, org lookup "
          f"{'on' if hub.people_settings['lookup_orgs'] else 'off'}", file=sys.stderr, flush=True)
    hub.seq += 1  # push the new view over SSE now
    return JSONResponse({"status": "OK", "settings": dict(hub.people_settings),
                         "people": hub.people_view(names_ok=True)}, headers={"Cache-Control": "no-store"})


@app.delete("/api/people/cache")
async def delete_people_cache(request: Request):
    """Forget every cached RSI lookup (memory + .tmp/rsi-cache.json)."""
    err = _origin_error(request)
    if err:
        return _forbidden(err)
    n = hub.orgs.clear() if hub.orgs is not None else 0
    hub.seq += 1
    return {"status": "OK", "removed": n}


@app.get("/api/action-states")
async def get_action_states():
    """The shared action -> telemetry state map (scgameapi/data/action_state_map.json), verbatim."""
    return _data_response("action_state_map.json")


@app.get("/api/shield-faces")
async def get_shield_faces():
    """Shield face layout metadata by faces[] length (scgameapi/data/shield_faces.json), verbatim."""
    return _data_response("shield_faces.json")


def _gamedata_status() -> dict:
    return hub.shipcaps.status() if hub.shipcaps is not None else {
        "loaded": False, "source": None, "source_build": None, "ships": 0, "error": "not loaded"}


@app.get("/api/ship")
async def get_ship():
    """Current vehicle class + its game-data capabilities (null when no vehicle / unknown class)."""
    return {"class": hub.vehicle_class(), "capabilities": hub.capabilities(), "gamedata": _gamedata_status()}


@app.get("/api/ships")
async def get_ships(request: Request):
    """?class=CLASS -> one class's capabilities (404 when unknown); no query -> the list of known classes."""
    cls = request.query_params.get("class")
    if cls is None:
        names = sorted(hub.shipcaps.table) if hub.shipcaps is not None else []
        return {"classes": names, "gamedata": _gamedata_status()}
    caps = hub.shipcaps.get(cls) if hub.shipcaps is not None else None
    if caps is None:
        return JSONResponse({"class": cls, "capabilities": None, "gamedata": _gamedata_status()}, status_code=404)
    return {"class": cls, "capabilities": caps, "gamedata": _gamedata_status()}


@app.get("/api/history")
async def get_history():
    pts = list(history)
    return {
        "interval_s": 1 / SAMPLE_HZ,
        "t": [t for t, _ in pts],
        "series": {k: [s[k] for _, s in pts] for k in SERIES},
    }


_actions_cache = {"t": 0.0, "actions": []}  # last NON-empty game list


@app.get("/api/actions")
async def actions():
    base = {"dangerous": sorted(DANGEROUS)}
    if app.state.fake is not None:
        return {"actions": app.state.fake.actions, **base, "source": "fake"}
    c = _actions_cache
    if c["actions"] and time.monotonic() - c["t"] < ACTIONS_CACHE_S:
        return {"actions": c["actions"], **base, "source": "game"}
    # cache empty or expired: re-query (the game returns [] until the player has spawned)
    try:
        stub = app.state.apig.GameTelemetryServiceStub(app.state.channel)
        r = await stub.ListTriggerableActions(app.state.api.ListTriggerableActionsRequest(),
                                              timeout=TRIGGER_TIMEOUT_S)
        acts = normalise_actions(to_dict(r).get("actions"))
    except (grpc.aio.AioRpcError, asyncio.TimeoutError, OSError) as e:
        err = f"{e.code().name} {e.details()}" if isinstance(e, grpc.aio.AioRpcError) else type(e).__name__
        return {"actions": c["actions"], **base, "source": "game", "error": err}  # serve stale cache if any
    if acts:
        c["t"], c["actions"] = time.monotonic(), acts
    return {"actions": acts, **base, "source": "game"}


def _log_trigger(name, hold_ms, status, message, http=200):
    hold = f" hold={hold_ms}ms" if hold_ms is not None else ""
    print(f"[trigger] {time.strftime('%H:%M:%S')} {name}{hold} -> {status} ({http}) {message}",
          file=sys.stderr, flush=True)


def _allowed_origins() -> set[str]:
    port = CFG.port
    hosts = {"127.0.0.1", "localhost", CFG.host}
    return {f"http://{h}:{port}" for h in hosts if h and h not in ("0.0.0.0", "::")}


def _origin_error(request: Request) -> str | None:
    """CSRF / DNS-rebinding guard: None if the request comes from this dashboard's own origin."""
    allowed = _allowed_origins()
    origin = request.headers.get("origin")
    if origin is not None and origin not in allowed:
        return f"origin {origin!r} not allowed"
    host = request.headers.get("host", "")
    if f"http://{host}" not in allowed:
        return f"host {host!r} not allowed"
    return None


_trigger_last: dict[str, float] = {}  # bridge-side debounce, independent of the game's own
_trigger_inflight: set[str] = set()   # actions with a game call still pending (any client)


@app.post("/api/trigger")
async def trigger(request: Request):
    def reply(http, status, message, name="?", hold_ms=None):
        _log_trigger(name, hold_ms, status, message, http)
        return JSONResponse({"status": status, "message": message}, status_code=http)

    # CSRF / DNS-rebinding guard: only this dashboard's own origin may press game buttons
    err = _origin_error(request)
    if err:
        return reply(403, "FORBIDDEN", err)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return reply(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")

    clen = request.headers.get("content-length")
    if clen is not None and (not clen.isdigit() or int(clen) > TRIGGER_BODY_MAX):
        return reply(413, "PAYLOAD_TOO_LARGE", f"body must be at most {TRIGGER_BODY_MAX} bytes")
    raw = b""
    async for chunk in request.stream():  # also caps chunked bodies without Content-Length
        raw += chunk
        if len(raw) > TRIGGER_BODY_MAX:
            return reply(413, "PAYLOAD_TOO_LARGE", f"body must be at most {TRIGGER_BODY_MAX} bytes")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return reply(400, "BAD_REQUEST", "body is not valid JSON")
    if not isinstance(body, dict):
        return reply(400, "BAD_REQUEST", "body must be a JSON object")
    name, hold_ms, confirm = body.get("action_name"), body.get("hold_ms"), body.get("confirm")
    if not isinstance(name, str) or not ACTION_RE.fullmatch(name):
        return reply(400, "BAD_REQUEST", "action_name must match ^[a-z0-9_]+$ (1-64 chars)")
    if hold_ms is not None and (isinstance(hold_ms, bool) or not isinstance(hold_ms, int)
                                or not 1 <= hold_ms <= 10000):
        return reply(400, "BAD_REQUEST", "hold_ms must be an integer 1..10000 or null", name)
    if name in DANGEROUS and confirm is not True:
        return reply(409, "CONFIRM_REQUIRED", f"{name} is destructive; resend with \"confirm\": true",
                     name, hold_ms)

    if name in _trigger_inflight:
        return reply(429, "RATE_LIMITED", "bridge: previous call still pending", name, hold_ms)
    now = time.monotonic()
    if now - _trigger_last.get(name, -1e9) < TRIGGER_DEBOUNCE_S:
        return reply(429, "RATE_LIMITED", "bridge debounce", name, hold_ms)
    _trigger_last[name] = now
    _trigger_inflight.add(name)
    try:
        return await _forward(name, hold_ms, reply)
    finally:
        _trigger_inflight.discard(name)
        _trigger_last[name] = time.monotonic()  # debounce window counts from the reply, not the send


async def _forward(name, hold_ms, reply):
    if app.state.fake is not None:
        if CFG.fake_delay:
            await asyncio.sleep(CFG.fake_delay)
        status, message = app.state.fake.trigger(name, hold_ms)
        ff = getattr(CFG, "fake_fail", None)  # test: force FAILED for matching names (exercises UNSUPPORTED)
        if ff and status == STATUS_PREFIX + "OK" and re.fullmatch(ff, name):
            status, message = STATUS_PREFIX + "FAILED", "fake: forced FAILED (--fake-fail)"
        return reply(200, status, message, name, hold_ms)
    api = app.state.api
    req = api.TriggerGameActionRequest(action_name=name)
    if hold_ms is not None:
        req.hold_ms = hold_ms
    try:
        stub = app.state.apig.GameTelemetryServiceStub(app.state.channel)
        r = await stub.TriggerGameAction(req, timeout=TRIGGER_TIMEOUT_S)
    except (grpc.aio.AioRpcError, asyncio.TimeoutError, OSError) as e:
        msg = f"{e.code().name}: {e.details()}" if isinstance(e, grpc.aio.AioRpcError) else type(e).__name__
        return reply(502, "BRIDGE_ERROR", msg, name, hold_ms)
    try:
        status = api.TriggerGameActionStatus.Name(r.status)
    except ValueError:
        status = f"{STATUS_PREFIX}{r.status}"
    return reply(200, status, r.message, name, hold_ms)


# ---- alerts (scgameapi/alerts.py, docs/telemetry/alerts.md) ----------------------------------------

ALERTS: alerts.AlertService | None = None


def alerts_user_path() -> pathlib.Path:
    p = getattr(CFG, "alerts", None) or os.environ.get("SCGAMEAPI_ALERTS")
    return pathlib.Path(p) if p else ALERTS_USER_JSON


def _alert_event(a: dict):
    """Every alert also lands in the hub event list (snapshot/SSE `events`) as ALERT."""
    hub.add_event("ALERT", a["t"], a["text"], rule=a["rule"], severity=a["severity"], **({"test": True} if a.get("test") else {}))
    hub.seq += 1


def build_alerts() -> alerts.AlertService:
    user = alerts_user_path()
    cfg, err = alerts.load_config(user)
    if err:
        print(f"[alerts] user rules ignored, using defaults: {err}", file=sys.stderr, flush=True)
    mode = getattr(CFG, "alerts_audio", None) or "auto"
    dry = bool(getattr(CFG, "alerts_dry_run", False))
    player = alerts.detect_player()
    tts = alerts.detect_tts(cfg["settings"].get("tts"))
    svc = alerts.AlertService(cfg, user_path=user, mode=mode, dry_run=dry, player=player, tts=tts,
                              emit_event=_alert_event, config_error=err)
    print(f"[alerts] {sum(r.get('enabled', True) for r in cfg['rules'])}/{len(cfg['rules'])} rules, audio {mode}"
          f"{' (dry run)' if dry else ''}, player {player[0] if player else 'none'}, "
          f"tts {tts['name'] if tts else 'none'}", file=sys.stderr, flush=True)
    return svc


async def run_alerts():
    logged: set[str] = set()
    while True:
        svc = ALERTS
        if svc is not None:
            try:  # one bad tick (e.g. a bad sounds setting) never ends alert audio for the session
                svc.on_source(hub.source)
                await svc.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ not in logged:
                    logged.add(type(e).__name__)
                    print(f"[alerts] tick failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        await asyncio.sleep(ALERTS_TICK_S)


def _svc_or_error():
    if ALERTS is None:
        return None, JSONResponse({"status": "UNAVAILABLE", "message": "alerts disabled"}, status_code=503)
    return ALERTS, None


async def _json_body(request: Request, max_bytes: int = JSON_BODY_MAX):
    """Guarded JSON object body -> (body, None) or (None, error response). Same Origin/Host guard as
    /api/trigger: only this dashboard's own origin may change alert settings."""
    def bad(http, status, message):
        return None, JSONResponse({"status": status, "message": message}, status_code=http)

    err = _origin_error(request)
    if err:
        return bad(403, "FORBIDDEN", err)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return bad(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")
    clen = request.headers.get("content-length")
    if clen is not None and (not clen.isdigit() or int(clen) > max_bytes):
        return bad(413, "PAYLOAD_TOO_LARGE", f"body must be at most {max_bytes} bytes")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > max_bytes:
            return bad(413, "PAYLOAD_TOO_LARGE", f"body must be at most {max_bytes} bytes")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return bad(400, "BAD_REQUEST", "body is not valid JSON")
    if not isinstance(body, dict):
        return bad(400, "BAD_REQUEST", "body must be a JSON object")
    return body, None


def _rel(p: pathlib.Path | None) -> str | None:
    if p is None:
        return None
    with contextlib.suppress(ValueError):
        return str(p.resolve().relative_to(stubs.ROOT.resolve()))
    return p.name


@app.get("/api/alerts")
async def get_alerts(request: Request):
    """Recent alerts (newest first), rules summary and audio routing state."""
    svc, err = _svc_or_error()
    if err:
        return err
    n = request.query_params.get("n", "50")
    n = int(n) if n.isdigit() else 50
    return {"recent": [svc._public(a) for a in list(svc.recent)[:max(0, min(n, 50))]],
            "rules": svc.rules_summary(), "audio": svc.status(), "boot": svc.boot,
            "config": {"user_file": _rel(svc.user_path), "error": svc.config_error}}


@app.get("/api/alerts/rules")
async def get_alert_rules():
    svc, err = _svc_or_error()
    if err:
        return err
    return {"rules": svc.rules(), "user_file": _rel(svc.user_path), "error": svc.config_error}


@app.put("/api/alerts/rules")
async def put_alert_rules(request: Request):
    """Full rule list (as GET returns it); validated, applied, and its diff from the defaults saved to
    the user file. Omitted default ids are dropped."""
    svc, err = _svc_or_error()
    if err:
        return err
    body, err = await _json_body(request)
    if err:
        return err
    try:
        rules = svc.set_rules(body.get("rules"))
    except (ValueError, TypeError, AttributeError) as e:
        return JSONResponse({"status": "INVALID", "message": str(e)[:2000]}, status_code=400)
    except OSError as e:
        return JSONResponse({"status": "ERROR", "message": f"save failed: {type(e).__name__}"}, status_code=500)
    return {"status": "OK", "rules": len(rules), "user_file": _rel(svc.user_path)}


@app.post("/api/alerts/test")
async def post_alert_test(request: Request):
    """Fire one rule's alert (text prefixed TEST:) through the active sink; ignores cooldown and mute."""
    svc, err = _svc_or_error()
    if err:
        return err
    body, err = await _json_body(request, 1024)
    if err:
        return err
    rid = body.get("id")
    if not isinstance(rid, str) or not alerts.RULE_ID_RE.fullmatch(rid):
        return JSONResponse({"status": "BAD_REQUEST", "message": "id must be a rule id"}, status_code=400)
    a = svc.fire_test(rid)
    if a is None:
        return JSONResponse({"status": "NOT_FOUND", "message": f"no rule {rid!r}"}, status_code=404)
    return {"status": "OK", "alert": svc._public(a), "audio": svc.status()}


@app.post("/api/alerts/mute")
async def post_alert_mute(request: Request):
    """{minutes: 0..240}: silence sound + speech (toasts continue); 0 unmutes."""
    svc, err = _svc_or_error()
    if err:
        return err
    body, err = await _json_body(request, 1024)
    if err:
        return err
    m = body.get("minutes")
    if isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m <= 240:
        return JSONResponse({"status": "BAD_REQUEST", "message": "minutes must be 0..240"}, status_code=400)
    svc.mute(float(m))
    return {"status": "OK", "audio": svc.status()}


@app.put("/api/alerts/audio")
async def put_alert_audio(request: Request):
    """{mode: auto|bridge|browser|off} at runtime (not persisted; CLI/env sets the start value)."""
    svc, err = _svc_or_error()
    if err:
        return err
    body, err = await _json_body(request, 1024)
    if err:
        return err
    try:
        svc.set_mode(body.get("mode"))
    except ValueError as e:
        return JSONResponse({"status": "BAD_REQUEST", "message": str(e)}, status_code=400)
    return {"status": "OK", "audio": svc.status()}


@app.post("/api/alerts/audio-client")
async def post_audio_client(request: Request):
    """Dashboard tab registers / heartbeats as an audio client: {client_id, enabled, speech?}. While
    an enabled tab has its /events?client=ID stream open it plays and the bridge stays silent (auto)."""
    svc, err = _svc_or_error()
    if err:
        return err
    body, err = await _json_body(request, 1024)
    if err:
        return err
    cid, en, sp = body.get("client_id"), body.get("enabled"), body.get("speech")
    if not isinstance(cid, str) or not alerts.CLIENT_ID_RE.fullmatch(cid) or not isinstance(en, bool) \
            or (sp is not None and not isinstance(sp, bool)):
        return JSONResponse({"status": "BAD_REQUEST",
                             "message": "client_id ^[A-Za-z0-9_-]{8,64}$, enabled bool, speech bool|null"},
                            status_code=400)
    now = time.time()
    svc.router.heartbeat(cid, en, now, sp)
    route, who = svc.router.route(now)
    return {"status": "OK", "target": route == "browser" and who == cid, "audio": svc.status(now)}


def sse_alert_lines(cid: str | None, last: int, stream: int | None = None) -> tuple[list[str], int]:
    """New alert feed items as named SSE events ("event: alert"), personalised for tab `cid` (only its
    newest open stream `stream` gets play:true)."""
    svc = ALERTS
    if svc is None:
        return [], last
    out = []
    newest = svc.router.is_newest(cid, stream)
    for item in svc.feed_since(last):
        out.append(f"event: alert\ndata: {json.dumps(svc.for_client(item, cid, newest))}\n\n")
        last = item["seq"]
    return out, last


@app.get("/events")
async def events(request: Request):
    """SSE: unnamed `data:` snapshots (as before) + named `alert` events (toast / play jobs).
    ?client=ID identifies a dashboard tab for audio routing (POST /api/alerts/audio-client)."""
    cid = request.query_params.get("client")
    cid = cid if isinstance(cid, str) and alerts.CLIENT_ID_RE.fullmatch(cid) else None
    names_ok = _origin_error(request) is None  # handles only to this dashboard's own origin

    async def gen():
        last = -1
        idle = 0
        feed = ALERTS.feed_seq if ALERTS is not None else 0  # live only: history is in /api/alerts
        svc = ALERTS
        stream = svc.router.sse_open_stream(cid, time.time()) if svc is not None else None
        try:
            while True:
                lines, feed = sse_alert_lines(cid, feed, stream)
                for line in lines:
                    yield line
                if hub.seq != last or idle >= PUSH_HZ:  # push on new frame, or 1 Hz heartbeat for status
                    last, idle = hub.seq, 0
                    yield f"data: {json.dumps(hub.snapshot(names_ok))}\n\n"
                else:
                    idle += 1
                await asyncio.sleep(1 / PUSH_HZ)
        finally:
            if svc is not None:
                svc.router.sse_close(cid, stream, time.time())

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grpc", default="127.0.0.1:50051")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mock", action="store_true", help="synthetic frames, no game needed")
    ap.add_argument("--record", action="store_true", help="append frames to .tmp/recordings/*.jsonl")
    ap.add_argument("--replay", metavar="PATH.jsonl", help="replay a --record file (loops)")
    ap.add_argument("--replay-speed", type=float, default=1.0, help="replay pacing multiplier")
    ap.add_argument("--fake-delay", type=float, default=0.0, help=argparse.SUPPRESS)  # test: slow fake replies
    ap.add_argument("--fake-fail", metavar="REGEX", default=None, help=argparse.SUPPRESS)  # test: fake FAILED
    ap.add_argument("--game-log", metavar="PATH", help="Game.log to follow for session context (read-only)")
    ap.add_argument("--channel", default=(os.environ.get("SCGAMEAPI_CHANNEL") or "").upper() or None, type=str.upper,
                    choices=gamelog.CHANNELS,
                    help="game channel for Game.log + schema ($SCGAMEAPI_CHANNEL; default: last played, else LIVE)")
    ap.add_argument("--sc-dir", default=os.environ.get("SC_DIR") or None,
                    help="StarCitizen install dir holding LIVE/, PTU/ ... ($SC_DIR; default: auto-detected, "
                         "see docs/telemetry/api-spec.md)")
    ap.add_argument("--no-game-log", action="store_true", help="disable Game.log session context")
    ap.add_argument("--gamedata", metavar="PATH", default=os.environ.get("SCGAMEAPI_GAMEDATA") or None,
                    help="sc-gamedata game-ships.json (or its directory) for ship capabilities ($SCGAMEAPI_GAMEDATA); "
                         "default .tmp/gamedata/<channel>/game-ships.json")
    ap.add_argument("--alerts", metavar="PATH", default=os.environ.get("SCGAMEAPI_ALERTS") or None,
                    help="user alert rules patch ($SCGAMEAPI_ALERTS; default .tmp/alerts.json if present)")
    ap.add_argument("--alerts-audio", default=(os.environ.get("SCGAMEAPI_ALERTS_AUDIO") or "auto").lower(),
                    type=str.lower, choices=alerts.AUDIO_MODES,
                    help="who plays alert audio: auto (dashboard tab with sound enabled, else bridge) | bridge | "
                         "browser | off ($SCGAMEAPI_ALERTS_AUDIO)")
    ap.add_argument("--alerts-dry-run", action="store_true",
                    default=os.environ.get("SCGAMEAPI_ALERTS_DRY_RUN") == "1",
                    help="bridge never plays audio, only logs what it would play ($SCGAMEAPI_ALERTS_DRY_RUN=1)")
    ap.add_argument("--show-names", action="store_true", default=os.environ.get("SCGAMEAPI_SHOW_NAMES") == "1",
                    help="show player handles seen in Game.log (same-origin dashboard only; $SCGAMEAPI_SHOW_NAMES=1)")
    ap.add_argument("--lookup-orgs", action="store_true", default=os.environ.get("SCGAMEAPI_LOOKUP_ORGS") == "1",
                    help="look up those handles' orgs on robertsspaceindustries.com, cached in .tmp/rsi-cache.json "
                         "($SCGAMEAPI_LOOKUP_ORGS=1)")
    ap.add_argument("--rsi-cache", metavar="PATH", default=None, help=argparse.SUPPRESS)  # test: cache location
    ap.add_argument("--fake-rsi", action="store_true", default=os.environ.get("SCGAMEAPI_FAKE_RSI") == "1",
                    help=argparse.SUPPRESS)  # test: offline synthetic RSI pages, never the network
    ap.parse_args(namespace=CFG)
    if CFG.alerts_audio not in alerts.AUDIO_MODES:
        ap.error(f"--alerts-audio / $SCGAMEAPI_ALERTS_AUDIO: invalid choice {CFG.alerts_audio!r}")
    sc_dir = paths.find_sc_dir(CFG.sc_dir)
    CFG.sc_dir = str(sc_dir) if sc_dir is not None else None
    if CFG.channel is None:
        CFG.channel = paths.default_channel(sc_dir)
    if CFG.channel not in gamelog.CHANNELS:  # argparse does not apply choices to an env-derived default
        ap.error(f"--channel / $SCGAMEAPI_CHANNEL: invalid choice {CFG.channel!r} (choose from {', '.join(gamelog.CHANNELS)})")
    if CFG.fake_fail:
        try:
            re.compile(CFG.fake_fail)
        except re.error as e:
            ap.error(f"--fake-fail: {e}")
    if CFG.replay and not pathlib.Path(CFG.replay).is_file():
        ap.error(f"--replay: no such file {CFG.replay}")
    if CFG.no_game_log:
        CFG.game_log_path = None
    elif CFG.game_log:
        CFG.game_log_path = pathlib.Path(CFG.game_log)
    elif CFG.replay or CFG.mock:
        CFG.game_log_path = None  # replay/mock never pick up the live game's log implicitly
    else:
        CFG.game_log_path = gamelog.default_log_path(CFG.sc_dir, CFG.channel)
        if CFG.game_log_path is None:
            print(f"[gamelog] no Star Citizen install found (searched: {paths.describe_search()}); "
                  f"session context off: {paths.setup_hint()} or pass --sc-dir / --game-log",
                  file=sys.stderr, flush=True)
        elif not CFG.game_log_path.exists():
            print(f"[gamelog] {CFG.game_log_path} not found yet (waiting for the game; "
                  "set --sc-dir / $SC_DIR / --channel if this is the wrong install)", file=sys.stderr, flush=True)
    # the schema (stubs.load -> schema.resolve) follows the same install dir + channel as Game.log
    if CFG.sc_dir:
        os.environ["SC_DIR"] = CFG.sc_dir
    os.environ["SCGAMEAPI_CHANNEL"] = CFG.channel
    # SSE streams never end on their own: without a timeout a stopped bridge lingers until every
    # dashboard tab closes, still serving the old process's frames/alerts to tabs that never reconnect
    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level="warning", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
