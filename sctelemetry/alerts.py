"""Alert engine: telemetry frames + bridge events -> typed alerts (toast / sound / speech).

Pure and clock-injected (every entry point takes `now`), so tests and recordings drive it without a
server. Layers:
  rules     load/merge/validate (data/alerts.default.json + optional user patch file)
  AlertEngine     per-rule state machines (threshold / transition / edge / composite / event)
  AudioScheduler  one spoken alert at a time, >= speech_gap_s apart, priority queue, critical preempts
  AudioRouter     who plays: the audio-enabled dashboard tab (browser) or the bridge (local), never both
  sinks     LocalAudioSink (winsound | afplay | pw-play/paplay/aplay/ffplay + piper/espeak-ng/espeak/spd-say/
            Windows SAPI), DryRunSink (records only)
  AlertService    glue used by server.py: recent list, SSE feed, mute, mode, test, rules save
See docs/telemetry/alerts.md.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import copy
import importlib.util
import json
import math
import os
import pathlib
import re
import shutil
import string
import sys
import tempfile
import time

from . import envcompat

DATA = pathlib.Path(__file__).parent / "data"
DEFAULT_RULES = DATA / "alerts.default.json"

SEVERITIES = {"info": 0, "warn": 1, "critical": 2}
SCOPES = ("any", "vehicle", "pilot", "on_foot")
KINDS = ("threshold", "transition", "edge", "composite", "event")
OPS = ("<", "<=", ">", ">=", "==", "!=", "in")
AUDIO_MODES = ("auto", "bridge", "browser", "off")
RULE_ID_RE = re.compile(r"^[a-z0-9_]{1,48}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CLIENT_TTL_S = 10.0
TEXT_MAX = 160
RULES_MAX = 100
SETTING_DEFAULTS = {"speech_gap_s": 2.0, "queue_max": 6, "queue_ttl_s": 10.0, "settle_s": 1.5, "stale_s": 3.0,
                    "event_max_age_s": 30.0, "max_eval_hz": 20, "sounds": {}, "tts": {}}
# numeric settings: (min, max) inclusive; validated on the merged config so a typo in a user file is
# rejected at load (defaults used) instead of crashing the engine on the first frame
SETTING_RANGES = {"speech_gap_s": (0, 60), "queue_max": (1, 50), "queue_ttl_s": (0, 600), "settle_s": (0, 60),
                  "stale_s": (0.1, 600), "event_max_age_s": (0, 86400), "max_eval_hz": (0.1, 1e6)}
DETAIL_RE_MAX = 200
# message / speech placeholders: known fields with simple format specs only (no attribute / index
# access, no huge widths): {value}, {pct}, {value:.0f}, {count:>3}
TEMPLATE_FIELDS = ("value", "pct", "pct_words", "detail", "count", "class", "kind", "text")
_PLACEHOLDER_RE = re.compile(r"(%s)(:[<>^]?\d{0,2}(\.\d{1,2})?[dfs%%]?)?" % "|".join(TEMPLATE_FIELDS))
RULE_FIELDS = {"id", "description", "kind", "enabled", "severity", "scope", "cooldown_s", "debounce_s", "message",
               "speech", "sound", "verified", "when", "supersedes", "on_entry",
               "path", "op", "value", "rearm",           # threshold
               "to", "from",                             # transition / edge
               "all", "any", "not",                      # composite
               "event", "detail_re", "match"}            # event


# ---- values + conditions --------------------------------------------------------------------------

def clean(v):
    """Frame value -> comparable: numbers with FLT_MAX sentinel / NaN -> None; other types as-is."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return None if (v != v or abs(v) > 1e30) else float(v)
    return v


def resolve(frame: dict | None, derived: dict, path: str):
    if path.startswith("$"):
        return clean(derived.get(path[1:]))
    cur = frame
    for k in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return clean(cur)


