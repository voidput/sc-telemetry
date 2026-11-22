"""Game.log tailer: session context (shard, region, system, build) the gRPC API does not expose.

The game rewrites Game.log on every launch (new file at the same path, the old one moved to
logbackups/). The tailer polls (no inotify, no extra deps), reads the whole current file once,
then follows appended lines; a rewrite (inode change, size shrink, changed file head or a
"Log started" line) resets the session and re-reads from the start. Game.log is only ever
opened read-only.
"""
import asyncio
import collections
import contextlib
import datetime as dt
import os
import pathlib
import re
import sys
import threading
import time
import traceback

from . import paths, people

CHANNELS = paths.CHANNELS
POLL_S = 0.5
HEAD_BYTES = 128          # file head fingerprint: a same-inode rewrite changes it
READ_CHUNK = 1 << 20
# Windows: st_ino is not a reliable identity (0 on FAT/exFAT, network shares), so rotation there is
# detected by size shrink + changed file head + the "Log started" line only.
USE_INODE = os.name != "nt"
# Windows: open() lacks FILE_SHARE_DELETE, so while we hold Game.log the game cannot rename it to
# logbackups/ on relaunch. The tailer opens and closes the file on every poll (never a persistent
# handle) and caps one poll's read so the handle is only held briefly. Linux/Wine: no cap.
READ_MAX_PER_POLL = (4 << 20) if os.name == "nt" else None

REGIONS = {
    "use1": "US East", "usw2": "US West", "euw1": "EU West", "euc1": "EU Central",
    "apse2": "Asia-Pacific SE (Sydney)", "apne1": "Asia-Pacific NE (Tokyo)",
}
# fields that identify the player's connection/session: never written to recordings
PRIVATE_FIELDS = ("server_address", "server_port", "session_id", "env_session")
FIELDS = ("channel", "build", "env_session", "session_id", "shard", "region_code", "region_name",
          "server_address", "server_port", "location_id", "system", "joined_at", "log_started_at")

_TS = re.compile(r"^<(?P<ts>\d{4}-\d\d-\d\dT[\d:.]+Z)>\s?(?P<body>.*)$")
_RULES = [
    ("log_started", re.compile(r"^Log started on (?P<when>.+?)\s*$")),
    ("build", re.compile(r"^FileVersion:\s*(?P<build>\S+)")),
    ("executable", re.compile(r"^Executable:\s*(?P<path>.+?)\s*$")),
    ("session_id", re.compile(r"^\[Trace\] @session:\s*'(?P<v>[^']+)'")),
    ("env_session", re.compile(r"^\[Trace\] @env_session:\s*'(?P<v>[^']+)'")),
    ("join", re.compile(r"<Join PU> address\[(?P<address>[^\]]*)\] port\[(?P<port>\d*)\] "
                        r"shard\[(?P<shard>[^\]]*)\](?: locationId\[(?P<loc>-?\d+)\])?")),
    ("shard", re.compile(r"<Update Shard Id> New Shard Id: (?P<shard>[A-Za-z0-9_\-]+?)\.?(?:\s|$)")),
    ("loading", re.compile(r"^Loading screen for (?P<system>[^:]+?) : \S+ closed after")),
]


def parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    with contextlib.suppress(ValueError):
        return dt.datetime.fromisoformat(ts).timestamp()
    return None


def parse_line(line: str) -> dict | None:
    """One Game.log line -> {"kind", "ts", ...fields} or None. Pure; tolerant of CRLF."""
    line = line.rstrip("\r\n")
    m = _TS.match(line)
    ts, body = (m["ts"], m["body"]) if m else (None, line)
    for kind, rx in _RULES:
        g = rx.search(body) if kind in ("join", "shard") else rx.match(body)
        if not g:
            continue
        ev = {"kind": kind, "ts": ts}
        if kind == "log_started":
            ev["when"] = g["when"]
        elif kind == "build":
            ev["build"] = g["build"]
        elif kind == "executable":
            ev["channel"] = channel_from_exe(g["path"])
        elif kind in ("session_id", "env_session"):
            ev["value"] = g["v"]
        elif kind == "join":
            ev.update(address=g["address"] or None, port=int(g["port"]) if g["port"] else None,
                      shard=g["shard"] or None, location_id=g["loc"])
        elif kind == "shard":
            ev["shard"] = g["shard"]
        elif kind == "loading":
            ev["system"] = system_name(g["system"].strip())
        return ev
    return None


