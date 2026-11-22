"""People seen in Game.log (crew / party / nearby) and an opt-in RSI citizen/org lookup.

Privacy model (docs/telemetry/people-and-lookup.md):
- Handles live in memory only, for the current Game.log session. They are never written to --record
  recordings, events, logs or disk, with one documented exception: the opt-in org lookup cache
  (.tmp/rsi-cache.json, gitignored, clearable).
- PeopleState.public(names=False) carries counts only. Names leave the bridge only when "Show names"
  is on AND the request passes the same Origin/Host guard as /api/session/private.
- OrgLookup never touches the network unless "Look up orgs" is on; then it fetches at most one page at a
  time, at least MIN_INTERVAL_S apart, with a fixed User-Agent that carries no personal data.

Handle sources (verified against 1.3 GB of LIVE/PTU logs, see the doc): Vehicle Destroyed killer (+ victim via
an id->handle map), QT calibration toasts, party toasts / party-launch lines, voice-channel toasts and
"started to QT to your current location". Self-only lines (comms notifications, hangar owner, login lines)
only feed the local identity.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import datetime as dt
import html.parser
import http.client
import json
import os
import pathlib
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PEOPLE_CAP = 300               # people remembered per session (least recently seen nearby evicted first)
ID_MAP_CAP = 2000              # Vehicle Destroyed player id -> handle entries
TOAST_MAX_LINES = 5            # notification joiner gives up after this many physical lines
NAME_MAX = 64
RELATIONS = ("crew", "party", "party_likely", "nearby")   # strongest first
RELATION_LABEL = {"self": "you", "crew": "crew", "party": "party", "party_likely": "party?", "nearby": "nearby"}

HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{2,60}$")   # RSI handle charset; anything else is shown, never linked
_NPC_RE = re.compile(r"^(?:PU_|AI_|Citizen-\d+$)|_\d{6,}$", re.IGNORECASE)
_TS = re.compile(r"^<(?P<ts>\d{4}-\d\d-\d\dT[\d:.]+Z)>\s?(?P<body>.*)$")
_TAGGED = re.compile(r"^(?:\[SPAM \d+\]\s*)?\[\w+\] <")
_NOTE_START = '<SHUDEvent_OnNotification> Added notification "'
_NOTE_END = re.compile(r'^(?P<text>.*?)" \[\d+\] to queue', re.DOTALL)

# local identity (self-only lines): handle / geid are compared, never emitted as "other"
_SELF_RULES = [
    ("handle", "Handle[", re.compile(r"User Login Success - Handle\[(?P<v>[^\]]+)\]")),
    ("handle", "handle[", re.compile(r"Fully authenticated : handle\[(?P<v>[^\]]+)\]")),
    ("handle", "destinations received", re.compile(r"destinations received for player \[(?P<v>[^\]]+)\]")),
    ("char", "AccountLoginCharacterStatus", re.compile(
        r"<AccountLoginCharacterStatus_Character> Character: .*?geid (?P<geid>\d+) .*?- name (?P<v>\S+) - state")),
    ("geid", "ClearDriver", re.compile(r"ClearDriver: Local client node \[(?P<geid>\d+)\]")),
]
_VD = re.compile(r"<Vehicle Destroyed> Vehicle '[^']*' \[\d+\] class '(?P<cls>[\w-]+)' "
                 r"\(owner player \[(?P<owner>\d+)\], driver \[\d+\]\) destroyed: reason \[(?P<reason>\w+)\] "
                 r".*?killer '(?P<killer>[^']*)' \[(?P<kid>\d+)\]")
_LAUNCH = re.compile(r"party-launch from leader\[(?P<p>[^\]]*)\]")

# toast rules: applied to each line of the joined toast text (title first)
_T_CAL = re.compile(r"^Quantum Travel Calibration (?P<phase>Started|Complete) By (?P<p>.*)$")
_T_YOU_CHANNEL = re.compile(r"^You have joined channel '(?P<ship>.+) : (?P<owner>[^']*)'\.?$")
_T_MEMBER_CHANNEL = re.compile(r"^(?P<p>.+?) has (?P<act>joined|left) the channel '(?P<ship>.+) : (?P<owner>[^']*)'\.?$")
_T_PARTY_JOIN = re.compile(r"^(?P<p>.+?) has joined the party\.?$")
_T_PARTY_LEFT = re.compile(r"^(?P<p>.*?) has left the party\.?$")
_T_LEADER = re.compile(r"^(?P<p>.+?) is now party leader\.?$")
_T_BY_LEADER = re.compile(r"^(?:Initiated by|Join queue canceled by) party leader (?P<p>.+?)\.?$")
_T_INITIATED = re.compile(r"^Initiated by (?P<p>.+?)\.?$")
_T_CONNECTED = re.compile(r"^(?P<p>.+?) (?P<act>connected|disconnected)\.?$")
_T_QT_INCOMING = re.compile(r"^(?P<p>.+?) has started to QT to your current location\.?$")
_NOT_NAMES = frozenset({"party", "the party"})   # body words of `You have joined` that are not a leader


def parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    with contextlib.suppress(ValueError):
        return dt.datetime.fromisoformat(ts).timestamp()
    return None


def is_npc(name: str | None) -> bool:
    """AI / unnamed actors: empty, "none", PU_* / AI_* classes, Citizen-<n>, or a trailing entity id. Never a
    blanket "PREFIX_" rule: real handles contain underscores."""
    n = (name or "").strip()
    return not n or n.lower() == "none" or bool(_NPC_RE.search(n))


def clean_name(name: str | None) -> str | None:
    """Validated display name from a log field: printable, <= NAME_MAX chars, not an NPC. Else None."""
    n = (name or "").strip()
    if not n or len(n) > NAME_MAX or not n.isprintable() or "'" in n or '"' in n or is_npc(n):
        return None
    return n


def valid_handle(name: str | None) -> bool:
    return bool(name) and bool(HANDLE_RE.fullmatch(name))


def citizen_url(handle: str) -> str:
    return "https://robertsspaceindustries.com/citizens/" + urllib.parse.quote(handle, safe="")


def toast_hits(text: str) -> list[tuple[str, str, str, dict]]:
    """Joined toast text -> [(relation, name, source, extra)]. Pure. `text` = title + "\\n" + body lines,
    trailing ': ' already stripped."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return []
    title, out = lines[0], []
    m = _T_CAL.match(title)
    if m:
        return [("party_likely", m["p"], "qt_calibration", {"phase": m["phase"].lower()})]
    m = _T_YOU_CHANNEL.match(title)
    if m:
        return [("crew", m["owner"], "voice_channel", {"ship": m["ship"], "owner": True})]
    if title == "You have joined" and len(lines) == 2:   # you joined <leader>'s party
        # real logs (240 files) only ever carry the literal word "party" here: never treat it as a name
        if lines[1].casefold() not in _NOT_NAMES and valid_handle(lines[1]):
            return [("party", lines[1], "party_toast", {"leader": True})]
        return []
    for i, ln in enumerate(lines):
        if (m := _T_MEMBER_CHANNEL.match(ln)):
            out.append(("crew", m["p"], "voice_channel", {"ship": m["ship"], "left": m["act"] == "left"}))
            out.append(("crew", m["owner"], "voice_channel", {"ship": m["ship"], "owner": True}))
        elif i > 0 and title == "New Member Joined" and (m := _T_PARTY_JOIN.match(ln)):
            out.append(("party", m["p"], "party_toast", {}))
        elif i > 0 and title == "Member Left" and (m := _T_PARTY_LEFT.match(ln)):
            out.append(("party", m["p"], "party_toast", {"left": True}))
        elif (i > 0 and title == "New Party Leader" and (m := _T_LEADER.match(ln))) or (m := _T_BY_LEADER.match(ln)):
            out.append(("party", m["p"], "party_toast", {"leader": True}))
        elif (m := _T_QT_INCOMING.match(ln)):
            out.append(("party", m["p"], "qt_incoming", {}))
        elif (m := _T_INITIATED.match(ln)) and (i > 0 or len(lines) > 1 and lines[1].startswith("Party Launch")):
            out.append(("party", m["p"], "party_launch", {"leader": True}))
        elif i > 0 and title == "Party" and (m := _T_CONNECTED.match(ln)):
            out.append(("party", m["p"], "party_toast", {}))
    return out