def enum_eq(a, b) -> bool:
    """`b` matches `a` exactly, or as the suffix of a proto enum name ("DEPLOYED" ~ "..._STATE_DEPLOYED")."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if a == b:
        return True
    return isinstance(a, str) and isinstance(b, str) and a.endswith("_" + b)


def cond(c: dict, frame, derived):
    """-> True / False / None (data missing). {path, op, value} | {all: [...]} | {any: [...]} | {not: c}."""
    if "all" in c:
        rs = [cond(x, frame, derived) for x in c["all"]]
        return False if False in rs else (None if None in rs else True)
    if "any" in c:
        rs = [cond(x, frame, derived) for x in c["any"]]
        return True if True in rs else (None if None in rs else False)
    if "not" in c:
        r = cond(c["not"], frame, derived)
        return None if r is None else not r
    return compare(resolve(frame, derived, c["path"]), c.get("op", "=="), c.get("value"))


def compare(v, op, ref):
    if v is None:
        return None
    if op == "==":
        return enum_eq(v, ref)
    if op == "!=":
        return not enum_eq(v, ref)
    if op == "in":
        return any(enum_eq(v, r) for r in ref)
    if isinstance(v, bool) or not isinstance(v, float):
        return None
    return {"<": v < ref, "<=": v <= ref, ">": v > ref, ">=": v >= ref}[op]


# ---- rules file -----------------------------------------------------------------------------------

def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _validate_cond(c, where, errs, depth=0):
    if depth > 4 or not isinstance(c, dict):
        errs.append(f"{where}: condition must be an object (max nesting 4)")
        return
    for key in ("all", "any"):
        if key in c:
            if not isinstance(c[key], list) or not 1 <= len(c[key]) <= 10:
                errs.append(f"{where}.{key}: 1..10 conditions")
                return
            for i, x in enumerate(c[key]):
                _validate_cond(x, f"{where}.{key}[{i}]", errs, depth + 1)
            return
    if "not" in c:
        _validate_cond(c["not"], f"{where}.not", errs, depth + 1)
        return
    p, op = c.get("path"), c.get("op", "==")
    if not isinstance(p, str) or not re.fullmatch(r"\$?[a-z_][a-z0-9_]*(\.[a-z0-9_]+)*", p):
        errs.append(f"{where}.path: dotted lowercase path or $derived")
    if op not in OPS:
        errs.append(f"{where}.op: one of {', '.join(OPS)}")
    v = c.get("value")
    if op in ("<", "<=", ">", ">=") and not _num(v):
        errs.append(f"{where}.value: number for {op}")
    if op == "in" and (not isinstance(v, list) or not v):
        errs.append(f"{where}.value: non-empty list for in")


def _text(x, where, errs, allow_null=True, template=False):
    if x is None and allow_null:
        return
    if not isinstance(x, str) or not x.strip() or len(x) > TEXT_MAX:
        errs.append(f"{where}: string of 1..{TEXT_MAX} chars" + (" or null" if allow_null else ""))
        return
    if template:
        rest = re.sub(r"\{([^{}]*)\}", lambda m: "" if _PLACEHOLDER_RE.fullmatch(m.group(1)) else "\0", x)
        if "\0" in rest or "{" in rest or "}" in rest:
            errs.append(f"{where}: placeholders must be {{{'|'.join(TEMPLATE_FIELDS)}}} with an optional "
                        "simple format spec such as :.0f")


def validate_rule(r, errs: list[str]):
    if not isinstance(r, dict):
        errs.append("rule must be an object")
        return
    rid = r.get("id")
    w = f"rule {rid!r}"
    if not isinstance(rid, str) or not RULE_ID_RE.fullmatch(rid):
        errs.append(f"{w}: id must match ^[a-z0-9_]{{1,48}}$")
    unknown = set(r) - RULE_FIELDS - {"$comment"}
    if unknown:
        errs.append(f"{w}: unknown field(s) {sorted(unknown)}")
    kind = r.get("kind")
    if kind not in KINDS:
        errs.append(f"{w}: kind must be one of {', '.join(KINDS)}")
    if r.get("severity") not in SEVERITIES:
        errs.append(f"{w}: severity must be info|warn|critical")
    if r.get("scope", "any") not in SCOPES:
        errs.append(f"{w}: scope must be one of {', '.join(SCOPES)}")
    for k in ("cooldown_s", "debounce_s"):
        if k in r and (not _num(r[k]) or not 0 <= r[k] <= 3600):
            errs.append(f"{w}: {k} must be 0..3600")
    for k in ("enabled", "verified", "on_entry"):
        if k in r and not isinstance(r[k], bool):
            errs.append(f"{w}: {k} must be a boolean")
    _text(r.get("message"), f"{w}.message", errs, allow_null=False, template=True)
    _text(r.get("speech"), f"{w}.speech", errs, template=True)
    _text(r.get("description"), f"{w}.description", errs)
    s = r.get("sound")
    if s is not None and (not isinstance(s, str) or not RULE_ID_RE.fullmatch(s)):
        errs.append(f"{w}.sound: sound id (^[a-z0-9_]+$) or null")
    sup = r.get("supersedes", [])
    if not isinstance(sup, list) or not all(isinstance(x, str) for x in sup):
        errs.append(f"{w}.supersedes: list of rule ids")
    if "when" in r:
        _validate_cond(r["when"], f"{w}.when", errs)
    if kind == "threshold":
        _validate_cond({k: r.get(k) for k in ("path", "op", "value")}, w, errs)
        if r.get("op") not in ("<", "<=", ">", ">="):
            errs.append(f"{w}: threshold op must be <, <=, > or >=")
        if "rearm" in r and r["rearm"] is not None:
            if not _num(r["rearm"]):
                errs.append(f"{w}.rearm: number")
            elif _num(r.get("value")) and ((r.get("op") in ("<", "<=") and r["rearm"] < r["value"])
                                           or (r.get("op") in (">", ">=") and r["rearm"] > r["value"])):
                errs.append(f"{w}.rearm: must sit on the safe side of value (hysteresis)")
    elif kind == "transition":
        _validate_cond({"path": r.get("path"), "op": "==", "value": 0}, w, errs)
        for k in ("to", "from"):
            if k in r and (not isinstance(r[k], list) or not r[k]
                           or not all(isinstance(x, (str, bool, int, float)) for x in r[k])):
                errs.append(f"{w}.{k}: non-empty list of values")
        if "to" not in r:
            errs.append(f"{w}.to: required")
    elif kind == "edge":
        _validate_cond({"path": r.get("path"), "op": "==", "value": 0}, w, errs)
        if not isinstance(r.get("to"), bool):
            errs.append(f"{w}.to: true or false")
    elif kind == "composite":
        if not any(k in r for k in ("all", "any", "not")):
            errs.append(f"{w}: composite needs all / any / not")
        else:
            _validate_cond({k: r[k] for k in ("all", "any", "not") if k in r}, w, errs)
    elif kind == "event":
        ev = r.get("event")
        evs = ev if isinstance(ev, list) else [ev]
        if not evs or not all(isinstance(x, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", x) for x in evs):
            errs.append(f"{w}.event: event type or list of types (UPPER_CASE)")
        if "detail_re" in r:
            if not isinstance(r["detail_re"], str) or len(r["detail_re"]) > DETAIL_RE_MAX:
                errs.append(f"{w}.detail_re: regex string of at most {DETAIL_RE_MAX} chars")
            else:
                try:
                    re.compile(r["detail_re"])
                except re.error as e:
                    errs.append(f"{w}.detail_re: {e}")
        if "match" in r and not isinstance(r["match"], dict):
            errs.append(f"{w}.match: object of field -> value")


def validate_rules(rules) -> list[str]:
    errs: list[str] = []
    if not isinstance(rules, list) or not 0 < len(rules) <= RULES_MAX:
        return [f"rules must be a list of 1..{RULES_MAX} rules"]
    seen = set()
    for r in rules:
        validate_rule(r, errs)
        rid = r.get("id") if isinstance(r, dict) else None
        if rid in seen:
            errs.append(f"rule {rid!r}: duplicate id")
        seen.add(rid)
    return errs


def validate_settings(s: dict) -> list[str]:
    errs = []
    for k, (lo, hi) in SETTING_RANGES.items():
        v = s.get(k)
        if not _num(v) or not lo <= v <= hi:
            errs.append(f"settings.{k}: number {lo:g}..{hi:g}")
    if "speech" in s and not isinstance(s["speech"], bool):
        errs.append("settings.speech: true/false")
    snd = s.get("sounds")
    if not isinstance(snd, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and all(isinstance(p, str) for p in v) for k, v in snd.items()):
        errs.append("settings.sounds: object of sound id -> list of file paths")
    tts = s.get("tts")
    if not isinstance(tts, dict):
        errs.append("settings.tts: object")
    else:
        rate = tts.get("rate_wpm")
        if rate is not None and (not _num(rate) or not 80 <= rate <= 500):
            errs.append("settings.tts.rate_wpm: number 80..500 or null")
        pv = tts.get("piper_voice")
        if pv is not None and not isinstance(pv, str):
            errs.append("settings.tts.piper_voice: path string or null")
    return errs


def load_defaults(path: pathlib.Path = DEFAULT_RULES) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def merge(defaults: dict, user: dict | None) -> dict:
    """Default config + user patch -> {settings, rules}. User rules patch defaults by id (field-wise;
    a field set to null is removed from the default rule) or add new ids; `removed` lists default ids
    to drop; user settings patch default settings (sounds / tts merged one level deep)."""
    settings = {**SETTING_DEFAULTS, **copy.deepcopy(defaults.get("settings") or {})}
    rules = [copy.deepcopy(r) for r in defaults.get("rules") or []]
    if user is not None:
        if not isinstance(user, dict):
            raise ValueError("user file must be a JSON object")
        us = user.get("settings") or {}
        if not isinstance(us, dict):
            raise ValueError("settings must be an object")
        for k, v in us.items():
            if k in ("sounds", "tts"):
                if not isinstance(v, dict):
                    raise ValueError(f"settings.{k} must be an object")
                settings[k] = {**settings.get(k, {}), **copy.deepcopy(v)}
            elif k in SETTING_DEFAULTS:
                settings[k] = v
        by_id = {r["id"]: r for r in rules}
        urules = user.get("rules") or []
        if not isinstance(urules, list):
            raise ValueError("rules must be a list")
        removed = user.get("removed") or []
        if not isinstance(removed, list) or not all(isinstance(x, str) for x in removed):
            raise ValueError("removed must be a list of rule ids")
        for ur in urules:
            if not isinstance(ur, dict) or not isinstance(ur.get("id"), str):
                raise ValueError("every user rule needs an id")
            if ur["id"] in by_id:
                tgt = by_id[ur["id"]]
                for k, v in copy.deepcopy(ur).items():
                    if v is None:
                        tgt.pop(k, None)  # explicit null = unset the default's field
                    else:
                        tgt[k] = v
            else:
                r = {k: v for k, v in copy.deepcopy(ur).items() if v is not None}
                rules.append(r)
                by_id[r["id"]] = r
        drop = set(removed) - {ur["id"] for ur in urules}
        rules = [r for r in rules if r["id"] not in drop]
    errs = validate_settings(settings)
    if errs:
        raise ValueError("; ".join(errs[:10]))
    for r in rules:
        r.setdefault("enabled", True)
        r.setdefault("scope", "any")
        r.setdefault("cooldown_s", 0)
        r.setdefault("debounce_s", 0)
        r.setdefault("verified", False)
    errs = validate_rules(rules)
    if errs:
        raise ValueError("; ".join(errs[:10]))
    return {"settings": settings, "rules": rules}


def diff_rules(defaults: dict, rules: list[dict]) -> list[dict]:
    """Rules -> minimal user patch: changed fields of default ids + whole new rules."""
    base = {r["id"]: r for r in merge(defaults, None)["rules"]}
    out = []
    for r in rules:
        d = base.get(r["id"])
        if d is None:
            out.append(copy.deepcopy(r))
            continue
        patch = {k: v for k, v in r.items() if d.get(k) != v}
        patch.update({k: None for k in d if k not in r and d[k] is not None})  # field removed from a default
        if patch:
            out.append({"id": r["id"], **patch})
    return out


def load_config(user_path: pathlib.Path | None, defaults_path: pathlib.Path = DEFAULT_RULES) -> tuple[dict, str | None]:
    """-> (config, error). A broken user file never disables alerts: defaults are used and the error returned."""
    defaults = load_defaults(defaults_path)
    if user_path is None or not user_path.is_file():
        return merge(defaults, None), None
    try:
        return merge(defaults, json.loads(user_path.read_text(encoding="utf-8"))), None
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as e:
        return merge(defaults, None), f"{user_path}: {e}"


def read_user_settings(path: pathlib.Path | None) -> dict | None:
    """The `settings` patch of an existing user file (kept when PUT rewrites its rules), else None."""
    if path is None or not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    s = body.get("settings") if isinstance(body, dict) else None
    return s if isinstance(s, dict) and s else None


def save_user(path: pathlib.Path, defaults: dict, rules: list[dict], settings_patch: dict | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "rules": diff_rules(defaults, rules)}
    keep = {r["id"] for r in rules}
    removed = [r["id"] for r in defaults.get("rules") or [] if r["id"] not in keep]
    if removed:
        body["removed"] = removed
    if settings_patch:
        body["settings"] = settings_patch
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".alerts-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


# ---- text -----------------------------------------------------------------------------------------

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def words(n: int) -> str:
    """0..100 -> English words (speakable without a TTS number normaliser)."""
    n = max(0, min(100, int(n)))
    if n == 100:
        return "one hundred"
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + ("" if o == 0 else " " + _ONES[o])


class _Safe(dict):
    def __missing__(self, key):
        return "?"


def render(template: str | None, fields: dict) -> str | None:
    if template is None:
        return None
    try:
        return string.Formatter().vformat(template, (), _Safe(fields))[:TEXT_MAX]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return template[:TEXT_MAX]


# ---- engine ---------------------------------------------------------------------------------------

class _RuleState:
    __slots__ = ("armed", "pending", "last_fire", "need_base", "prev_out", "in_scope")

    def __init__(self):
        self.armed = False
        self.pending = None      # time the firing condition started holding (debounce)
        self.last_fire = -1e18
        self.need_base = True
        self.prev_out = None     # transition: last value outside the `to` set
        self.in_scope = None     # None until the first evaluated frame (startup is never a scope "entry")


def _alert(rule: dict, now: float, value=None, extra: dict | None = None, test=False) -> dict:
    f = {"value": value if value is not None else "?", "detail": "?", "count": "?", "class": "?", "kind": "?",
         "text": "?"}
    if isinstance(value, float):
        f["pct"] = round(max(0.0, min(1.0, value)) * 100)
        f["pct_words"] = words(f["pct"])
    for k, v in (extra or {}).items():
        if isinstance(v, (str, int, float, bool)):
            f[k] = v
    text = render(rule["message"], f)
    return {"rule": rule["id"], "severity": rule["severity"], "text": ("TEST: " + text) if test else text,
            "speech": render(rule.get("speech"), f), "sound": rule.get("sound"), "t": round(now, 3),
            "value": round(value, 4) if isinstance(value, float) else None, "test": test,
            "supersedes": list(rule.get("supersedes") or [])}


TEST_FIELDS = {"detail": "example", "count": 3, "class": "example ship", "kind": "example", "text": "example"}


class AlertEngine:
    """Per-rule state machines. evaluate_frame / evaluate_event return new alerts (dicts)."""

    def __init__(self, config: dict):
        self.set_config(config)

    def set_config(self, config: dict):
        self.settings = config["settings"]
        self.rules = [r for r in config["rules"]]
        self.by_id = {r["id"]: r for r in self.rules}
        old = getattr(self, "state", {})
        self.state = {r["id"]: old.get(r["id"]) or _RuleState() for r in self.rules}
        for s in self.state.values():
            s.need_base = True  # changed rules re-baseline instead of firing on the current value
        self._re = {r["id"]: re.compile(r["detail_re"]) for r in self.rules if r.get("detail_re")}
        if not hasattr(self, "last_t"):
            self.last_t = None
            self.last_eval = -1e18
            self.vehicle_key = None
            self.settle_until = -1e18
            self.quantum_seen = False
            self.face_shares = None

    def rebaseline(self, now: float):
        self.settle_until = now + float(self.settings.get("settle_s", 1.5))
        for s in self.state.values():
            s.need_base = True
            s.pending = None

    # derived values a rule can address as "$name"
    def derive(self, frame: dict) -> dict:
        d = {}
        vit = (frame.get("actor") or {}).get("vitals") or {}
        ox = vit.get("oxygen")
        if isinstance(ox, dict):
            cur, mx = clean(ox.get("current")), clean(ox.get("max"))
            if isinstance(cur, float):
                d["oxygen"] = cur / mx if isinstance(mx, float) and mx > 0 else cur
        vs = (frame.get("vehicle") or {}).get("state")
        if isinstance(vs, dict):
            q = clean(((vs.get("flight") or {}).get("instruments") or {}).get("quantum_fuel"))
            if isinstance(q, float) and q > 0:
                self.quantum_seen = True
            sh = vs.get("shields") or {}
            lvl, faces = clean(sh.get("level")), sh.get("faces")
            if isinstance(faces, list) and faces and isinstance(lvl, float):
                fv = [clean(x) for x in faces]
                if all(isinstance(x, float) for x in fv):
                    if lvl >= 0.99:  # full charge: each face shows its tuning share (data/shield_faces.json)
                        self.face_shares = fv
                    shares = self.face_shares if self.face_shares and len(self.face_shares) == len(fv) else None
                    d["shield_faces_down"] = float(sum(
                        1 for i, x in enumerate(fv)
                        if x <= 0.01 and lvl > 0.05 and not (shares and shares[i] <= 0.05)))
        d["quantum_seen"] = self.quantum_seen
        return d

    def _scope_ok(self, scope: str, frame: dict) -> bool:
        vs = (frame.get("vehicle") or {}).get("state")
        has_v = isinstance(vs, dict) and bool(vs)
        if scope == "vehicle":
            return has_v
        if scope == "pilot":
            return has_v and enum_eq(((frame.get("actor") or {}).get("state") or {}).get("crew_role"), "PILOT")
        if scope == "on_foot":
            return not has_v
        return True

    def _active(self, r: dict, st: _RuleState, frame, derived):
        """-> (active True/False/None, rearm_ok bool, value)."""
        k = r["kind"]
        if k == "threshold":
            v = resolve(frame, derived, r["path"])
            a = compare(v, r["op"], r["value"])
            if a is None:
                return None, False, None
            rearm = r.get("rearm")
            if rearm is None:
                return a, not a, v
            ok = v >= rearm if r["op"] in ("<", "<=") else v <= rearm
            return a, ok and not a, v
        if k == "transition":
            v = resolve(frame, derived, r["path"])
            if v is None:
                return None, False, None
            inside = any(enum_eq(v, x) for x in r["to"])
            if not inside:
                st.prev_out = v
                return False, True, None
            frm = r.get("from")
            a = frm is None or (st.prev_out is not None and any(enum_eq(st.prev_out, x) for x in frm))
            return a, not a, None
        if k == "edge":
            v = resolve(frame, derived, r["path"])
            if not isinstance(v, bool):
                return None, False, None
            a = v is r["to"]
            return a, not a, None
        # composite
        a = cond({x: r[x] for x in ("all", "any", "not") if x in r}, frame, derived)
        return a, a is False, None

    def evaluate_frame(self, frame: dict | None, now: float, stale: bool = False) -> list[dict]:
        if not isinstance(frame, dict) or stale:
            return []
        if now - self.last_eval < 1.0 / max(1, float(self.settings.get("max_eval_hz", 20))) - 1e-6:
            return []
        self.last_eval = now
        vs = (frame.get("vehicle") or {}).get("state")
        vkey = None
        if isinstance(vs, dict) and vs:
            vkey = vs.get("name") or vs.get("class_name") or "?"
        if self.last_t is None or now - self.last_t > float(self.settings.get("stale_s", 3.0)):
            self.rebaseline(now)  # first frame, or resuming after a gap: baseline, never "changes"
        elif vkey is not None and vkey != self.vehicle_key:
            self.rebaseline(now)  # vehicle appeared / changed: the game fills it in over ~0.5 s
        if vkey is not None and vkey != self.vehicle_key:  # per-vehicle derived state starts over
            self.vehicle_key, self.quantum_seen, self.face_shares = vkey, False, None
        self.last_t = now
        derived = self.derive(frame)
        settling = now < self.settle_until
        out = []
        for r in self.rules:
            if r["kind"] == "event":
                continue
            st = self.state[r["id"]]
            if not r.get("enabled", True):
                st.need_base, st.pending = True, None
                continue
            in_scope = self._scope_ok(r.get("scope", "any"), frame)
            entered = in_scope and st.in_scope is False
            st.in_scope = in_scope
            if not in_scope:
                st.need_base, st.pending = True, None
                continue
            active, rearm_ok, value = self._active(r, st, frame, derived)
            if active is None:
                st.pending = None
                continue
            guard = cond(r["when"], frame, derived) if "when" in r else True
            if guard is not True:
                st.armed, st.pending = not active, None  # gated off: current state is the baseline
                continue
            if st.need_base or settling:
                if entered and r.get("on_entry") and not settling:  # e.g. stepping out into vacuum
                    st.need_base = False
                    st.armed = True
                else:
                    st.need_base = False
                    st.armed = not active
                    st.pending = None
                    continue
            if not st.armed:
                if rearm_ok:
                    st.armed = True
                continue
            if not active:
                st.pending = None
                continue
            if st.pending is None:
                st.pending = now
            if now - st.pending < float(r.get("debounce_s", 0)) - 1e-6:
                continue
            if now - st.last_fire < float(r.get("cooldown_s", 0)) - 1e-6:
                continue  # still armed + pending: fires once the cooldown ends if the condition holds
            st.armed, st.pending, st.last_fire = False, None, now
            out.append(_alert(r, now, value))
        self._apply_supersedes(out)
        return out

    def _apply_supersedes(self, out: list[dict]):
        sup = {x for a in out for x in a["supersedes"]}
        if sup:  # a stronger alert (e.g. boost empty) silences its weaker sibling in the same frame
            out[:] = [a for a in out if a["rule"] not in sup]
            for rid in sup:
                st = self.state.get(rid)
                if st and st.armed:
                    st.armed, st.pending = False, None

    def evaluate_event(self, ev: dict, now: float) -> list[dict]:
        typ = ev.get("type") if isinstance(ev, dict) else None
        if not isinstance(typ, str) or typ == "ALERT":
            return []
        t = ev.get("t")
        if _num(t) and now - t > float(self.settings.get("event_max_age_s", 30)):
            return []  # historical (Game.log read from the start, replayed backlog)
        out = []
        for r in self.rules:
            if r["kind"] != "event" or not r.get("enabled", True):
                continue
            types = r["event"] if isinstance(r["event"], list) else [r["event"]]
            if typ not in types:
                continue
            rx = self._re.get(r["id"])
            if rx and not rx.search(str(ev.get("detail") or ev.get("text") or "")):
                continue
            if any(ev.get(k) != v for k, v in (r.get("match") or {}).items()):
                continue
            st = self.state[r["id"]]
            if now - st.last_fire < float(r.get("cooldown_s", 0)):
                continue
            st.last_fire = now
            out.append(_alert(r, now, extra=ev))
        return out

    def test_alert(self, rule_id: str, now: float) -> dict | None:
        r = self.by_id.get(rule_id)
        if r is None:
            return None
        v = r.get("value") if r["kind"] == "threshold" and _num(r.get("value")) else None
        extra = TEST_FIELDS if r["kind"] == "event" else None
        return _alert(r, now, float(v) if v is not None else None, extra=extra, test=True)


# ---- audio scheduling + routing -------------------------------------------------------------------

def est_duration(alert: dict) -> float:
    """Rough playback length (sound + speech) for sinks that cannot report busy (browser, dry run)."""
    return 0.6 + 0.075 * len(alert.get("speech") or "")


class AudioScheduler:
    """One spoken alert at a time, starts >= gap_s apart. Highest severity first, then oldest. A
    critical alert preempts a lower one that is playing and skips the gap."""

    def __init__(self, gap_s=2.0, queue_max=6, ttl_s=10.0):
        self.gap_s, self.queue_max, self.ttl_s = gap_s, queue_max, ttl_s
        self.queue: list[dict] = []
        self.last_start = -1e18
        self.playing: dict | None = None
        self.busy_until = -1e18

    def submit(self, alert: dict):
        if not alert.get("sound") and not alert.get("speech"):
            return
        sup = set(alert.get("supersedes") or [])
        self.queue = [a for a in self.queue if a["rule"] not in sup and a["rule"] != alert["rule"]]
        self.queue.append(alert)
        while len(self.queue) > self.queue_max:  # drop the least important, oldest first
            self.queue.remove(min(self.queue, key=lambda a: (SEVERITIES[a["severity"]], a["t"])))

    def clear(self):
        self.queue.clear()

    def busy(self, now: float) -> int | None:
        return SEVERITIES[self.playing["severity"]] if self.playing and now < self.busy_until else None

    def next(self, now: float, busy: int | None = None) -> tuple[dict, bool] | None:
        """-> (alert, preempt) to start now, or None. `busy` overrides the estimate (real sink state)."""
        self.queue = [a for a in self.queue if now - a["t"] <= self.ttl_s or a.get("test")]
        if not self.queue:
            return None
        top = max(self.queue, key=lambda a: (SEVERITIES[a["severity"]], -a["t"]))
        sev = SEVERITIES[top["severity"]]
        cur = self.busy(now) if busy is None else busy
        last_sev = SEVERITIES[self.playing["severity"]] if self.playing else -1
        preempt = False
        if cur is not None:
            if sev <= cur or sev < SEVERITIES["critical"]:
                return None
            preempt = True
        elif now - self.last_start < self.gap_s and not (sev == SEVERITIES["critical"] and last_sev < sev):
            return None
        self.queue.remove(top)
        self.last_start, self.playing = now, top
        self.busy_until = now + est_duration(top)
        return top, preempt


class AudioRouter:
    """auto: the newest audio-enabled dashboard tab that is still alive plays, else the bridge.
    bridge / browser: only that side. off: nobody. A tab is alive while it is audio-enabled AND its
    /events stream is open: a play job only reaches a tab through that stream, so a tab without one
    (heartbeat only, or reconnecting) never takes the audio. Heartbeats carry the enabled state; a
    client that stops heartbeating is forgotten after 10 min."""

    def __init__(self, mode="auto"):
        self.mode = mode if mode in AUDIO_MODES else "auto"
        self.clients: dict[str, dict] = {}
        self.sse_open: dict[str, set[int]] = {}  # client id -> serials of its open /events streams
        self._stream = 0

    def heartbeat(self, cid: str, enabled: bool, now: float, speech: bool | None = None):
        c = self.clients.get(cid)
        if c is None:
            if not enabled:
                return
            c = self.clients[cid] = {"enabled_at": now, "speech": None}
        if enabled and not c.get("enabled"):
            c["enabled_at"] = now
        c.update(enabled=bool(enabled), seen=now)
        if speech is not None:
            c["speech"] = bool(speech)
        self._gc(now)

    def sse_open_stream(self, cid: str | None, now: float) -> int | None:
        """An /events stream of tab `cid` opened -> its serial (pass it to sse_close / is_newest)."""
        if not cid:
            return None
        self._stream += 1
        self.sse_open.setdefault(cid, set()).add(self._stream)
        c = self.clients.get(cid)
        if c is not None:
            c["seen"] = now
        return self._stream

    def sse_close(self, cid: str | None, serial: int | None, now: float):
        """Stream closed. Its last stream gone = the tab cannot receive play jobs: the bridge takes over
        at once (no grace: a job routed to a closed stream would be heard nowhere)."""
        if not cid or serial is None:
            return
        open_ = self.sse_open.get(cid)
        if open_ is not None:
            open_.discard(serial)
            if not open_:
                del self.sse_open[cid]
        c = self.clients.get(cid)
        if c is not None:
            c["seen"] = now

    def is_newest(self, cid: str | None, serial: int | None) -> bool:
        """Only the newest open stream of a client id plays (two connections with one id, e.g. a
        duplicated tab that cloned its id, never both play)."""
        open_ = self.sse_open.get(cid) if cid else None
        return serial is None or (bool(open_) and serial == max(open_))

    def _alive(self, cid: str, c: dict, now: float) -> bool:
        return bool(c.get("enabled")) and bool(self.sse_open.get(cid))

    def _gc(self, now: float):
        for k in [k for k, c in self.clients.items() if now - c.get("seen", -1e18) > 600 and k not in self.sse_open]:
            del self.clients[k]

    def browser_target(self, now: float) -> str | None:
        alive = [(c["enabled_at"], k) for k, c in self.clients.items() if self._alive(k, c, now)]
        return max(alive)[1] if alive else None

    def route(self, now: float) -> tuple[str, str | None]:
        """-> ("browser", client_id) | ("bridge", None) | ("none", reason)."""
        if self.mode == "off":
            return "none", "audio off"
        tgt = self.browser_target(now) if self.mode in ("auto", "browser") else None
        if tgt:
            return "browser", tgt
        if self.mode == "browser":
            return "none", "no audio-enabled dashboard"
        return "bridge", None

    def present(self, now: float) -> int:
        return sum(1 for k, c in self.clients.items() if self._alive(k, c, now))


# ---- local playback -------------------------------------------------------------------------------

DATA_DIR = pathlib.Path(__file__).parent / "data"   # relative sound paths resolve here (data/sounds/*.wav)
WINSOUND = "winsound"   # in-process player (Windows stdlib), not an executable
WAV_ONLY = frozenset({"aplay", "afplay", WINSOUND})  # players that cannot decode .oga/.ogg


def which(name: str, path: str | None = None) -> str | None:
    return shutil.which(name, path=path if path is not None else os.environ.get("PATH"))


def detect_player(path: str | None = None, platform: str | None = None) -> list[str] | None:
    """Sound player argv (file appended), first available: Windows winsound (stdlib, WAV);
    macOS afplay; Linux pw-play > paplay > aplay (WAV) > ffplay. None = no player."""
    platform = platform or sys.platform
    if platform == "win32":
        return [WINSOUND]
    if platform == "darwin":
        exe = which("afplay", path)
        return [exe] if exe else None
    for name, extra in (("pw-play", []), ("paplay", []), ("aplay", ["-q"]),
                        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"])):
        exe = which(name, path)
        if exe:
            return [exe] + extra
    return None


def player_formats(player: list[str] | None) -> frozenset[str] | None:
    """File extensions the player can play (None = anything a sound file may be)."""
    if not player:
        return None
    return frozenset({".wav"}) if pathlib.Path(player[0]).stem.lower() in WAV_ONLY else None


def _sapi_rate(wpm: int) -> int:
    return max(-10, min(10, round((wpm - 175) / 25)))


def detect_tts(tts_cfg: dict | None = None, path: str | None = None, env: dict | None = None,
               find_module=importlib.util.find_spec, platform: str | None = None) -> dict | None:
    """-> {"name", "argv"(text placeholder "{text}" unless stdin_text), "stdin_text", "wav": bool} for the
    first available backend: piper (module or binary, needs a voice model file) > espeak-ng > espeak >
    spd-say (Linux) / Windows SAPI via PowerShell (text on stdin, never in argv); else None."""
    tts_cfg = tts_cfg or {}
    env = os.environ if env is None else env
    platform = platform or sys.platform
    rate = int(tts_cfg.get("rate_wpm") or 175)
    voice = envcompat.getenv("PIPER_VOICE", env=env) or tts_cfg.get("piper_voice")
    if voice and pathlib.Path(voice).is_file():
        exe = which("piper", path)
        base = [exe] if exe else ([sys.executable, "-m", "piper"] if _has_module(find_module, "piper") else None)
        if base:
            return {"name": "piper", "argv": base + ["--model", str(voice), "--output_file", "{wav}"],
                    "stdin_text": True, "wav": True}
    # "--" ends option parsing: a speech text starting with "-" is never read as a flag
    backends = [("espeak-ng", ["-s", str(rate), "--", "{text}"]), ("espeak", ["-s", str(rate), "--", "{text}"])]
    if platform != "win32":
        backends.append(("spd-say", ["-w", "-r", "0", "--", "{text}"]))
    for name, argv in backends:
        exe = which(name, path)
        if exe:
            return {"name": name, "argv": [exe] + argv, "stdin_text": False, "wav": False}
    if platform == "win32":
        ps = which("powershell", path) or which("pwsh", path)
        if ps:
            script = ("[Console]::InputEncoding=[Text.Encoding]::UTF8; Add-Type -AssemblyName System.Speech; "
                      "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      f"$s.Rate={_sapi_rate(rate)}; $s.Speak([Console]::In.ReadToEnd())")
            return {"name": "sapi", "argv": [ps, "-NoProfile", "-NonInteractive", "-Command", script],
                    "stdin_text": True, "wav": False}
    return None


def tts_hint(platform: str | None = None) -> str:
    if (platform or sys.platform) == "win32":
        return "Windows PowerShell (System.Speech) not found; install espeak-ng, or piper + a voice"
    return "install espeak-ng (or speech-dispatcher, or piper + a voice)"


def _has_module(find_module, name) -> bool:
    try:
        return find_module(name) is not None
    except (ImportError, ValueError):
        return False


def resolve_sound(sounds: dict, sound_id: str | None, severity: str, exts=None,
                  base: pathlib.Path = DATA_DIR) -> str | None:
    """First existing file for the sound id, then the severity. Relative paths resolve against the
    package data dir (the bundled `sounds/*.wav`); `exts` limits to what the player can decode."""
    for key in (sound_id, severity):
        for p in (sounds.get(key) or []) if key else []:
            if not isinstance(p, str) or not p:
                continue
            f = pathlib.Path(p).expanduser()
            if not f.is_absolute():
                f = base / f
            if exts is not None and f.suffix.lower() not in exts:
                continue
            if f.is_file():
                return str(f)
    return None


def wav_seconds(path: str) -> float:
    import wave
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        return 1.0


class DryRunSink:
    """Records what would be played; never starts a process. Used by tests and --alerts-dry-run."""
    name = "dry-run"

    def __init__(self, sounds: dict | None = None, tts: dict | None = None, player: list[str] | None = None,
                 log=None):
        self.sounds, self.tts, self.player, self.log = sounds or {}, tts, player, log
        self.played: list[dict] = []

    def busy(self):
        return None

    async def play(self, alert: dict, preempt: bool = False):
        rec = {"rule": alert["rule"], "severity": alert["severity"], "speech": alert.get("speech"),
               "sound": resolve_sound(self.sounds, alert.get("sound"), alert["severity"], player_formats(self.player))
               if alert.get("sound") else None,
               "tts": self.tts["name"] if self.tts and alert.get("speech") else None, "preempt": preempt,
               "t": alert["t"]}
        self.played.append(rec)
        if self.log:
            self.log(f"[alerts] dry-run would play {rec}")

    async def stop(self):
        pass


class LocalAudioSink:
    """Plays the sound then the speech with subprocesses on the event loop (never blocks it). A
    preempting alert kills what is playing; otherwise alerts play back to back."""
    name = "bridge"

    def __init__(self, sounds: dict, tts: dict | None, player: list[str] | None, log=None):
        if os.environ.get("PYTEST_CURRENT_TEST") and envcompat.getenv("ALLOW_AUDIO_TEST") != "1":
            raise RuntimeError("refusing real audio playback under pytest (set SCTELEMETRY_ALLOW_AUDIO_TEST=1)")
        self.sounds, self.tts, self.player, self.log = sounds, tts, player, log or (lambda m: None)
        self._task: asyncio.Task | None = None
        self._proc = None
        self._sev: int | None = None

    def busy(self) -> int | None:
        return self._sev if self._task and not self._task.done() else None

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
        if self.player == [WINSOUND]:
            _winsound_stop()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def play(self, alert: dict, preempt: bool = False):
        prev = self._task
        if preempt:
            await self.stop()
            prev = None
        self._sev = SEVERITIES[alert["severity"]]
        self._task = asyncio.create_task(self._run(alert, prev))

    async def _play_file(self, f: str):
        if self.player == [WINSOUND]:
            await self._winsound(f)
        elif self.player:
            await self._exec(self.player + [f])

    async def _run(self, alert, prev):
        if prev and not prev.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prev
        snd = (resolve_sound(self.sounds, alert.get("sound"), alert["severity"], player_formats(self.player))
               if alert.get("sound") else None)
        if snd and self.player:
            await self._play_file(snd)
        text = alert.get("speech")
        if text and self.tts:
            if self.tts.get("wav"):
                with tempfile.TemporaryDirectory(prefix="sctelemetry-tts-") as d:
                    wav = str(pathlib.Path(d) / "a.wav")
                    argv = [a.replace("{wav}", wav) for a in self.tts["argv"]]
                    if await self._exec(argv, stdin=text.encode("utf-8")) == 0 and self.player:
                        await self._play_file(wav)
            elif self.tts.get("stdin_text"):
                await self._exec(list(self.tts["argv"]), stdin=text.encode("utf-8"))  # text never in argv
            else:
                await self._exec([a.replace("{text}", text) for a in self.tts["argv"]])

    async def _winsound(self, f: str):
        """Windows: async PlaySound + sleep for the clip length, so a preempt (stop) can purge it."""
        try:
            import winsound
            winsound.PlaySound(f, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except (ImportError, RuntimeError) as e:
            self.log(f"[alerts] winsound failed: {type(e).__name__}")
            return
        try:
            await asyncio.sleep(wav_seconds(f) + 0.05)
        except asyncio.CancelledError:
            _winsound_stop()
            raise

    async def _exec(self, argv, stdin: bytes | None = None, timeout=15.0) -> int | None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(self._proc.communicate(stdin), timeout)
            return self._proc.returncode
        except (OSError, asyncio.TimeoutError) as e:
            if self._proc and self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
            self.log(f"[alerts] {argv[0]} failed: {type(e).__name__}")
            return None


def _winsound_stop():
    with contextlib.suppress(ImportError, RuntimeError):
        import winsound
        winsound.PlaySound(None, 0)


# ---- service --------------------------------------------------------------------------------------

class AlertService:
    """Engine + scheduler + router + sinks. server.py feeds on_frame / on_event and calls tick()."""

    def __init__(self, config: dict, defaults: dict | None = None, user_path: pathlib.Path | None = None,
                 mode: str = "auto", dry_run: bool = False, emit_event=None, log=None,
                 player: list[str] | None = None, tts: dict | None = None, sink=None, config_error: str | None = None):
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.defaults = defaults if defaults is not None else load_defaults()
        self.user_path = user_path
        self.config_error = config_error
        self.engine = AlertEngine(config)
        s = config["settings"]
        self.scheduler = AudioScheduler(float(s["speech_gap_s"]), int(s["queue_max"]), float(s["queue_ttl_s"]))
        self.router = AudioRouter(mode)
        self.emit_event = emit_event          # hook(alert) -> hub ALERT event
        self.recent: collections.deque = collections.deque(maxlen=50)
        self.feed: collections.deque = collections.deque(maxlen=200)  # SSE "alert" payloads
        self.feed_seq = 0
        self.muted_until = 0.0
        self.last_route: dict | None = None
        self.dry_run = dry_run
        self.player = player
        self.tts = tts
        self.sink = sink or (DryRunSink(s["sounds"], tts, player, self.log) if dry_run
                             else LocalAudioSink(s["sounds"], tts, player, self.log))
        self._seq = 0
        self.boot = f"{int(time.time() * 1000):x}{os.getpid() & 0xffff:04x}"  # per process: alert ids restart at 1
        self._warned_tts = False
        self._seen_source = None

    # -- inputs ------------------------------------------------------------------------------
    def on_frame(self, frame: dict, now: float | None = None, stale: bool = False):
        now = time.time() if now is None else now
        for a in self.engine.evaluate_frame(frame, now, stale):
            self.emit(a, now)

    def on_event(self, ev: dict, now: float | None = None):
        now = time.time() if now is None else now
        for a in self.engine.evaluate_event(ev, now):
            self.emit(a, now)

    def on_source(self, source: str, now: float | None = None):
        """New stream source / replay loop: re-baseline so a jump in values is not reported as a change."""
        if source != self._seen_source and self._seen_source is not None:
            self.engine.rebaseline(time.time() if now is None else now)
        self._seen_source = source

    def emit(self, alert: dict, now: float):
        self._seq += 1
        alert = {**alert, "id": self._seq}
        if not self.engine.settings.get("speech", False):
            alert["speech"] = None  # speech off (user default 2026-11-21): sounds + toasts only, bridge and browser
        self.recent.appendleft(alert)
        self._push_feed({"kind": "toast", "alert": self._public(alert)})
        if self.emit_event:
            self.emit_event(alert)
        if self.router.mode != "off" and (alert.get("test") or now >= self.muted_until):
            self.scheduler.submit(alert)
        return alert

    def _public(self, a: dict) -> dict:
        return {**{k: a[k] for k in ("id", "rule", "severity", "text", "speech", "sound", "t", "value", "test")},
                "boot": self.boot}

    def _push_feed(self, item: dict):
        self.feed_seq += 1
        self.feed.append({"seq": self.feed_seq, **item})

    def feed_since(self, seq: int) -> list[dict]:
        return [x for x in self.feed if x["seq"] > seq]

    @staticmethod
    def for_client(item: dict, cid: str | None, newest: bool = True) -> dict:
        """SSE payload for one connection: play jobs say whether THIS connection is the one that plays
        (the target tab's newest open stream)."""
        out = {k: v for k, v in item.items() if k != "target"}
        if item["kind"] == "play":
            out["play"] = bool(cid) and newest and item.get("target") == cid
        return out

    # -- audio -------------------------------------------------------------------------------
    async def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        route, who = self.router.route(now)
        busy = self.sink.busy() if route == "bridge" else None
        nxt = self.scheduler.next(now, busy)
        if nxt is None:
            return None
        alert, preempt = nxt
        self.last_route = {"to": route, "rule": alert["rule"], "t": round(now, 3)}
        if route == "bridge":
            if alert.get("speech") and not self.tts and not self._warned_tts:
                self._warned_tts = True
                self.log(f"[alerts] no TTS available ({tts_hint()}): sounds only")
            await self.sink.play(alert, preempt)
        elif route == "browser":
            self._push_feed({"kind": "play", "alert": self._public(alert), "target": who, "preempt": preempt})
        else:
            self.last_route["reason"] = who
        return self.last_route

    def mute(self, minutes: float, now: float | None = None):
        now = time.time() if now is None else now
        self.muted_until = now + minutes * 60 if minutes > 0 else 0.0
        if minutes > 0:
            self.scheduler.clear()

    def set_mode(self, mode: str):
        if mode not in AUDIO_MODES:
            raise ValueError(f"mode must be one of {', '.join(AUDIO_MODES)}")
        self.router.mode = mode
        if mode == "off":
            self.scheduler.clear()

    def fire_test(self, rule_id: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        a = self.engine.test_alert(rule_id, now)
        return self.emit(a, now) if a else None

    # -- rules -------------------------------------------------------------------------------
    def rules(self) -> list[dict]:
        return copy.deepcopy(self.engine.rules)

    def set_rules(self, rules: list[dict], save: bool = True):
        """Validate (merged onto defaults by id), apply, and persist the diff to the user file. The
        user file's own `settings` patch is carried over unchanged; omitted default ids are saved as
        `removed`, so they stay dropped after a restart."""
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")
        cfg = merge(self.defaults, {"rules": rules})
        keep = {r["id"] for r in rules}
        cfg["rules"] = [r for r in cfg["rules"] if r["id"] in keep]  # PUT is the full list: omitted ids drop
        if not cfg["rules"]:
            raise ValueError("rules must not be empty")
        cfg["settings"] = self.engine.settings
        if save and self.user_path is not None:
            save_user(self.user_path, self.defaults, cfg["rules"], read_user_settings(self.user_path))
        self.engine.set_config(cfg)
        self.config_error = None
        return cfg["rules"]

    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        route, who = self.router.route(now)
        return {
            "mode": self.router.mode,
            "route": {"to": route, **({"reason": who} if route == "none" else {})},
            "browser_clients": self.router.present(now),
            "bridge": {"sink": self.sink.name, "player": pathlib.Path(self.player[0]).name if self.player else None,
                       "tts": self.tts["name"] if self.tts else None},
            "muted": now < self.muted_until,
            "muted_until": round(self.muted_until, 3) if now < self.muted_until else None,
            "queue": len(self.scheduler.queue),
            "last": self.last_route,
        }

    def rules_summary(self) -> list[dict]:
        return [{"id": r["id"], "kind": r["kind"], "enabled": r.get("enabled", True), "severity": r["severity"],
                 "scope": r.get("scope", "any"), "verified": r.get("verified", False),
                 "description": r.get("description"), "message": r["message"], "speech": r.get("speech")}
                for r in self.engine.rules]
