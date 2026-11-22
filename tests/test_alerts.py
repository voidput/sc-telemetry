"""Alert engine (sctelemetry/alerts.py): rule kinds, hysteresis / cooldown / debounce, re-baseline, routing,
scheduling, TTS detection, rules files, and a replay over real recordings. No audio is ever played: the
service runs with DryRunSink and LocalAudioSink refuses to start under pytest."""
import asyncio
import copy
import json
import pathlib
import stat
import sys

import pytest

from sctelemetry import alerts as A

REC = pathlib.Path(__file__).resolve().parents[1] / ".tmp" / "recordings"
PERSEUS = REC / "20261121-150112.jsonl"
PALADIN = REC / "20261121-165507.jsonl"
INS = ("vehicle", "state", "flight", "instruments")


def rule(**kw):
    base = {"id": "r", "kind": "threshold", "severity": "warn", "scope": "any", "cooldown_s": 0, "debounce_s": 0,
            "message": "m {pct}", "speech": "s", "sound": "warn"}
    base.update(kw)
    return base


def engine(*rules, settle_s=0.0, **settings):
    cfg = A.merge({"settings": {"settle_s": settle_s, "max_eval_hz": 1000, **settings}, "rules": list(rules)}, None)
    return A.AlertEngine(cfg)


def frame(boost=1.0, name="SHIP_A_1", **vs):
    st = {"name": name, "class_name": name.rsplit("_", 1)[0], "power": {"engines": True, "shield_generators": True,
          "shield_emitters": True}, "flight": {"instruments": {"boost": {"reserves": boost}}}}
    for k, v in vs.items():
        st[k] = v
    return {"vehicle": {"state": st}, "actor": {"state": {"crew_role": "ACTOR_CREW_ROLE_PILOT"}}}


def feed(e, frames, t0=0.0, dt=0.1):
    out = []
    for i, f in enumerate(frames):
        out += [(round(t0 + i * dt, 2), a["rule"]) for a in e.evaluate_frame(f, t0 + i * dt)]
    return out


BOOST = rule(id="boost_low", path="vehicle.state.flight.instruments.boost.reserves", op="<=", value=0.25, rearm=0.35,
             scope="vehicle")


# ---- rule kinds -----------------------------------------------------------------------------------

def test_threshold_fires_once_with_hysteresis_and_rearm():
    e = engine(BOOST)
    seq = [1.0, 0.5, 0.25, 0.2, 0.0, 0.3, 0.2, 0.3, 0.36, 0.24, 0.1]
    got = feed(e, [frame(b) for b in seq])
    assert got == [(0.2, "boost_low"), (0.9, "boost_low")]  # 0.3 is not above rearm 0.35: no re-fire at 0.6


def test_first_frame_is_baseline_and_message_formatting():
    e = engine(BOOST)
    assert feed(e, [frame(0.1), frame(0.05)]) == []  # already low at start: no startup spam
    e2 = engine(BOOST)
    e2.evaluate_frame(frame(1.0), 0)
    a = e2.evaluate_frame(frame(0.249), 0.1)[0]
    assert a["text"] == "m 25" and a["speech"] == "s" and a["severity"] == "warn" and a["value"] == 0.249


def test_cooldown_defers_and_debounce_requires_hold():
    e = engine(dict(BOOST, cooldown_s=1.0, rearm=0.3))
    got = feed(e, [frame(b) for b in (1, 0.2, 0.5, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2)])
    assert got == [(0.1, "boost_low"), (1.1, "boost_low")]  # re-armed at 0.2 s, held back until cooldown ends
    d = engine(dict(BOOST, debounce_s=0.25))
    assert feed(d, [frame(b) for b in (1, 0.2, 0.2, 1, 0.2, 0.2, 0.2, 0.2)]) == [(0.7, "boost_low")]


def test_transition_annunciator_to_major_and_from_filter():
    P = "ANNUNCIATOR_STATE_"
    r = rule(id="misl", kind="transition", path="vehicle.state.annunciators.misl", to=["MAJOR_ON", "MAJOR_FLASHING"])
    e = engine(r)
    fr = [frame(annunciators={"misl": P + s}) for s in
          ("NOMINAL", "MAJOR_ON", "MAJOR_FLASHING", "MINOR_ON", "MAJOR_FLASHING", "NOMINAL")]
    assert feed(e, fr) == [(0.1, "misl"), (0.4, "misl")]  # ON -> FLASHING stays one alert
    e2 = engine(dict(r, **{"from": ["NOMINAL"]}))
    assert feed(e2, fr) == [(0.1, "misl")]  # MINOR -> MAJOR does not match from=[NOMINAL]
    e3 = engine(r)
    assert feed(e3, [frame(annunciators={"misl": P + "MAJOR_ON"})] * 3) == []  # MAJOR at baseline