class PeopleState:
    """Folds raw Game.log lines into a per-session people table. Thread-safe (the tailer feeds lines in a
    worker thread; the event loop reads public())."""

    def __init__(self):
        self.lock = threading.RLock()
        self.version = 0
        self.reset()

    def reset(self):
        """New file / Log started: identity and every person are forgotten."""
        with self.lock:
            self.self_handles: set[str] = set()   # casefolded; several aliases per file are possible
            self.self_display: str | None = None
            self.self_ids: set[str] = set()
            self.people: collections.OrderedDict[str, dict] = collections.OrderedDict()  # casefold -> entry
            self._ids: collections.OrderedDict[str, str] = collections.OrderedDict()     # player id -> handle
            self._victims: collections.OrderedDict[str, float | None] = collections.OrderedDict()
            self._toast: list[str] | None = None
            self._toast_ts: str | None = None
            self.version += 1

    def clear_nearby(self):
        """Shard join/change/leave: nearby-only people are history; crew and party survive."""
        with self.lock:
            for k in [k for k, p in self.people.items() if set(p["relations"]) <= {"nearby"}]:
                del self.people[k]
            self._victims.clear()
            self.version += 1

    # -- identity ------------------------------------------------------------------------------
    def is_self(self, name: str | None, pid: str | None = None) -> bool:
        return bool(name and name.casefold() in self.self_handles) or bool(pid and pid in self.self_ids)

    def _add_self(self, handle: str | None = None, geid: str | None = None):
        if handle and clean_name(handle):
            self.self_handles.add(handle.casefold())
            self.self_display = self.self_display or handle
            self.people.pop(handle.casefold(), None)   # was recorded as "other" before identity was known
        if geid:
            self.self_ids.add(geid)
        self.version += 1

    # -- folding -------------------------------------------------------------------------------
    def _see(self, rel: str, name: str | None, source: str, t: float | None, extra: dict | None = None):
        n = clean_name(name)
        if n is None or self.is_self(n):
            return
        k = n.casefold()
        p = self.people.get(k)
        if p is None:
            p = {"name": n, "relations": [], "sources": [], "first_seen": t, "last_seen": t, "count": 0,
                 "ship": None, "left": False, "leader": False}
            self.people[k] = p
            while len(self.people) > PEOPLE_CAP:
                victim = next((kk for kk, pp in self.people.items() if set(pp["relations"]) <= {"nearby"}),
                              next(iter(self.people)))
                del self.people[victim]
        if rel not in p["relations"]:
            p["relations"].append(rel)
            p["relations"].sort(key=RELATIONS.index)
        if source not in p["sources"]:
            p["sources"].append(source)
        p["count"] += 1
        if t is not None:
            p["last_seen"] = t if p["last_seen"] is None else max(p["last_seen"], t)
            p["first_seen"] = t if p["first_seen"] is None else min(p["first_seen"], t)
        ex = extra or {}
        if ex.get("ship"):
            p["ship"] = ex["ship"]
        if "left" in ex or rel in ("party", "crew"):
            p["left"] = bool(ex.get("left"))
        if ex.get("leader"):
            p["leader"] = True
        self.people.move_to_end(k)
        self.version += 1

    def feed(self, line: str) -> None:
        """One raw Game.log line (any line; cheap substring prefilters skip the rest)."""
        line = line.rstrip("\r\n")
        m = _TS.match(line)
        ts, body = (m["ts"], m["body"]) if m else (None, line)
        with self.lock:
            if self._toast is not None:
                if _TAGGED.match(body) or body.startswith((" ", "\t")) or not m:
                    self._toast = None   # abandoned (queue dump, tagged line): emit nothing for it
                else:
                    self._toast.append(body)
                    self._try_toast()
                    return
            i = body.find(_NOTE_START)
            if i >= 0:
                self._toast, self._toast_ts = [body[i + len(_NOTE_START):]], ts
                self._try_toast()
                return
            if "Vehicle Destroyed" in body:
                g = _VD.search(body)
                if g:
                    self._vehicle_destroyed(g, parse_ts(ts))
                return
            if "party-launch from leader" in body:
                g = _LAUNCH.search(body)
                if g:
                    self._see("party", g["p"], "party_launch", parse_ts(ts), {"leader": True})
                return
            for kind, pre, rx in _SELF_RULES:
                if pre in body:
                    g = rx.search(body)
                    if g:
                        gd = g.groupdict()
                        self._add_self(gd.get("v"), gd.get("geid"))
                    return

    def _try_toast(self):
        text = "\n".join(self._toast)
        e = _NOTE_END.match(text)
        if e is None:
            if len(self._toast) >= TOAST_MAX_LINES:
                self._toast = None
            return
        self._toast = None
        body = re.sub(r"[:\s]+$", "", e["text"])
        t = parse_ts(self._toast_ts)
        for rel, name, source, extra in toast_hits(body):
            self._see(rel, name, source, t, extra)

    def _vehicle_destroyed(self, g, t):
        killer, kid, owner = g["killer"], g["kid"], g["owner"]
        if killer and not is_npc(killer) and kid != "0":
            if self.is_self(killer, kid):
                self.self_ids.add(kid)
            else:
                self._ids[kid] = killer
                while len(self._ids) > ID_MAP_CAP:
                    self._ids.popitem(last=False)
                self._see("nearby", killer, "vehicle_destroyed", t)
                if kid in self._victims:   # was a victim earlier, its id now resolves
                    self._victims.pop(kid)
        if owner and owner != "0" and owner not in self.self_ids:
            h = self._ids.get(owner)
            if h:
                self._see("nearby", h, "vehicle_destroyed", t)
            else:
                self._victims[owner] = t
                while len(self._victims) > ID_MAP_CAP:
                    self._victims.popitem(last=False)

    # -- views ---------------------------------------------------------------------------------
    def counts(self) -> dict:
        with self.lock:
            c = {r: 0 for r in RELATIONS}
            for p in self.people.values():
                c[p["relations"][0]] += 1
            return {**c, "total": len(self.people), "self_known": bool(self.self_handles)}

    def handles(self) -> list[str]:
        """Lookup candidates (valid RSI handles, self first, then strongest relation, most recent)."""
        with self.lock:
            ppl = sorted(self.people.values(), key=lambda p: (RELATIONS.index(p["relations"][0]),
                                                              -(p["last_seen"] or 0)))
            out = [self.self_display] if self.self_display and valid_handle(self.self_display) else []
            return out + [p["name"] for p in ppl if valid_handle(p["name"])]

    def public(self, names: bool = False, orgs=None) -> dict:
        """names=False: counts only (safe for any client). names=True: the roster, for the guarded API /
        a same-origin snapshot while Show names is on. `orgs(handle)` -> lookup dict | None."""
        out = {"counts": self.counts(), "people": None, "self": None}
        if not names:
            return out
        with self.lock:
            rows = []
            for p in sorted(self.people.values(), key=lambda p: (RELATIONS.index(p["relations"][0]),
                                                                 -(p["last_seen"] or 0))):
                ok = valid_handle(p["name"])
                rows.append({"name": p["name"], "relation": p["relations"][0], "relations": list(p["relations"]),
                             "label": RELATION_LABEL[p["relations"][0]], "sources": list(p["sources"]),
                             "first_seen": p["first_seen"], "last_seen": p["last_seen"], "count": p["count"],
                             "ship": p["ship"], "left": p["left"], "leader": p["leader"], "valid_handle": ok,
                             "url": citizen_url(p["name"]) if ok else None,
                             "org": orgs(p["name"]) if orgs and ok else None})
            me = self.self_display
            out["people"] = rows
            out["self"] = ({"name": me, "label": "you", "valid_handle": valid_handle(me),
                            "url": citizen_url(me) if valid_handle(me) else None,
                            "org": orgs(me) if orgs and valid_handle(me) else None} if me else None)
        return out