def channel_from_exe(path: str) -> str | None:
    parts = [p for p in re.split(r"[\\/]", path) if p]
    for i, p in enumerate(parts):
        if p.lower() == "bin64" and i:
            return parts[i - 1].upper()
    return None


def system_name(raw: str) -> str:
    return "Main menu" if raw.lower().startswith("frontend") else raw


def parse_shard(shard: str | None) -> dict:
    """"ptu_use1b_13248297_020" -> {region_code, region_name, shard_short}. Unknown parts stay raw."""
    out = {"region_code": None, "region_name": None, "shard_short": None}
    if not shard:
        return out
    parts = shard.split("_")
    if len(parts) < 2:
        out["shard_short"] = shard.upper()
        return out
    code = parts[1].lower()
    out["region_code"] = code
    out["region_name"] = next((name for pre, name in sorted(REGIONS.items(), key=lambda kv: -len(kv[0]))
                               if code.startswith(pre)), code.upper())
    out["shard_short"] = code.upper() + (" · " + parts[-1] if len(parts) >= 3 else "")
    return out


def public(session: dict | None) -> dict | None:
    """Session without connection/session identifiers (for recordings and anything persisted)."""
    if session is None:
        return None
    return {k: v for k, v in session.items() if k not in PRIVATE_FIELDS}


def public_view(session: dict | None) -> dict | None:
    """public() plus has_* flags, so a client can offer "Reveal" without ever receiving the values."""
    if session is None:
        return None
    return {**public(session), "has_server_address": bool(session.get("server_address")),
            "has_session_ids": bool(session.get("session_id") or session.get("env_session"))}


def private(session: dict | None) -> dict:
    """Only the private fields (served by the host/origin-guarded /api/session/private)."""
    return {k: (session or {}).get(k) for k in PRIVATE_FIELDS}


class SessionState:
    """Folds parsed lines into session fields; apply() returns session events to surface."""

    def __init__(self, channel: str | None = None):
        self.default_channel = channel
        self.reset()

    def reset(self):
        self.f = dict.fromkeys(FIELDS)
        self.f["channel"] = self.default_channel
        self.shard_short = None

    def as_dict(self) -> dict:
        return {**self.f, "shard_short": self.shard_short}

    def _set_shard(self, shard):
        self.f["shard"] = shard
        p = parse_shard(shard)
        self.f["region_code"], self.f["region_name"], self.shard_short = (
            p["region_code"], p["region_name"], p["shard_short"])

    def apply(self, ev: dict) -> list[dict]:
        """-> [{"type": SESSION_*, "t": epoch|None, "detail": str}] (never carries IP/ids)."""
        k, t, out = ev["kind"], parse_ts(ev.get("ts")), []
        if k == "log_started":
            self.reset()
            self.f["log_started_at"] = ev.get("ts") or ev.get("when")
        elif k == "build":
            self.f["build"] = ev["build"]
        elif k == "executable":
            self.f["channel"] = ev["channel"] or self.f["channel"]
        elif k in ("session_id", "env_session"):
            self.f[k] = ev["value"]
        elif k == "join":
            self._set_shard(ev["shard"])
            self.f.update(server_address=ev["address"], server_port=ev["port"],
                          location_id=ev["location_id"], joined_at=ev.get("ts"))
            out.append({"type": "SESSION_JOIN", "t": t, "detail": self._shard_label()})
        elif k == "shard" and ev["shard"] != self.f["shard"]:
            had = self.f["shard"] is not None
            self._set_shard(ev["shard"])
            if had:
                out.append({"type": "SESSION_SHARD_CHANGE", "t": t, "detail": self._shard_label()})
        elif k == "loading" and ev["system"] != self.f["system"]:
            if ev["system"] == "Main menu" and (self.f["joined_at"] or self.f["shard"]):
                left = self._shard_label()  # back at the menu: the shard session is over
                self._set_shard(None)
                self.f.update(server_address=None, server_port=None, location_id=None, joined_at=None)
                out.append({"type": "SESSION_LEAVE", "t": t, "detail": left})
            self.f["system"] = ev["system"]
            out.append({"type": "SESSION_SYSTEM", "t": t, "detail": ev["system"]})
        return out

    def _shard_label(self):
        s = self.f["shard"] or "?"
        return f"{s} ({self.f['region_name']})" if self.f["region_name"] else s