def test_edge_and_composite():
    e = engine(rule(id="emit", kind="edge", path="vehicle.state.power.shield_emitters", to=False, scope="vehicle"))
    pw = lambda em: frame(power={"shield_emitters": em, "shield_generators": True, "engines": True})  # noqa: E731
    assert feed(e, [pw(True), pw(False), pw(False), pw(True), pw(False)]) == [(0.1, "emit"), (0.4, "emit")]
    gear = rule(id="gear", kind="composite", scope="vehicle", all=[
        {"path": "vehicle.state.flight.state.gear", "op": "==", "value": "DEPLOYED"},
        {"path": "vehicle.state.flight.instruments.speed", "op": ">", "value": 120},
        {"path": "vehicle.state.flight.state.is_landed", "op": "==", "value": False}])
    G = "VEHICLE_COMPONENT_DEPLOYMENT_STATE_"

    def fr(g, v, landed=False):
        f = frame()
        f["vehicle"]["state"]["flight"] = {"state": {"gear": G + g, "is_landed": landed}, "instruments": {"speed": v}}
        return f
    e = engine(gear)
    assert feed(e, [fr("RETRACTED", 200), fr("DEPLOYED", 100), fr("DEPLOYED", 130), fr("DEPLOYED", 150),
                    fr("DEPLOYED", 130, True), fr("DEPLOYED", 130)]) == [(0.2, "gear"), (0.5, "gear")]


def test_event_rules_regex_match_age_and_cooldown():
    rs = [rule(id="arm", kind="event", event="ZONE", detail_re=r"(?i)^leaving\b.*armistice", message="{detail}",
               cooldown_s=10),
          rule(id="qt", kind="event", event="QT_ARRIVED", match={"self": True}, message="arrived {class}"),
          rule(id="sess", kind="event", event=["SESSION_JOIN", "SESSION_SHARD_CHANGE"], message="{detail}",
               speech=None, sound=None)]
    e = engine(*rs)
    ev = lambda typ, t=100, **kw: {"type": typ, "t": t, **kw}  # noqa: E731
    assert [a["text"] for a in e.evaluate_event(ev("ZONE", detail="Leaving Armistice Zone - Caution"), 100)] == \
        ["Leaving Armistice Zone - Caution"]
    assert e.evaluate_event(ev("ZONE", detail="Leaving Armistice Zone"), 105) == []  # cooldown
    assert e.evaluate_event(ev("ZONE", t=120, detail="Entering Armistice Zone"), 120) == []
    assert e.evaluate_event(ev("QT_ARRIVED", **{"self": False, "class": "X"}), 100) == []
    assert e.evaluate_event(ev("QT_ARRIVED", **{"self": True, "class": "RSI_Perseus"}), 100)[0]["text"] == \
        "arrived RSI_Perseus"
    assert e.evaluate_event(ev("SESSION_JOIN", t=10, detail="old"), 100) == []  # older than 30 s: backlog
    a = e.evaluate_event(ev("SESSION_SHARD_CHANGE", detail="use1b 020"), 100)[0]
    assert a["speech"] is None and a["sound"] is None
    assert e.evaluate_event({"type": "ALERT", "t": 100}, 100) == []


# ---- baselines, scope, gating ---------------------------------------------------------------------

def test_rebaseline_on_vehicle_change_settle_and_stale_gap():
    e = engine(BOOST, settle_s=0.5)
    assert feed(e, [frame(1.0)] * 6 + [frame(0.2)]) == [(0.6, "boost_low")]
    # a different ship already low: settling window swallows it
    assert e.evaluate_frame(frame(1.0, name="SHIP_B_2"), 1.0) == []
    assert e.evaluate_frame(frame(0.1, name="SHIP_B_2"), 1.2) == []
    # stream gap (> stale_s 3 s): resume is a baseline
    e2 = engine(BOOST)
    e2.evaluate_frame(frame(1.0), 0)
    assert e2.evaluate_frame(frame(0.1), 10) == []
    assert e2.evaluate_frame(frame(0.1), 10.1) == []
    # stale frames are ignored entirely
    e3 = engine(BOOST)
    e3.evaluate_frame(frame(1.0), 0)
    assert e3.evaluate_frame(frame(0.1), 0.1, stale=True) == []
    assert e3.evaluate_frame(frame(0.1), 0.2) != []


def test_scope_vehicle_rule_is_baselined_after_exit():
    e = engine(BOOST)
    onfoot = {"actor": {"state": {"locomotion": "ACTOR_LOCOMOTION_STATE_ON_FOOT"}}}
    got = feed(e, [frame(1.0), onfoot, onfoot, frame(0.1), frame(0.1)])  # back in the seat already low
    assert got == []


def test_when_guard_gates_and_baselines():
    r = rule(id="sh", path="vehicle.state.shields.level", op="<=", value=0.25, rearm=0.4, scope="vehicle",
             when={"path": "vehicle.state.power.shield_generators", "op": "==", "value": True})

    def fr(lvl, gen):
        return frame(shields={"level": lvl, "faces": [lvl] * 4},
                     power={"shield_generators": gen, "shield_emitters": gen, "engines": True})
    e = engine(r)
    # generators off -> level decays: no alert; powered on at 0.1 -> charge-up is not "shields low"
    seq = [fr(1, True), fr(0.8, False), fr(0.1, False), fr(0.1, True), fr(0.2, True), fr(0.5, True), fr(0.2, True)]
    assert feed(e, seq) == [(0.6, "sh")]