def mock_people() -> PeopleState:
    """Synthetic people for --mock (obviously fake handles)."""
    now = dt.datetime.now(dt.UTC)

    def ts(ago):
        t = now - dt.timedelta(seconds=ago)
        return "<" + t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z> "

    note = '[Notice] <SHUDEvent_OnNotification> Added notification "'
    tail = ': " [1] to queue. New queue size: 1, MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] [Team_X]'
    st = PeopleState()
    for ago, line in [
        (400, "[Notice] <Legacy login response> [CIG-net] User Login Success - Handle[TestPilot_Self] - Time[1]"),
        (300, note + "You have joined"), (300, "TestPilot_Lead" + tail),
        (280, note + "New Member Joined"), (280, "TestPilot_B has joined the party." + tail),
        (200, note + "You have joined channel 'Drake Cutlass Black : TestPilot_Lead'."), (200, tail.lstrip(": ")),
        (120, note + "Quantum Travel Calibration Started By TestPilot_C" + tail),
        (60, ("[Notice] <Vehicle Destroyed> Vehicle 'AEGS_Gladius_000000000031' [000000000031] class 'AEGS_Gladius' "
             "(owner player [000000000041], driver [0]) destroyed: reason [Combat] level [0] -> [2] flags [0] "
             "killer 'TestPilot_D' [000000000042] authority [0] [Team_X][Vehicle]")),
        (50, ("[Notice] <Vehicle Destroyed> Vehicle 'ANVL_Arrow_000000000032' [000000000032] class 'ANVL_Arrow' "
             "(owner player [000000000042], driver [0]) destroyed: reason [Combat] level [0] -> [2] flags [0] "
             "killer 'PU_Pilots-Human-Criminal-Fighter_000000000043' [000000000043] authority [0] [Team_X][Vehicle]")),
    ]:
        st.feed(ts(ago) + line)
    return st