# ---- signals: nearby ships, QT arrivals, HUD zone notices, vehicle control, corpse items ---------
# Unofficial PTU 5.0 debug lines (docs/telemetry/gamelog-signals.md). Entity ids are used INTERNALLY
# only (de-duplication, own-ship exclusion); nothing returned by public()/events carries one.
NEARBY_CAP = 50        # classes in the public nearby list (most recently seen first)
ENTITY_CAP = 1000      # entities remembered per session (oldest-seen evicted)
BURST_S = 1.0          # at most one NEARBY_SHIP event per second; a burst becomes "N new ships"
CORPSE_WINDOW_S = 5.0  # corpse item lines within this many seconds (log time) = one corpse (ANY corpse nearby: no owner in the line)
RESET_ON = ("SESSION_JOIN", "SESSION_SHARD_CHANGE", "SESSION_LEAVE")
ZONE_KINDS = ("armistice", "monitored", "ungoverned", "jurisdiction", "other")
_ENT = r"(?P<ent>[A-Za-z][A-Za-z0-9_]*?_\d{6,})(?:\[\d+\])?"
_ENT_FULL = re.compile(r"^(?P<cls>[A-Za-z][A-Za-z0-9_]*?)_(?P<eid>\d{6,})$")
_SIG_RULES = [  # (kind, cheap substring prefilter, regex)
    ("nearby", "<Local Route Guard", re.compile(
        r"<Local Route Guard - [^>]*>.*?\| NOT [A-Z]+ \| " + _ENT + r"\|CSCItemNavigation::PostInitialize")),
    ("qt_arrived", "<Quantum Drive Arrived", re.compile(
        r"<Quantum Drive Arrived - [^>]*>.*?\| NOT [A-Z]+ \| " + _ENT + r"\|")),
    ("notification", "<SHUDEvent_OnNotification>", re.compile(
        r'<SHUDEvent_OnNotification> Added notification "(?P<text>[^"]*)"')),
    ("vehicle_released", "<Vehicle Control Flow>", re.compile(
        r"<Vehicle Control Flow> CVehicleMovementBase::ClearDriver:.*?releasing control token for '" + _ENT + "'")),
    ("corpse_item", "<Adding non kept item", re.compile(
        r"<Adding non kept item \[CSCActorCorpseUtils::PopulateItemPortForItemRecoveryEntitlement\]>")),
]


def split_entity(ent: str | None) -> tuple[str, str] | None:
    """"RSI_Perseus_000000000001" -> ("RSI_Perseus", "000000000001"); None if no trailing id."""
    m = _ENT_FULL.match(ent or "")
    return (m["cls"], m["eid"]) if m else None


def display_class(cls: str | None) -> str | None:
    """Class name for people: entity id already stripped, underscores -> spaces."""
    return re.sub(r"_+", " ", cls).strip() if cls else None


def parse_signal(line: str) -> dict | None:
    """One Game.log line -> {"kind", "ts", ...} for the signal lines, else None. Pure. "eid" is internal."""
    line = line.rstrip("\r\n")
    m = _TS.match(line)
    ts, body = (m["ts"], m["body"]) if m else (None, line)
    for kind, pre, rx in _SIG_RULES:
        if pre not in body:
            continue
        g = rx.search(body)
        if not g:
            return None
        ev = {"kind": kind, "ts": ts}
        if "ent" in rx.groupindex:
            ev["class"], ev["eid"] = split_entity(g["ent"])
        elif kind == "notification":
            ev["text"] = g["text"]
        return ev
    return None