def test_on_entry_fires_when_stepping_out_into_vacuum():
    r = rule(id="air", kind="edge", path="actor.environment.breathable_atmosphere", to=False, scope="on_foot",
             on_entry=True, debounce_s=0.2)
    e = engine(r)
    foot = lambda b: {"actor": {"environment": {"breathable_atmosphere": b}}}  # noqa: E731
    got = feed(e, [frame(), frame(), foot(False), foot(False), foot(False), foot(False), foot(True), foot(False)] +
               [foot(False)] * 3)
    assert got == [(0.4, "air"), (0.9, "air")]
    assert feed(engine(r), [foot(False)] * 5) == []  # on foot in vacuum at startup: baseline


def test_supersedes_silences_weaker_sibling():
    empty = rule(id="boost_empty", path=BOOST["path"], op="<=", value=0.01, rearm=0.1, scope="vehicle",
                 supersedes=["boost_low"])
    e = engine(BOOST, empty)
    assert feed(e, [frame(1.0), frame(0.0), frame(0.0)]) == [(0.1, "boost_empty")]
    sch = A.AudioScheduler()
    sch.submit({"rule": "boost_low", "severity": "warn", "t": 0, "sound": "warn", "speech": "x"})
    sch.submit({"rule": "boost_empty", "severity": "warn", "t": 0.5, "sound": "warn", "speech": "y",
                "supersedes": ["boost_low"]})
    assert [a["rule"] for a in sch.queue] == ["boost_empty"]


def test_shield_face_down_respects_tuning():
    r = rule(id="face", path="$shield_faces_down", op=">=", value=1, scope="vehicle", message="down {value:.0f}")
    e = engine(r)

    def fr(lvl, faces):
        return frame(shields={"level": lvl, "faces": faces})
    got = feed(e, [fr(1.0, [1, 1, 1, 1]), fr(1.0, [0.6, 0.6, 1.0, 0.6]), fr(0.9, [0.5, 0.5, 0.9, 0.5]),
                   fr(0.0, [0, 0, 0, 0]), fr(0.6, [0.0, 0.6, 0.9, 0.6])])
    assert got == [(0.4, "face")]  # tuned-down faces and all-zero (shields off) are not DOWN
    e2 = engine(r)  # a face tuned to a 0 share reads 0 at full charge: not DOWN
    assert feed(e2, [fr(1.0, [0.0, 1.0, 1.0, 1.0]), fr(0.8, [0.0, 0.8, 0.8, 0.8])]) == []


def test_quantum_seen_is_per_vehicle():
    r = rule(id="q", path="vehicle.state.flight.instruments.quantum_fuel", op="<=", value=0.15, rearm=0.2,
             scope="vehicle", when={"path": "$quantum_seen", "op": "==", "value": True})

    def fr(q, name="SHIP_A_1"):
        f = frame(name=name)
        f["vehicle"]["state"]["flight"]["instruments"]["quantum_fuel"] = q
        return f
    e = engine(r)
    assert feed(e, [fr(0.0)] * 5) == []  # ships whose quantum always reads 0 never alert
    assert feed(e, [fr(0.5), fr(0.1)], t0=1) == [(1.1, "q")]
    assert feed(e, [fr(0.0, "SHIP_B_2")] * 3, t0=2) == []  # new vehicle: seen resets


def test_flt_max_and_missing_values_are_ignored():
    e = engine(rule(id="alt", path="vehicle.state.flight.instruments.altitude", op="<", value=100, scope="vehicle"))
    f = lambda alt: frame(flight={"instruments": {"altitude": alt}})  # noqa: E731
    assert feed(e, [f(500), f(3.4028235e38), f(None), f(50)]) == [(0.3, "alt")]


def test_words_and_template_safety():
    assert [A.words(n) for n in (0, 7, 20, 25, 99, 100)] == ["zero", "seven", "twenty", "twenty five",
                                                              "ninety nine", "one hundred"]
    assert A.render("x {nope} {value:.1f}", {"value": 0.25}) == "x ? 0.2"
    assert A.render("{value:.1f}", {"value": "?"}) == "{value:.1f}"  # bad format falls back to the raw template


# ---- scheduler + router ---------------------------------------------------------------------------

def al(rule_id, sev, t, speech="x"):
    return {"rule": rule_id, "severity": sev, "t": t, "sound": sev, "speech": speech}


def test_scheduler_gap_priority_preempt_ttl():
    s = A.AudioScheduler(gap_s=2.0, queue_max=3, ttl_s=10.0)
    s.submit(al("a", "info", 0))
    s.submit(al("b", "warn", 0.1))
    assert s.next(0.2)[0]["rule"] == "b"           # higher severity first
    assert s.next(0.5) is None                     # busy / within gap
    s.submit(al("c", "critical", 0.6))
    nxt = s.next(0.7)
    assert nxt[0]["rule"] == "c" and nxt[1] is True  # critical preempts a playing warn
    assert s.next(1.0) is None
    assert s.next(3.0)[0]["rule"] == "a"           # gap passed
    s.submit(al("old", "warn", 0))
    assert s.next(20) is None and s.queue == []    # expired
    for i in range(5):
        s.submit(al(f"i{i}", "info", 30 + i))
    s.submit(al("w", "warn", 29))
    assert len(s.queue) == 3 and "w" in [a["rule"] for a in s.queue]  # least important / oldest dropped
    s.submit({"rule": "silent", "severity": "info", "t": 40, "sound": None, "speech": None})
    assert "silent" not in [a["rule"] for a in s.queue]