# ---- RSI citizen / org lookup (opt-in) ---------------------------------------------------------------
USER_AGENT = "scgameapi/0.1 (local telemetry dashboard)"
MIN_INTERVAL_S = 3.0
TIMEOUT_S = 10.0
TTL_S = 7 * 86400
NEG_TTL_S = 86400
ERROR_RETRY_S = 3600
BODY_MAX = 2 << 20
CACHE_VERSION = 1
CACHE_MAX = 2000               # entries kept on disk (oldest dropped first)
ERROR_TRIP = 3                 # consecutive failed lookups that pause the worker like a 429
FIELD_MAX = 100                # chars kept per parsed field
PARSE_DEPTH_MAX = 400          # open-element depth / entry count past which a page is "not recognised"
PARSE_ENTRIES_MAX = 500


class _TooComplex(Exception):
    """Page exceeds PARSE_DEPTH_MAX / PARSE_ENTRIES_MAX: not a citizen page we understand."""


def _cap(v: str | None) -> str | None:
    return v[:FIELD_MAX] if isinstance(v, str) else v


class _PageParser(html.parser.HTMLParser):
    """Collects (section, label, value, href) entries from the public citizen page's `p.entry` blocks.
    section = "org" inside the `main-org` box, else "profile"."""

    VOID = frozenset({"img", "br", "meta", "link", "input", "hr", "source", "wbr", "area", "base", "col", "embed", "param",
            "track"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.entries: list[tuple[str, str | None, str, str | None]] = []
        self.label: str | None = None
        self.org_classes: set[str] | None = None
        self.org_empty = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = set((a.get("class") or "").split())
        if "main-org" in cls:
            self.org_classes = cls
        if tag in self.VOID:
            return
        if tag == "p" and "entry" in cls:
            self.label = None
        if len(self.stack) >= PARSE_DEPTH_MAX:
            raise _TooComplex
        self.stack.append({"tag": tag, "cls": cls, "text": [], "href": a.get("href")})

    def handle_endtag(self, tag):
        if tag in self.VOID or not any(f["tag"] == tag for f in self.stack):
            return
        while self.stack:
            f = self.stack.pop()
            text = re.sub(r"\s+", " ", "".join(f["text"])).strip()
            if self.stack:
                self.stack[-1]["text"].append("".join(f["text"]))
            if "label" in f["cls"]:
                self.label = text
            elif "value" in f["cls"]:
                sec = "org" if any("main-org" in g["cls"] for g in self.stack) else "profile"
                self.entries.append((sec, _cap(self.label), _cap(text), f["href"]))
                if len(self.entries) > PARSE_ENTRIES_MAX:
                    raise _TooComplex
            elif "empty" in f["cls"] and any("main-org" in g["cls"] for g in self.stack):
                self.org_empty = True
            if f["tag"] == tag:
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1]["text"].append(data)


def parse_citizen(page: str) -> dict | None:
    """Public citizen page HTML -> {handle, moniker, enlisted, org} or None when the page does not look like
    a citizen profile (layout changed / blocked). Pure; tolerant of missing fields."""
    p = _PageParser()
    try:
        p.feed(page or "")
        p.close()
    except Exception:  # noqa: BLE001 - html.parser is lenient; anything else is "unparseable"
        return None
    prof = [e for e in p.entries if e[0] == "profile"]
    orge = [e for e in p.entries if e[0] == "org"]
    by = {}
    for _, label, value, _h in prof:
        if label and label.lower() not in by:
            by[label.lower()] = value
    handle = by.get("handle name")
    if not handle:   # a `profile` box without a handle is a challenge / changed layout, never a profile
        return None
    moniker = next((v for _, label, v, _h in prof if not label and v), None)
    org = None
    vis = None
    if p.org_classes is not None:
        vis_cls = next((c for c in p.org_classes if c.startswith("visibility-")), "")
        vis = {"visibility-V": "public", "visibility-R": "redacted", "visibility-H": "hidden"}.get(vis_cls)
    if orge and vis not in ("redacted", "hidden"):
        ob = {label.lower(): v for _, label, v, _h in orge if label}
        name = next((v for _, label, v, _h in orge if not label and v), None)
        sid = ob.get("spectrum identification (sid)")
        if not sid:
            href = next((h for *_x, h in orge if h and "/orgs/" in h), None)
            sid = href.rstrip("/").rsplit("/", 1)[-1] if href else None
        org = {"name": name, "sid": _cap(sid), "rank": ob.get("organization rank"), "visibility": vis or "public"}
    elif vis in ("redacted", "hidden"):
        org = {"name": None, "sid": None, "rank": None, "visibility": vis}
    return {"handle": handle, "moniker": moniker, "enlisted": by.get("enlisted"), "org": org,
            "no_main_org": org is None}


_AFFIL_RE = re.compile(r'class="[^"]*\bbox-content\b[^"]*\borg\b[^"]*\baffiliation\b[^"]*"')
_MAINORG_RE = re.compile(r'class="[^"]*\bbox-content\b[^"]*\borg\b[^"]*\bmain\b[^"]*"')


def parse_affiliations(page: str) -> int | None:
    """Citizen organizations tab -> number of affiliations (main org not counted), None if unrecognised."""
    page = page or ""
    n = len(_AFFIL_RE.findall(page))
    if n or _MAINORG_RE.search(page) or "orgs-content" in page or "NO ORG" in page.upper():
        return n
    return None


def urllib_fetch(url: str, timeout: float = TIMEOUT_S) -> tuple[int, str]:
    """Blocking GET -> (status, text). 4xx/5xx come back as their status with an empty body."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html",
                                               "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(BODY_MAX).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def synthetic_citizen_html(handle: str, moniker: str | None = None, org: dict | None = None,
                           enlisted: str = "Jan 1, 2950", org_visibility: str = "V") -> str:
    """Synthetic page modelled on the public citizen page structure (tests and --fake-rsi). No real data."""
    if org:
        org_box = (f'<div class="main-org right-col visibility-{org_visibility}"><div class="inner clearfix">'
                   f'<div class="thumb"><a href="/orgs/{org["sid"]}"><img src="/x.png"></a></div><div class="info">'
                   f'<p class="entry"><a href="/orgs/{org["sid"]}" class="value data14">{org["name"]}</a></p>'
                   f'<p class="entry"><span class="label data14">Spectrum Identification (SID)</span>'
                   f'<strong class="value data14">{org["sid"]}</strong></p>'
                   f'<p class="entry"><span class="label data14">Organization rank</span>'
                   f'<strong class="value data14">{org.get("rank", "Member")}</strong></p></div></div></div>')
    else:
        org_box = ('<div class="main-org right-col visibility-"><div class="inner clearfix">'
                   '<div class="empty">NO MAIN ORG FOUND IN PUBLIC RECORDS</div></div></div>')
    return ('<!doctype html><html><head><title>' + handle + ' | Citizen</title></head><body><div id="public-profile">'
            '<div class="profile-content overview-content clearfix"><div class="box-content profile-wrapper clearfix">'
            '<div class="inner-bg clearfix"><div class="profile left-col"><div class="inner clearfix">'
            '<div class="thumb"><img src="/a.png"></div><div class="info">'
            f'<p class="entry"><strong class="value">{moniker or handle}</strong></p>'
            f'<p class="entry"><span class="label">Handle name</span><strong class="value">{handle}</strong></p>'
            '</div></div></div>' + org_box + '</div></div>'
            '<div class="left-col"><div class="inner"><p class="entry"><span class="label">Enlisted</span>'
            f'<strong class="value">{enlisted}</strong></p></div></div></div></div></body></html>')


def synthetic_orgs_html(affiliations: int, main: bool = True) -> str:
    blocks = ('<div class="box-content org main visibility-V"><div class="inner-bg">MAIN</div></div>' if main else "")
    blocks += "".join('<div class="box-content org affiliation visibility-V"><div class="inner-bg">A</div></div>'
                      for _ in range(affiliations))
    return f'<html><body><div class="orgs-content clearfix">{blocks}</div></body></html>'


def fake_fetch(url: str, timeout: float = TIMEOUT_S) -> tuple[int, str]:
    """Offline stand-in for urllib_fetch (--fake-rsi and tests): handles starting with Ghost -> 404,
    Blocked -> 429, NoOrg -> no main org, Redacted -> redacted org; others get a synthetic org."""
    path = urllib.parse.urlparse(url).path.split("/")
    h = urllib.parse.unquote(path[2]) if len(path) > 2 else ""
    if h.startswith("Ghost"):
        return 404, ""
    if h.startswith("Blocked"):
        return 429, ""
    if len(path) > 3 and path[3] == "organizations":
        return 200, synthetic_orgs_html(0 if h.startswith("NoOrg") else 2, main=not h.startswith("NoOrg"))
    if h.startswith("NoOrg"):
        return 200, synthetic_citizen_html(h)
    org = {"name": "Test Org " + h[-1:].upper(), "sid": "TST" + h[-1:].upper(), "rank": "Member"}
    return 200, synthetic_citizen_html(h, org=org, org_visibility="R" if h.startswith("Redacted") else "V")


class _Disabled(Exception):
    """Lookups were switched off between scheduling and sending a request."""


class OrgLookup:
    """Cached, rate-limited citizen lookups. `enabled` gates every network call; run() is the only caller
    of the fetcher (one request in flight, MIN_INTERVAL_S between requests, back-off when blocked)."""

    def __init__(self, cache_path: pathlib.Path | None, fetch=None, enabled: bool = False,
                 min_interval: float = MIN_INTERVAL_S, clock=time.time, sleep=asyncio.sleep):
        self.cache_path = cache_path
        self.fetch = fetch or urllib_fetch
        self.enabled = enabled
        self.min_interval = max(float(min_interval), 0.0)
        self.clock, self.sleep = clock, sleep
        self.cache: dict[str, dict] = {}
        self.errors: dict[str, float] = {}     # handle -> retry-after (memory only)
        self.in_flight: str | None = None
        self.last_request = float("-inf")
        self.blocked_until = 0.0
        self.backoff = 60.0
        self.requests = 0
        self.last_status: str | None = None    # "ok" | "not_found" | "blocked (HTTP 429)" | "error: ..."
        self.cache_error: str | None = None
        self.generation = 0                    # bumped by clear(): an in-flight lookup then drops its result
        self.fail_streak = 0                   # consecutive error results (unparseable, timeout, other HTTP)
        self._load()

    # -- cache -------------------------------------------------------------------------------
    def _load(self):
        if not self.cache_path:
            return
        try:
            c = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.cache_error = f"cache unreadable ({type(e).__name__}); starting empty"
            return
        if isinstance(c, dict) and c.get("version") == CACHE_VERSION and isinstance(c.get("entries"), dict):
            self.cache = {k: v for k, v in c["entries"].items() if isinstance(v, dict) and isinstance(v.get("t"), (int, float))}
        self._prune()

    @staticmethod
    def _ttl(e: dict) -> float:
        if e.get("status") == "not_found":
            return NEG_TTL_S
        return ERROR_RETRY_S if e.get("partial") else TTL_S

    def _prune(self):
        """Drop expired entries (handles must not outlive the documented TTL on disk) and cap the size."""
        now = self.clock()
        self.cache = {k: e for k, e in self.cache.items() if now - e["t"] < self._ttl(e)}
        if len(self.cache) > CACHE_MAX:
            keep = sorted(self.cache, key=lambda k: self.cache[k]["t"])[-CACHE_MAX:]
            self.cache = {k: self.cache[k] for k in keep}

    def _save(self):
        self._prune()
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            # 0600 is POSIX-only privacy: on Windows the mode only sets the read-only bit, so the
            # cache is protected by the user-profile ACL of the repo dir instead
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            if os.name == "posix":
                with contextlib.suppress(OSError):
                    os.fchmod(fd, 0o600)  # a leftover tmp file keeps its old mode under O_TRUNC
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": CACHE_VERSION, "note": "scgameapi RSI lookup cache: public citizen pages; "
                           "clear with DELETE /api/people/cache", "entries": self.cache}, f, separators=(",", ":"))
            tmp.replace(self.cache_path)
            self.cache_error = None
        except OSError as e:
            self.cache_error = f"cache not saved ({type(e).__name__})"

    def clear(self) -> int:
        n = len(self.cache)
        self.generation += 1
        self.cache.clear()
        self.errors.clear()
        if self.cache_path:
            with contextlib.suppress(OSError):
                self.cache_path.unlink()
            with contextlib.suppress(OSError):
                self.cache_path.with_suffix(".tmp").unlink()
        return n

    def _fresh(self, key: str, now: float) -> dict | None:
        e = self.cache.get(key)
        if not e:
            return None
        return e if now - e["t"] < self._ttl(e) else None

    def get(self, handle: str) -> dict | None:
        """Public lookup info for the roster (cached data, or pending / error state); None = not looked up."""
        if not valid_handle(handle):
            return None
        k, now = handle.casefold(), self.clock()
        e = self._fresh(k, now)
        if e:
            return {"status": e["status"], "checked_at": e["t"], **(e.get("data") or {}),
                    **({"partial": True} if e.get("partial") else {})}
        if self.in_flight == k:
            return {"status": "fetching"}
        if k in self.errors and now < self.errors[k]:
            return {"status": "error"}
        return {"status": "pending"} if self.enabled else None

    def status(self) -> dict:
        now = self.clock()
        return {"enabled": self.enabled, "cache_entries": len(self.cache), "requests": self.requests,
                "in_flight": self.in_flight is not None, "last_status": self.last_status,
                "blocked_for_s": round(self.blocked_until - now) if self.blocked_until > now else 0,
                "cache_error": self.cache_error, "min_interval_s": self.min_interval}

    # -- worker ------------------------------------------------------------------------------
    def next_handle(self, handles) -> str | None:
        now = self.clock()
        for h in list(handles):   # scan everything (PeopleState caps it): filter first, then pick
            k = h.casefold()
            if valid_handle(h) and not self._fresh(k, now) and not (k in self.errors and now < self.errors[k]):
                return h
        return None

    async def _get(self, url: str) -> tuple[int, str]:
        wait = self.last_request + self.min_interval - self.clock()
        if wait > 0:
            await self.sleep(wait)
        if not self.enabled:   # toggled off while waiting: no request leaves the bridge
            raise _Disabled
        self.last_request = self.clock()
        self.requests += 1
        return await asyncio.to_thread(self.fetch, url, TIMEOUT_S)

    def _blocked(self, code: int | None = None, why: str | None = None):
        """Global pause with doubling back-off (60 s .. 15 min). code = HTTP status, or why = failure streak."""
        self.blocked_until = self.clock() + self.backoff
        self.backoff = min(self.backoff * 2, 900.0)
        reason = f"HTTP {code}" if code is not None else (why or "errors")
        self.last_status = f"blocked ({reason})" if code is not None else f"paused ({reason})"
        print(f"[people] RSI lookup paused {round(self.blocked_until - self.clock())} s: {reason}",
              file=sys.stderr, flush=True)

    def _error(self, k: str, status: str) -> str:
        """Per-handle retry later + global circuit breaker after ERROR_TRIP consecutive failures."""
        self.errors[k] = self.clock() + ERROR_RETRY_S
        self.last_status = status
        self.fail_streak += 1
        if self.fail_streak >= ERROR_TRIP:
            self.fail_streak = 0
            self._blocked(why=f"{ERROR_TRIP} failed lookups in a row; last {status}")
        return "error"

    def _success(self):
        self.fail_streak = 0
        self.backoff = 60.0

    async def lookup_one(self, handle: str) -> str:
        """Fetch + parse + cache one handle (two pages: profile, organizations tab). -> status string."""
        k = handle.casefold()
        gen = self.generation
        self.in_flight = k
        try:
            try:
                code, page = await self._get(citizen_url(handle))
            except _Disabled:
                return "disabled"
            except (OSError, ValueError, http.client.HTTPException) as e:   # URLError, timeout, bad response
                return self._error(k, f"error: {type(e).__name__}")
            if gen != self.generation:
                return "cleared"
            if code == 404:
                self.cache[k] = {"t": self.clock(), "status": "not_found", "data": {"valid": False}}
                self.last_status = "not_found"
                self._success()
                self._save()
                return "not_found"
            if code in (403, 429, 503):
                self._blocked(code)
                return "blocked"
            if code != 200:
                return self._error(k, f"error: HTTP {code}")
            data = await asyncio.to_thread(parse_citizen, page)
            if data is None:
                return self._error(k, "error: page not recognised (layout changed?)")
            if (data.get("handle") or "").casefold() != k:
                return self._error(k, "error: page is for a different handle")
            aff = None
            if self.enabled:
                with contextlib.suppress(OSError, ValueError, http.client.HTTPException, _Disabled):
                    c2, p2 = await self._get(citizen_url(handle) + "/organizations")
                    if c2 == 200:
                        aff = await asyncio.to_thread(parse_affiliations, p2)
                    elif c2 in (403, 429, 503):
                        self._blocked(c2)
            if gen != self.generation:   # cache cleared while we were waiting: keep it cleared
                return "cleared"
            data.update(valid=True, affiliations=aff)
            entry = {"t": self.clock(), "status": "ok", "data": data}
            if aff is None:   # second page skipped / failed: keep for ERROR_RETRY_S only, then retry
                entry["partial"] = True
            self.cache[k] = entry
            self.last_status = "ok" if aff is not None else "ok (organizations tab not read)"
            self._success()
            self._save()
            return "ok"
        finally:
            self.in_flight = None

    async def run(self, get_handles, poll_s: float = 0.5):
        """Loop: while enabled and not blocked, look up the next uncached handle from get_handles()."""
        while True:
            try:
                if self.enabled and self.clock() >= self.blocked_until:
                    h = self.next_handle(get_handles() or [])
                    if h:
                        await self.lookup_one(h)
                        continue
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - never log the handle
                self.last_status = f"error: {type(e).__name__}"
                print(f"[people] lookup failed: {type(e).__name__}", file=sys.stderr, flush=True)
            await self.sleep(poll_s)