def classify_zone(text: str) -> dict | None:
    """HUD notification text -> {"kind", "text", "armistice": True|False|None, "jurisdiction"} or None
    when the notice is not about a zone (hangar, injuries, QT calibration...)."""
    t = re.sub(r"[\s:]+$", "", text or "").strip()
    low = t.lower()
    entering = low.startswith(("entered ", "entering "))
    leaving = low.startswith(("left ", "leaving ", "exited ", "exiting "))
    if not (entering or leaving):
        return None
    out = {"text": t, "armistice": None, "jurisdiction": None}
    if "armistice" in low:
        out.update(kind="armistice" if entering else "other", armistice=entering)
    elif "monitored space" in low:
        out["kind"] = "monitored" if entering else "other"
    elif "jurisdiction" in low and entering:
        name = re.sub(r"^(entered|entering)\s+|\s+jurisdiction.*$", "", t, flags=re.I).strip()
        out.update(kind="ungoverned" if name.lower() == "ungoverned" else "jurisdiction", jurisdiction=name or None)
    elif "zone" in low or "space" in low or "jurisdiction" in low:
        out["kind"] = "other"
    else:
        return None
    return out


class SignalsState:
    """Folds parse_signal() events into per-session signal state. Thread-safe: the tailer applies lines
    in a worker thread while the event loop reads public() and feeds the telemetry vehicle."""

    def __init__(self):
        self.lock = threading.RLock()
        self.version = 0
        self._tele_own: tuple[str | None, str | None] | None = None  # (class, eid) from telemetry
        self._tele_raw = None
        self._stamp = 0        # change counter: own() prefers whichever own-ship source changed last
        self._tele_stamp = 0
        self.reset()

    def reset(self):
        with self.lock:
            self.ships: collections.OrderedDict[str, list] = collections.OrderedDict()  # eid -> [cls, first, last]
            self.zone: dict | None = None
            self.armistice = False
            self.jurisdiction: str | None = None
            self.own_log: tuple[str, str] | None = None  # (class, eid) last piloted (Vehicle Control Flow)
            self._log_stamp = 0
            self._own_seen: set[str] = set()  # every entity piloted this session: yours, never "nearby"
            self._pending: list[tuple[float | None, str, str]] = []  # new sightings not yet announced
            self._corpse: list | None = None  # [burst start t, t, count, touched_this_step]
            self._last_emit = float("-inf")
            self.version += 1

    # -- own ship ------------------------------------------------------------------------------
    def own(self) -> tuple[str | None, str | None] | None:
        """Most recently changed of telemetry vehicle / last Game.log seat release (ties: telemetry)."""
        if self.own_log and (not self._tele_own or self._log_stamp > self._tele_stamp):
            return self.own_log
        return self._tele_own

    def _own_ids(self) -> set:
        return self._own_seen | {o[1] for o in (self._tele_own, self.own_log) if o and o[1]}

    def _forget(self, eid):
        self.ships.pop(eid, None)
        self._pending = [p for p in self._pending if p[2] != eid]

    def set_telemetry_vehicle(self, name, class_name=None):
        """Own vehicle from the telemetry frame (vehicle.state.name / class_name). Empty = unchanged
        (on foot keeps the last ship). Called per frame: a no-op unless the value changed."""
        raw = (name if isinstance(name, str) else "", class_name if isinstance(class_name, str) else "")
        if raw == self._tele_raw or not any(raw):
            return
        with self.lock:
            self._tele_raw = raw
            sp = split_entity(raw[0].strip())
            self._tele_own = sp if sp else ((raw[1] or None), None)
            self._stamp += 1
            self._tele_stamp = self._stamp
            if sp:
                self._own_seen.add(sp[1])  # stays yours after telemetry moves on to another ship
                self._forget(sp[1])
            self.version += 1

    # -- folding -------------------------------------------------------------------------------
    def _sight(self, cls, eid, t):
        if eid in self._own_ids():
            return
        s = self.ships.get(eid)
        if s is None:
            self.ships[eid] = [cls, t, t]
            self._pending.append((t, cls, eid))
            while len(self.ships) > ENTITY_CAP:
                self.ships.popitem(last=False)
        else:
            s[2] = t if t is not None else s[2]
            self.ships.move_to_end(eid)

    def _flush_corpse(self) -> list[dict]:
        c, self._corpse = self._corpse, None
        if not c:
            return []
        n = c[2]
        return [{"type": "CORPSE_ITEMS", "t": c[1], "count": n,
                 "detail": f"corpse nearby: {n} item{'s' if n != 1 else ''} (owner unknown)"}]

    def apply(self, ev: dict) -> list[dict]:
        """-> immediate events (QT_ARRIVED, ZONE, VEHICLE_RELEASED, CORPSE_ITEMS of a finished burst).
        New ship sightings are queued for drain() (rate limit). Events never carry an entity id."""
        k, ts = ev["kind"], ev.get("ts")
        t = parse_ts(ts)
        out: list[dict] = []
        with self.lock:
            self.version += 1
            if k == "nearby":
                self._sight(ev["class"], ev["eid"], t)
            elif k == "qt_arrived":
                own = ev["eid"] in self._own_ids()
                if not own:
                    self._sight(ev["class"], ev["eid"], t)
                disp = display_class(ev["class"])
                out.append({"type": "QT_ARRIVED", "t": t, "class": ev["class"], "self": own,
                            "detail": ("You (" + disp + ")" if own else disp) + " arrived from quantum"})
            elif k == "notification":
                z = classify_zone(ev["text"])
                if z:
                    if z["armistice"] is not None:
                        self.armistice = z["armistice"]
                    if z["jurisdiction"]:
                        self.jurisdiction = z["jurisdiction"]
                    same = self.zone and self.zone["text"] == z["text"]
                    self.zone = {"kind": z["kind"], "text": z["text"], "since": self.zone["since"] if same else t}
                    if not same:
                        out.append({"type": "ZONE", "t": t, "kind": z["kind"], "text": z["text"], "detail": z["text"]})
            elif k == "vehicle_released":
                self.own_log = (ev["class"], ev["eid"])
                self._stamp += 1
                self._log_stamp = self._stamp
                self._own_seen.add(ev["eid"])
                self._forget(ev["eid"])
                out.append({"type": "VEHICLE_RELEASED", "t": t, "class": ev["class"],
                            "detail": "Left pilot seat of " + display_class(ev["class"])})
            elif k == "corpse_item":
                c = self._corpse  # no timestamp: same burst (drain() still closes it after an idle poll)
                if c and t is not None and c[0] is not None and t - c[0] > CORPSE_WINDOW_S:
                    out += self._flush_corpse()  # a different death: close the previous list
                if self._corpse is None:
                    self._corpse = [t, t, 0, True]
                if self._corpse[0] is None:  # burst began on an untimestamped line: first real time
                    self._corpse[0] = self._corpse[1] = t
                self._corpse[2] += 1
                self._corpse[3] = True
        return out

    def drain(self, now: float | None = None) -> list[dict]:
        """End of a poll: emit the queued NEARBY_SHIP (at most one per BURST_S of wall time, aggregated)
        and a corpse list that got no new line this poll."""
        now = time.time() if now is None else now
        out: list[dict] = []
        with self.lock:
            if self._corpse:
                if self._corpse[3]:
                    self._corpse[3] = False
                else:
                    out += self._flush_corpse()
            if self._pending and now - self._last_emit >= BURST_S:
                pend, self._pending = self._pending, []
                self._last_emit = now
                classes = list(dict.fromkeys(cls for _, cls, _ in pend))
                ts = [t for t, _, _ in pend if t is not None]
                n = len(pend)
                names = ", ".join(display_class(c) for c in classes[:3]) + ("…" if len(classes) > 3 else "")
                out.append({"type": "NEARBY_SHIP", "t": max(ts) if ts else now, "count": n,
                            "class": classes[0] if n == 1 else None, "classes": classes[:10],
                            "detail": display_class(classes[0]) if n == 1 else f"{n} new ships: {names}"})
            if out:
                self.version += 1
        return out

    def public(self) -> dict:
        """Snapshot/SSE/recording view: class names, counts and times only (no entity ids)."""
        with self.lock:
            groups: dict[str, dict] = {}
            for cls, first, last in self.ships.values():
                g = groups.get(cls)
                if g is None:
                    groups[cls] = {"class": cls, "display": display_class(cls), "count": 1,
                                   "first_seen": first, "last_seen": last}
                    continue
                g["count"] += 1
                if first is not None and (g["first_seen"] is None or first < g["first_seen"]):
                    g["first_seen"] = first
                if last is not None and (g["last_seen"] is None or last > g["last_seen"]):
                    g["last_seen"] = last
            nearby = sorted(groups.values(), key=lambda g: -(g["last_seen"] or 0))[:NEARBY_CAP]
            own = self.own()
            own_pub = ({"class": own[0], "display": display_class(own[0]),
                        "source": "telemetry" if own is self._tele_own else "game_log"}
                       if own and own[0] else None)
            return {"zone": dict(self.zone) if self.zone else None, "armistice": self.armistice,
                    "jurisdiction": self.jurisdiction, "nearby": nearby, "nearby_count": len(self.ships),
                    "nearby_classes": len(groups), "own_ship": own_pub}