def test_scheduler_critical_skips_gap_but_not_after_critical():
    s = A.AudioScheduler(gap_s=2.0)
    s.submit(al("w", "warn", 0, speech=""))
    s.next(0)
    s.submit(al("c1", "critical", 0.9))
    assert s.next(0.9)[0]["rule"] == "c1"      # warn finished (0.6 s) but gap not over: critical goes anyway
    s.submit(al("c2", "critical", 1.0))
    assert s.next(1.0) is None                  # critical does not preempt critical
    assert s.next(3.0)[0]["rule"] == "c2"


def test_router_auto_client_presence_needs_open_stream():
    r = A.AudioRouter("auto")
    assert r.route(0) == ("bridge", None)
    r.heartbeat("tab-aaaaaaaa", True, 0)
    assert r.route(5) == ("bridge", None)              # heartbeat only, no stream: a play job would reach nobody
    s1 = r.sse_open_stream("tab-aaaaaaaa", 11)
    assert r.route(100) == ("browser", "tab-aaaaaaaa")  # open SSE stream keeps it alive (no heartbeat needed)
    r.sse_close("tab-aaaaaaaa", s1, 100)
    assert r.route(100) == ("bridge", None)            # stream gone: bridge at once, nothing lost
    sa = r.sse_open_stream("tab-aaaaaaaa", 199)
    r.heartbeat("tab-aaaaaaaa", True, 200)
    r.sse_open_stream("tab-bbbbbbbb", 200)
    r.heartbeat("tab-bbbbbbbb", True, 201)
    assert r.route(202) == ("browser", "tab-bbbbbbbb")   # newest enabled tab plays: exactly one
    r.heartbeat("tab-bbbbbbbb", False, 203)
    assert r.route(203) == ("browser", "tab-aaaaaaaa") and r.present(203) == 1
    r.mode = "bridge"
    assert r.route(203) == ("bridge", None)
    r.mode = "browser"
    assert r.route(203) == ("browser", "tab-aaaaaaaa")
    r.sse_close("tab-aaaaaaaa", sa, 300)
    assert r.route(300) == ("none", "no audio-enabled dashboard")
    r.mode = "off"
    assert r.route(203) == ("none", "audio off")


# ---- detection ------------------------------------------------------------------------------------

WIN = sys.platform == "win32"


def fake_bin(d: pathlib.Path, *names):
    """Directory of no-op executables found by shutil.which on this OS (.bat via PATHEXT on Windows)."""
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        if WIN:
            (d / f"{n}.bat").write_text("@exit /b 0\r\n", encoding="utf-8")
            continue
        p = d / n
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(d)


def stem(argv):
    return pathlib.Path(argv[0]).stem.lower()


def test_tts_and_player_detection_with_fake_path(tmp_path):
    no_mod = lambda name: None  # noqa: E731
    lin = {"platform": "linux"}
    assert A.detect_tts({}, path=fake_bin(tmp_path / "none"), env={}, find_module=no_mod, **lin) is None
    p = fake_bin(tmp_path / "b1", "spd-say", "espeak")
    assert A.detect_tts({}, path=p, env={}, find_module=no_mod, **lin)["name"] == "espeak"
    p = fake_bin(tmp_path / "b2", "spd-say", "espeak", "espeak-ng", "piper")
    assert A.detect_tts({}, path=p, env={}, find_module=no_mod, **lin)["name"] == "espeak-ng"  # piper needs a voice
    voice = tmp_path / "v.onnx"
    voice.write_bytes(b"x")
    t = A.detect_tts({}, path=p, env={"SCTELEMETRY_PIPER_VOICE": str(voice)}, find_module=no_mod, **lin)
    assert t["name"] == "piper" and t["wav"] and str(voice) in t["argv"]
    t = A.detect_tts({"piper_voice": str(voice)}, path=fake_bin(tmp_path / "b3"), env={},
                     find_module=lambda n: object() if n == "piper" else None, **lin)
    assert t["name"] == "piper" and t["argv"][1:3] == ["-m", "piper"]
    assert A.detect_tts({}, path=p, env={"SCTELEMETRY_PIPER_VOICE": str(tmp_path / "missing.onnx")},
                        find_module=no_mod, **lin)["name"] == "espeak-ng"
    assert stem(A.detect_player(fake_bin(tmp_path / "p1", "paplay"), **lin)) == "paplay"
    assert stem(A.detect_player(fake_bin(tmp_path / "p2", "paplay", "pw-play"), **lin)) == "pw-play"
    assert A.detect_player(fake_bin(tmp_path / "p3"), **lin) is None


def test_player_chain_per_platform(tmp_path):
    lin = fake_bin(tmp_path / "l", "aplay", "ffplay")
    a = A.detect_player(lin, platform="linux")
    assert stem(a) == "aplay" and a[1:] == ["-q"] and A.player_formats(a) == {".wav"}  # ALSA only: WAV only
    f = A.detect_player(fake_bin(tmp_path / "f", "ffplay"), platform="linux")
    assert stem(f) == "ffplay" and "-nodisp" in f and A.player_formats(f) is None
    assert A.player_formats(A.detect_player(fake_bin(tmp_path / "pw", "pw-play"), platform="linux")) is None
    assert A.detect_player(fake_bin(tmp_path / "e"), platform="win32") == [A.WINSOUND]  # stdlib, no PATH lookup
    assert A.player_formats([A.WINSOUND]) == {".wav"}
    mac = A.detect_player(fake_bin(tmp_path / "m", "afplay", "pw-play"), platform="darwin")
    assert stem(mac) == "afplay" and A.player_formats(mac) == {".wav"}
    assert A.detect_player(fake_bin(tmp_path / "m0"), platform="darwin") is None