def default_log_path(sc_dir: str | os.PathLike | None, channel: str) -> pathlib.Path | None:
    """<install dir>/<CHANNEL>/Game.log; install dir = sc_dir, else $SC_DIR / auto-detected (paths.py).
    None when no install dir is known."""
    base = pathlib.Path(sc_dir) if sc_dir else paths.find_sc_dir()
    return base / channel.upper() / "Game.log" if base is not None else None


class GameLogTailer:
    """Follow one Game.log path. step() does one poll (sync, testable); run() loops it off the event
    loop (asyncio.to_thread) and dispatches on_change back onto the loop."""

    def __init__(self, path, channel: str | None = None, on_change=None, poll_s: float = POLL_S):
        self.path = pathlib.Path(path)
        self.state = SessionState(channel)
        self.signals = SignalsState()  # nearby ships / zone / QT / corpse, reset with the session
        self._sig_ver = None
        self.people = people.PeopleState()  # handles seen this session (memory only; scgameapi/people.py)
        self._ppl_ver = None
        self.on_change = on_change  # callable(session_dict, events) after a poll that changed anything
        self.poll_s = poll_s
        self.found = False
        self._ino = None
        self._pos = 0
        self._head = b""
        self._buf = b""
        self.rotations = 0
        self._last = None
        self.mtime: float | None = None  # last seen st_mtime: "is the game still writing?"
        self._dispatch = None            # set by run(): hands on_change calls to the event loop

    def session(self) -> dict:
        return {**self.state.as_dict(), "source": "game_log", "log_found": self.found}

    def _restart(self):
        self._pos, self._buf, self._head = 0, b"", b""
        self.state.reset()
        self.signals.reset()
        self.people.reset()

    def step(self) -> list[dict]:
        """Poll once: detect rotation, read new complete lines, fold them. Returns session events."""
        try:
            f = self.path.open("rb")  # read-only, always
        except (FileNotFoundError, NotADirectoryError):
            if self.found:  # file vanished (rename during relaunch): session ends with it
                self.found, self._ino, self.mtime = False, None, None
                self._restart()
                self._notify([])
            return []
        except OSError:
            return []
        events, rotated = [], False
        try:
            with f:
                st = os.fstat(f.fileno())  # stat the handle we read, not the path (no replace race)
                self.mtime = st.st_mtime
                head = f.read(HEAD_BYTES)
                ino_changed = USE_INODE and bool(st.st_ino and self._ino) and st.st_ino != self._ino
                if self._ino is not None and (ino_changed or st.st_size < self._pos
                                              or (self._head and not head.startswith(self._head[:len(head)]))):
                    rotated = True
                    self.rotations += 1
                    self._restart()
                self._ino = st.st_ino
                if len(head) > len(self._head):
                    self._head = head
                f.seek(self._pos)
                budget = READ_MAX_PER_POLL
                while (budget is None or budget > 0) and (chunk := f.read(READ_CHUNK if budget is None
                                                                       else min(READ_CHUNK, budget))):
                    if budget is not None:
                        budget -= len(chunk)
                    self._pos += len(chunk)
                    self._buf += chunk
                    *lines, self._buf = self._buf.split(b"\n")  # partial last line stays buffered
                    for raw in lines:
                        events += self._fold(raw.decode("utf-8", "replace"))
                events += self.signals.drain()
        except OSError:
            return []
        changed = (rotated or not self.found or events or self.state.as_dict() != self._last
                   or self.signals.version != self._sig_ver or self.people.version != self._ppl_ver)
        self.found = True
        if changed:
            self._notify(events)
        return events

    def _fold(self, line: str) -> list[dict]:
        self.people.feed(line)  # own parser + toast joiner; never emits events (they would carry names)
        ev = parse_line(line)
        if ev:
            evs = self.state.apply(ev)
            if ev["kind"] == "log_started":
                self.people.reset()  # new launch: identity and roster start over
            if ev["kind"] == "log_started" or any(e["type"] in RESET_ON for e in evs):
                self.signals.reset()  # new launch / shard: ships and zone of the old one are history
                self.people.clear_nearby()
            return evs
        sg = parse_signal(line)
        return self.signals.apply(sg) if sg else []

    def _notify(self, events):
        self._last = self.state.as_dict()
        self._sig_ver = self.signals.version
        self._ppl_ver = self.people.version
        if not self.on_change:
            return
        if self._dispatch:
            self._dispatch(self.session(), events)
        else:
            self._safe_cb(self.session(), events)

    def _safe_cb(self, session, events):
        try:
            self.on_change(session, events)
        except Exception as e:  # noqa: BLE001 - a consumer failure (e.g. --record write) must not stop tailing
            print(f"[gamelog] on_change failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    async def run(self):
        loop = asyncio.get_running_loop()
        self._dispatch = lambda s, e: loop.call_soon_threadsafe(self._safe_cb, s, e)
        while True:
            try:
                await asyncio.to_thread(self.step)  # stat/read/regex stay off the event loop
            except Exception as e:  # noqa: BLE001 - keep tailing; never escalates to the bridge status
                print(f"[gamelog] poll failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                traceback.print_exc()
            await asyncio.sleep(self.poll_s)

    def log_age(self) -> float | None:
        """Seconds since Game.log was last written (None = unknown / not found)."""
        return max(0.0, time.time() - self.mtime) if self.mtime is not None and self.found else None


def mock_session() -> tuple[dict, list[dict]]:
    """Synthetic session for --mock (TEST-NET address, fake ids)."""
    now = dt.datetime.now(dt.UTC)
    iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    st = SessionState("MOCK")
    st.f.update(build="0.0.0.0", env_session="mock-env-session", session_id="00000000-0000-0000-0000-000000000000",
                log_started_at=iso)
    evs = st.apply({"kind": "join", "ts": iso, "address": "203.0.113.10", "port": 64000,
                    "shard": "mock_use1b_0_001", "location_id": "0"})
    evs += st.apply({"kind": "loading", "ts": iso, "system": "Stanton"})
    return {**st.as_dict(), "source": "mock", "log_found": False}, evs


def mock_signals() -> tuple[SignalsState, list[dict]]:
    """Synthetic signals for --mock: fake entity ids (_0000000000NN), own ship = the mock's Gladius."""
    now = dt.datetime.now(dt.UTC)

    def ts(ago_s):
        t = now - dt.timedelta(seconds=ago_s)
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

    nav = "[Notice] <Local Route Guard - Server Rerouted> [ItemNavigation][CL][1] | NOT AUTH | {e}|CSCItemNavigation::PostInitialize::<lambda_1>::operator ()|FinalStop=0"
    hud = '[Notice] <SHUDEvent_OnNotification> Added notification "{t}: " [0] to queue.'
    lines = [
        (300, hud.format(t="Entered Monitored Space")),
        (290, "[Notice] <Vehicle Control Flow> CVehicleMovementBase::ClearDriver: Local client node [1] "
              "releasing control token for 'AEGS_Gladius_000000000001' [1]"),
        (240, nav.format(e="DRAK_Cutlass_Black_000000000011")),
        (200, nav.format(e="AEGS_Avenger_Titan_000000000012")),
        (180, nav.format(e="DRAK_Cutlass_Black_000000000013")),
        (120, "[Notice] <Quantum Drive Arrived - Arrived at Final Destination> [ItemNavigation][CL][1] | NOT AUTH | "
              "MISC_Freelancer_000000000014[000000000014]|CSCItemNavigation::OnQuantumDriveArrived|arrived"),
        (60, hud.format(t="Entering Armistice Zone - Combat Prohibited")),
        (30, nav.format(e="RSI_Constellation_Andromeda_000000000015")),
    ]
    st, evs = SignalsState(), []
    for ago, body in lines:
        evs += st.apply(parse_signal(f"<{ts(ago)}> {body}"))
    evs += st.drain(time.time())
    return st, evs