def test_windows_tts_sapi_reads_text_from_stdin(tmp_path):
    no_mod = lambda name: None  # noqa: E731
    p = fake_bin(tmp_path / "w", "powershell", "spd-say")
    t = A.detect_tts({"rate_wpm": 250}, path=p, env={}, find_module=no_mod, platform="win32")
    assert t["name"] == "sapi" and t["stdin_text"] and not t["wav"]  # spd-say is never used on Windows
    assert stem(t["argv"]) == "powershell" and "{text}" not in " ".join(t["argv"]) and "$s.Rate=3" in t["argv"][-1]
    p = fake_bin(tmp_path / "w2", "powershell", "espeak-ng")
    assert A.detect_tts({}, path=p, env={}, find_module=no_mod, platform="win32")["name"] == "espeak-ng"
    assert A.detect_tts({}, path=fake_bin(tmp_path / "w3", "pwsh"), env={}, find_module=no_mod,
                        platform="win32")["name"] == "sapi"
    assert A.detect_tts({}, path=fake_bin(tmp_path / "w4"), env={}, find_module=no_mod, platform="win32") is None
    assert "PowerShell" in A.tts_hint("win32") and "espeak-ng" in A.tts_hint("linux")


def test_bundled_tones_are_default_fallback_and_valid_wav():
    import wave
    sounds = A.load_defaults()["settings"]["sounds"]
    for sev in ("info", "warn", "critical"):
        f = A.resolve_sound(sounds, None, sev, exts={".wav"})  # WAV-only player (winsound / aplay / afplay)
        assert f and pathlib.Path(f) == A.DATA_DIR / "sounds" / f"{sev}.wav"
        with wave.open(f, "rb") as w:
            assert w.getnchannels() == 1 and 0.1 < w.getnframes() / w.getframerate() < 2
        assert 0.1 < A.wav_seconds(f) < 2


def test_resolve_sound_relative_and_format_filter(tmp_path):
    (tmp_path / "a.oga").write_bytes(b"x")
    (tmp_path / "b.wav").write_bytes(b"x")
    sounds = {"warn": ["a.oga", "b.wav"]}
    assert A.resolve_sound(sounds, None, "warn", base=tmp_path) == str(tmp_path / "a.oga")
    assert A.resolve_sound(sounds, None, "warn", exts={".wav"}, base=tmp_path) == str(tmp_path / "b.wav")
    assert A.resolve_sound({"warn": ["", "nope.wav"]}, None, "warn", base=tmp_path) is None


def test_resolve_sound_falls_back_to_severity(tmp_path):
    f = tmp_path / "w.oga"
    f.write_bytes(b"x")
    sounds = {"warn": [str(tmp_path / "missing.oga"), str(f)], "custom": [str(tmp_path / "nope.oga")]}
    assert A.resolve_sound(sounds, "custom", "warn") == str(f)
    assert A.resolve_sound(sounds, None, "info") is None


def test_local_sink_refuses_under_pytest(monkeypatch):
    monkeypatch.delenv("SCTELEMETRY_ALLOW_AUDIO_TEST", raising=False)
    with pytest.raises(RuntimeError):
        A.LocalAudioSink({}, None, ["pw-play"])


# ---- rules files ----------------------------------------------------------------------------------

def test_default_rules_valid_and_all_kinds_used():
    cfg, err = A.load_config(None)
    assert err is None and len(cfg["rules"]) >= 20
    assert {r["kind"] for r in cfg["rules"]} == set(A.KINDS)
    assert all(r["speech"] is None or len(r["speech"]) <= 40 for r in cfg["rules"])  # short + speakable


@pytest.mark.parametrize("bad,needle", [
    ({"op": "=<"}, "op"),
    ({"rearm": 0.1}, "hysteresis"),
    ({"severity": "loud"}, "severity"),
    ({"bogus": 1}, "unknown field"),
    ({"kind": "event", "event": "zone"}, "event"),
    ({"kind": "event", "event": "ZONE", "detail_re": "("}, "detail_re"),
    ({"message": ""}, "message"),
    ({"scope": "space"}, "scope"),
    ({"when": {"path": "Bad Path"}}, "path"),
])
def test_validation_rejects(bad, needle):
    errs = A.validate_rules([rule(**{**BOOST, **bad})])
    assert errs and any(needle in e for e in errs), errs


def test_validation_duplicate_and_empty():
    assert any("duplicate" in e for e in A.validate_rules([BOOST, BOOST]))
    assert A.validate_rules([]) and A.validate_rules("x")


def test_user_patch_merge_diff_and_broken_file(tmp_path):
    defaults = A.load_defaults()
    user = tmp_path / "alerts.json"
    user.write_text(json.dumps({"rules": [{"id": "boost_low", "enabled": False, "value": 0.3},
                                          {**BOOST, "id": "my_rule"}], "settings": {"speech_gap_s": 3}}), encoding="utf-8")
    cfg, err = A.load_config(user)
    by = {r["id"]: r for r in cfg["rules"]}
    assert err is None and by["boost_low"]["enabled"] is False and by["boost_low"]["value"] == 0.3
    assert by["boost_low"]["speech"] == "Boost {pct_words} percent" and "my_rule" in by
    assert cfg["settings"]["speech_gap_s"] == 3 and cfg["settings"]["sounds"]["warn"]
    patch = A.diff_rules(defaults, cfg["rules"])
    assert {p["id"] for p in patch} == {"boost_low", "my_rule"}
    assert next(p for p in patch if p["id"] == "boost_low") == {"id": "boost_low", "enabled": False, "value": 0.3}
    user.write_text("{not json", encoding="utf-8")
    cfg, err = A.load_config(user)
    assert err and len(cfg["rules"]) == len(defaults["rules"])  # broken user file: defaults, never disabled
    user.write_text(json.dumps({"rules": [{"id": "boost_low", "op": "=<"}]}), encoding="utf-8")
    assert A.load_config(user)[1]


# ---- service --------------------------------------------------------------------------------------

def service(tmp_path=None, mode="auto", rules=None, speech=True):
    cfg = A.merge(A.load_defaults(), None) if rules is None else A.merge({"settings": {"settle_s": 0}, "rules": rules},
                                                                         None)
    cfg["settings"]["settle_s"] = 0
    cfg["settings"]["max_eval_hz"] = 1000
    cfg["settings"]["speech"] = speech
    events = []
    svc = A.AlertService(cfg, user_path=(tmp_path / "alerts.json") if tmp_path else None, mode=mode, dry_run=True,
                         emit_event=events.append, log=lambda m: None, player=["/usr/bin/pw-play"],
                         tts={"name": "espeak-ng", "argv": []})
    return svc, events


def test_service_routes_to_bridge_dry_run_or_browser_never_both():
    svc, events = service(rules=[BOOST])
    svc.on_frame(frame(1.0), now=0)
    svc.on_frame(frame(0.2), now=0.1)
    assert [e["rule"] for e in events] == ["boost_low"] and svc.recent[0]["id"] == 1
    assert [x["kind"] for x in svc.feed] == ["toast"]
    asyncio.run(svc.tick(0.2))
    assert [p["rule"] for p in svc.sink.played] == ["boost_low"] and svc.sink.played[0]["tts"] == "espeak-ng"
    assert [x["kind"] for x in svc.feed] == ["toast"]  # bridge played: no browser play job
    svc.router.heartbeat("tab-aaaaaaaa", True, 1.0)
    svc.router.sse_open_stream("tab-aaaaaaaa", 1.0)
    svc.on_frame(frame(0.5), now=1.0)
    svc.on_frame(frame(0.2), now=1.1)
    asyncio.run(svc.tick(3.0))
    play = [x for x in svc.feed if x["kind"] == "play"]
    assert len(svc.sink.played) == 1 and len(play) == 1 and play[0]["target"] == "tab-aaaaaaaa"
    assert A.AlertService.for_client(play[0], "tab-aaaaaaaa")["play"] is True
    assert A.AlertService.for_client(play[0], "tab-bbbbbbbb")["play"] is False
    assert "target" not in A.AlertService.for_client(play[0], None)
    assert svc.status(3.0)["route"] == {"to": "browser"} and svc.status(3.0)["browser_clients"] == 1


def test_service_mute_mode_off_and_test_alert():
    svc, events = service(rules=[BOOST])
    svc.mute(5, now=0)
    svc.on_frame(frame(1.0), now=0)
    svc.on_frame(frame(0.2), now=0.1)
    asyncio.run(svc.tick(0.2))
    assert len(events) == 1 and svc.sink.played == []  # toast yes, audio no
    a = svc.fire_test("boost_low", now=0.3)
    assert a["text"].startswith("TEST: ") and a["test"]
    asyncio.run(svc.tick(0.4))
    assert [p["rule"] for p in svc.sink.played] == ["boost_low"]  # test ignores mute
    assert svc.fire_test("nope") is None
    svc.mute(0, now=1)
    assert not svc.status(1)["muted"]
    svc.set_mode("off")
    svc.fire_test("boost_low", now=5)
    asyncio.run(svc.tick(5))
    assert len(svc.sink.played) == 1 and svc.status(5)["route"]["to"] == "none"
    with pytest.raises(ValueError):
        svc.set_mode("loud")


def test_service_set_rules_saves_diff_and_rebaselines(tmp_path):
    svc, _ = service(tmp_path)
    rules = svc.rules()
    for r in rules:
        if r["id"] == "boost_low":
            r["enabled"] = False
    svc.set_rules(rules)
    saved = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert saved["rules"] == [{"id": "boost_low", "enabled": False}]
    assert not svc.engine.by_id["boost_low"]["enabled"]
    bad = copy.deepcopy(rules)
    bad[0]["op"] = "=<"
    with pytest.raises(ValueError):
        svc.set_rules(bad)
    assert json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8")) == saved  # invalid PUT never written
    svc.set_rules([r for r in rules if r["id"] != "corpse"])
    assert "corpse" not in svc.engine.by_id


def test_source_change_rebaselines():
    svc, events = service(rules=[BOOST])
    svc.on_source("replay x", now=0)
    svc.on_frame(frame(1.0), now=0)
    svc.on_frame(frame(1.0), now=0.1)
    svc.engine.settings["settle_s"] = 0.5
    svc.on_source("replay x · loop 2", now=0.2)
    svc.on_frame(frame(0.1), now=0.25)  # loop restart jumps values: baseline, not an alert
    assert events == []


# ---- recordings -----------------------------------------------------------------------------------

def replay(path):
    e = A.AlertEngine(A.load_config(None)[0])
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if "frame" in rec:
                out += [(rec["t"], a["rule"]) for a in e.evaluate_frame(rec["frame"], rec["t"])]
    return out


@pytest.mark.skipif(not (PERSEUS.is_file() and PALADIN.is_file()), reason="local recordings not present")
def test_replay_recordings_sensible_counts():
    import collections
    per = collections.Counter(r for _, r in replay(PERSEUS))
    pal = collections.Counter(r for _, r in replay(PALADIN))
    # Perseus: boost runs dry twice, proximity MAJOR three times, gear down at speed once
    assert per["boost_low"] == 1 and per["boost_empty"] == 2 and per["proximity_warning"] == 3
    assert per["gear_overspeed"] == 1
    # Paladin: shields charge from 0 at power-on and are tuned 60/220/60/60: neither is low / DOWN
    assert pal["boost_low"] == 1 and pal["boost_empty"] == 1
    for c in (per, pal):
        assert c["shield_face_down"] == 0 and c["shields_low"] == 0 and c["hydrogen_low"] == 0
        assert c["hydrogen_critical"] == 0  # hydrogen reads 0 with engines off
        assert sum(c.values()) <= 10
    assert set(pal) <= {"boost_low", "boost_empty"}



# ---- hardening (review findings) ------------------------------------------------------------------

@pytest.mark.parametrize("user", [
    {"settings": {"max_eval_hz": "fast"}}, {"settings": {"stale_s": None}}, {"settings": {"settle_s": "x"}},
    {"settings": {"speech_gap_s": "x"}}, {"settings": {"queue_max": 0}}, {"settings": {"sounds": {"warn": 5}}},
    {"settings": {"sounds": ["a"]}}, {"settings": {"tts": "espeak"}}, {"settings": {"tts": {"rate_wpm": "x"}}},
    "just a string", [1, 2], {"removed": "boost_low"},
])
def test_bad_user_settings_fall_back_to_defaults(tmp_path, user):
    f = tmp_path / "alerts.json"
    f.write_text(json.dumps(user), encoding="utf-8")
    cfg, err = A.load_config(f)
    assert err and cfg["settings"]["max_eval_hz"] == 20 and len(cfg["rules"]) == len(A.load_defaults()["rules"])
    svc = A.AlertService(cfg, dry_run=True, log=lambda m: None)  # a working service from the fallback
    svc.on_frame(frame(1.0), now=0)
    asyncio.run(svc.tick(0.1))


def test_set_rules_keeps_user_settings_and_persists_removed_and_null(tmp_path):
    user = tmp_path / "alerts.json"
    settings = {"speech_gap_s": 4.0, "tts": {"piper_voice": "/x/voice.onnx"}, "sounds": {"warn": ["/my/warn.wav"]}}
    user.write_text(json.dumps({"settings": settings, "rules": [{"id": "radiation", "enabled": False}]}), encoding="utf-8")
    cfg, err = A.load_config(user)
    assert err is None
    svc = A.AlertService(cfg, user_path=user, dry_run=True, log=lambda m: None)
    rules = svc.rules()
    for r in rules:
        if r["id"] == "boost_low":
            r["enabled"] = False
        if r["id"] == "hydrogen_low":
            r["when"] = None  # explicit null unsets a default field (saved as null)
    rules = [r for r in rules if r["id"] != "corpse"]
    svc.set_rules(rules)
    saved = json.loads(user.read_text(encoding="utf-8"))
    assert saved["settings"] == settings and saved["removed"] == ["corpse"]
    by = {r["id"]: r for r in saved["rules"]}
    assert by["radiation"] == {"id": "radiation", "enabled": False} and by["hydrogen_low"] == {"id": "hydrogen_low",
                                                                                            "when": None}
    cfg2, err = A.load_config(user)  # after a restart
    ids = {r["id"]: r for r in cfg2["rules"]}
    assert err is None and "corpse" not in ids and "when" not in ids["hydrogen_low"]
    assert not ids["boost_low"]["enabled"] and cfg2["settings"]["speech_gap_s"] == 4.0
    assert "session_join" in ids and ids["session_join"].get("speech") is None
    assert A.diff_rules(A.load_defaults(), [r for r in svc.rules() if r["id"] == "session_join"]) == []


@pytest.mark.parametrize("tpl,ok", [
    ("Boost {pct}%", True), ("Health {value:.0f}", True), ("{count:>3} items", True), ("no fields", True),
    ("{value:>200000000}", False), ("{value.__class__}", False), ("{value[0]}", False), ("{nope}", False),
    ("x { y", False), ("{pct}}", False),
])
def test_template_whitelist(tpl, ok):
    errs = A.validate_rules([rule(**{**BOOST, "message": tpl})])
    assert (not errs) is ok, errs


def test_tts_argv_ends_options_and_detail_re_capped(tmp_path):
    p = fake_bin(tmp_path / "b", "espeak-ng")
    t = A.detect_tts({}, path=p, env={}, find_module=lambda n: None, platform="linux")
    assert t["argv"][-2:] == ["--", "{text}"]
    long_re = rule(id="ev", kind="event", event="ZONE", detail_re="a" * 201)
    assert any("detail_re" in e for e in A.validate_rules([long_re]))


def test_speech_matches_toast_test_fields_and_no_health_low_at_death():
    cfg, _ = A.load_config(None)
    cfg["settings"].update(settle_s=0, max_eval_hz=1000)
    e = A.AlertEngine(cfg)
    hold = {"vehicle.state.power.engines": True}
    f = lambda b: {"vehicle": {"state": {"name": "S_1", "power": {"engines": True},  # noqa: E731
                                         "flight": {"instruments": {"boost": {"reserves": b}}}}}}
    e.evaluate_frame(f(1.0), 0)
    a = e.evaluate_frame(f(0.2), 0.1)
    assert hold and [x["rule"] for x in a] == ["boost_low"] and a[0]["text"] == "Boost 20%"
    assert a[0]["speech"] == "Boost twenty percent"
    assert e.test_alert("corpse", 1)["text"] == "TEST: Corpse nearby: 3 items"
    assert e.test_alert("session_join", 1)["text"] == "TEST: Shard: example"
    me = lambda h: {"actor": {"vitals": {"health": {"current": h}}}}  # noqa: E731
    out = []
    for i, h in enumerate([95.9, 95.9, 0.0, 0.0, 100.0, 100.0, 25.0]):
        out += [x["rule"] for x in e.evaluate_frame(me(h), 10 + i * 0.1)]
    assert out == ["health_low"]  # death (95.9 -> 0) is silent; real low health while alive fires


def test_speech_off_by_default_strips_speech_keeps_sound_and_toast():
    assert A.load_defaults()["settings"]["speech"] is False
    svc, events = service(rules=[BOOST], speech=False)
    svc.on_frame(frame(1.0), now=0)
    svc.on_frame(frame(0.2), now=0.1)
    assert [e["rule"] for e in events] == ["boost_low"]
    assert svc.recent[0]["speech"] is None and svc.recent[0]["text"]
    asyncio.run(svc.tick(0.2))
    assert [p["rule"] for p in svc.sink.played] == ["boost_low"] and svc.sink.played[0]["tts"] is None


def test_settings_speech_must_be_bool():
    assert any("settings.speech" in e for e in A.validate_settings({**A.load_defaults()["settings"], "speech": "yes"}))


def test_local_sink_feeds_stdin_tts_and_routes_winsound(monkeypatch):
    monkeypatch.setenv("SCTELEMETRY_ALLOW_AUDIO_TEST", "1")  # _exec / _winsound are stubbed: nothing plays
    calls = []

    async def fake_exec(self, argv, stdin=None, timeout=15.0):
        calls.append(("exec", argv, stdin))
        return 0

    async def fake_win(self, f):
        calls.append(("winsound", f))

    monkeypatch.setattr(A.LocalAudioSink, "_exec", fake_exec)
    monkeypatch.setattr(A.LocalAudioSink, "_winsound", fake_win)
    sounds = A.load_defaults()["settings"]["sounds"]
    tts = {"name": "sapi", "argv": ["powershell", "-Command", "x"], "stdin_text": True, "wav": False}
    sink = A.LocalAudioSink(sounds, tts, [A.WINSOUND])
    alert = {"rule": "r", "severity": "warn", "sound": "warn", "speech": "-rm hull breach", "t": 0}

    async def go():
        await sink.play(alert)
        await sink._task

    asyncio.run(go())
    assert calls[0] == ("winsound", str(A.DATA_DIR / "sounds" / "warn.wav"))  # .oga skipped for winsound
    assert calls[1] == ("exec", ["powershell", "-Command", "x"], b"-rm hull breach")  # text on stdin only


def test_corpse_burst_is_not_your_death_and_telemetry_death_events_are():
    """2026-11-21 regression: a kill nearby streamed the victim's corpse items while the player sat in
    their ship at 63 health; the old corpse rule announced 'You died'."""
    cfg, _ = A.load_config(None)
    rules = {r["id"]: r for r in cfg["rules"]}
    assert rules["corpse"]["enabled"] is False and "died" not in rules["corpse"]["message"].lower()
    e = A.AlertEngine(cfg)
    assert e.evaluate_event({"type": "CORPSE_ITEMS", "t": 100.0, "count": 39,
                             "detail": "corpse nearby: 39 items (owner unknown)"}, 100.0) == []
    died = e.evaluate_event({"type": "GAME_EVENT_TYPE_ACTOR_DEAD", "t": 200.0}, 200.0)
    assert [a["rule"] for a in died] == ["you_died"] and died[0]["severity"] == "critical"
    down = e.evaluate_event({"type": "GAME_EVENT_TYPE_ACTOR_DOWNED", "t": 300.0}, 300.0)
    assert [a["rule"] for a in down] == ["you_down"]
