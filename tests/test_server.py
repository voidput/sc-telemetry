"""Bridge integration: session plumbing, privacy of snapshot/SSE/session endpoints, --record / --replay
session records. Synthetic data only (TEST-NET address, fake ids)."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from scgameapi import gamelog as gl
from scgameapi import server as srv

from .test_gamelog import LINES

SECRETS = ("192.0.2.7", "64000", "11111111-2222-3333-4444-555555555555", "ptu-sc-alpha-500-1-2")


def full_session():
    st = gl.SessionState()
    evs = []
    for ev in filter(None, map(gl.parse_line, LINES)):
        evs += st.apply(ev)
    return {**st.as_dict(), "source": "game_log", "log_found": True}, evs


@pytest.fixture
def hub(monkeypatch):
    h = srv.Hub()
    monkeypatch.setattr(srv, "hub", h)
    monkeypatch.setattr(srv.CFG, "host", "127.0.0.1", raising=False)
    monkeypatch.setattr(srv.CFG, "port", 8766, raising=False)
    return h


@pytest.fixture
def client(hub):
    return TestClient(srv.app, base_url="http://127.0.0.1:8766")  # no `with`: lifespan (gRPC/stubs) not run


def test_set_session_adds_events_and_bumps_seq(hub):
    s, evs = full_session()
    seq = hub.seq
    seen = []
    hub.on_session = seen.append
    hub.set_session(s, evs)
    assert hub.seq == seq + 1 and seen == [s]
    got = list(hub.events)
    assert [e["type"] for e in got] == ["SESSION_SYSTEM", "SESSION_JOIN", "SESSION_SYSTEM"][::-1]
    assert len({e["id"] for e in got}) == 3 and got[0]["detail"] == "Nyx"


def test_snapshot_never_carries_private_fields(hub):
    hub.set_session(*full_session())
    hub.log_age = lambda: 12.34
    snap = hub.snapshot()
    sess = snap["session"]
    assert not set(gl.PRIVATE_FIELDS) & set(sess)
    assert sess["has_server_address"] and sess["has_session_ids"] and sess["log_age_s"] == 12.3
    assert not any(x in json.dumps(snap) for x in SECRETS)
    assert hub.session["server_address"] == "192.0.2.7"  # kept server-side for the guarded endpoint


def test_api_session_public(client, hub):
    hub.set_session(*full_session())
    body = client.get("/api/session").text
    assert '"shard":"ptu_use1b_1_020"' in body and not any(x in body for x in SECRETS)


def test_api_session_null_when_disabled(client):
    assert client.get("/api/session").json() == {"session": None}
    assert client.get("/api/session/private").json() == {"session": None}


def test_private_endpoint_same_origin_only(client, hub):
    hub.set_session(*full_session())
    r = client.get("/api/session/private")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["session"] == {"server_address": "192.0.2.7", "server_port": 64000,
                                   "session_id": "11111111-2222-3333-4444-555555555555",
                                   "env_session": "ptu-sc-alpha-500-1-2"}
    for headers in ({"host": "evil.example:8766"},                                     # DNS rebinding
                    {"host": "127.0.0.1:8766", "origin": "http://evil.example:8766"},  # cross-site
                    {"host": "192.168.1.5:8766"}):                                      # LAN client
        r = client.get("/api/session/private", headers=headers)
        assert r.status_code == 403 and not any(x in r.text for x in SECRETS)


def test_session_recorder_strips_private_and_dedupes():
    buf = io.StringIO()
    rec = srv.session_recorder(buf)
    s, _ = full_session()
    rec(s)
    rec(dict(s))  # identical -> skipped
    rec({**s, "system": "Stanton"})
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert len(lines) == 2 and all(set(x) == {"t", "session"} for x in lines)
    assert not any(set(gl.PRIVATE_FIELDS) & set(x["session"]) for x in lines)
    assert not any(x in buf.getvalue() for x in SECRETS)


def test_replay_records_skip_session_and_junk():
    warned = []
    lines = [json.dumps({"t": 1.0, "session": {"shard": "x"}}), json.dumps({"t": 2.0, "frame": {"a": 1}}),
             "not json", json.dumps({"t": "nan", "frame": {}}), json.dumps({"t": 3.0, "frame": [1]}),
             json.dumps({"t": 4.0, "frame": {"b": 2}})]
    assert list(srv.replay_records(lines, warned.append)) == [(2.0, {"a": 1}), (4.0, {"b": 2})]
    assert warned == [[1]]


def test_bad_env_channel_rejected(monkeypatch, capsys):
    monkeypatch.setenv("SCGAMEAPI_CHANNEL", "PTUU")
    monkeypatch.setattr("sys.argv", ["server"])
    monkeypatch.setattr(srv.uvicorn, "run", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as e:
        srv.main()
    assert e.value.code == 2 and "PTUU" in capsys.readouterr().err


def _run_main(monkeypatch, argv, env):
    import argparse
    monkeypatch.setattr(srv, "CFG", argparse.Namespace())
    for k in ("SC_DIR", "SCGAMEAPI_CHANNEL", "SCGAMEAPI_GAMEDATA"):
        monkeypatch.setenv(k, "x")  # registered so monkeypatch restores what main() writes
        monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("sys.argv", ["server", *argv])
    started = []
    monkeypatch.setattr(srv.uvicorn, "run", lambda *a, **k: started.append(k))
    srv.main()
    assert started
    return srv.CFG


def test_main_detects_install_and_last_played_channel(monkeypatch, tmp_path, capsys):
    import os
    sc = tmp_path / "sc"
    for i, ch in enumerate(("LIVE", "PTU")):
        (sc / ch).mkdir(parents=True)
        (sc / ch / "Game.log").write_text("x\n", encoding="utf-8")
        os.utime(sc / ch / "Game.log", (1_700_000_000 + i, 1_700_000_000 + i))
    cfg = _run_main(monkeypatch, [], {"SC_DIR": str(sc)})
    assert cfg.channel == "PTU" and cfg.game_log_path == sc / "PTU" / "Game.log"
    assert os.environ["SCGAMEAPI_CHANNEL"] == "PTU" and os.environ["SC_DIR"] == str(sc)  # schema follows
    cfg = _run_main(monkeypatch, ["--channel", "live", "--sc-dir", str(sc)], {})
    assert cfg.channel == "LIVE" and cfg.game_log_path == sc / "LIVE" / "Game.log"


def test_main_without_install_warns_and_still_starts(monkeypatch, capsys):
    monkeypatch.setattr(srv.paths, "find_sc_dir", lambda explicit=None, **k: None)
    cfg = _run_main(monkeypatch, [], {})
    assert cfg.channel == "LIVE" and cfg.game_log_path is None
    assert "no Star Citizen install found" in capsys.readouterr().err


# ---- Game.log signals ------------------------------------------------------------------------

def signals_state():
    from .test_signals import feed, hud, nav, qt, vcf
    st = gl.SignalsState()
    evs = feed(st, [vcf("RSI_Perseus_000000000001", 0), nav("DRAK_Corsair_000000000011", 1),
                    qt("RSI_Perseus_000000000001", 2), hud("Entering Armistice Zone - Combat Prohibited", 3)])
    return st, evs


FAKE_IDS = ("000000000001", "000000000011")


def test_snapshot_and_api_signals_without_ids(client, hub):
    assert hub.snapshot()["signals"] is None and client.get("/api/signals").json() == {"signals": None}
    st, evs = signals_state()
    hub.signals = st
    hub.set_session(*full_session())
    hub.set_session(hub.session, evs)
    snap = hub.snapshot()
    sig = snap["signals"]
    assert sig["armistice"] and sig["nearby_count"] == 1 and sig["own_ship"]["class"] == "RSI_Perseus"
    assert sig["nearby"][0]["display"] == "DRAK Corsair"
    by_type = {e["type"]: e for e in snap["events"]}
    assert by_type["QT_ARRIVED"]["self"] is True and by_type["QT_ARRIVED"]["class"] == "RSI_Perseus"
    assert by_type["ZONE"]["kind"] == "armistice" and by_type["NEARBY_SHIP"]["count"] == 1
    body = client.get("/api/signals").text
    assert '"armistice":true' in body
    for blob in (json.dumps(snap), body):
        assert not any(x in blob for x in FAKE_IDS + SECRETS)


def test_push_feeds_own_vehicle_from_telemetry(hub):
    st, _ = signals_state()
    hub.signals = st
    hub.push({"vehicle": {"state": {"name": "DRAK_Corsair_000000000011", "class_name": "DRAK_Corsair"}}})
    sig = hub.public_signals()
    assert sig["nearby_count"] == 0 and sig["own_ship"] == {"class": "DRAK_Corsair", "display": "DRAK Corsair",
                                                            "source": "telemetry"}
    hub.push({"vehicle": "junk"})  # malformed frames are ignored
    hub.push({})


def test_signals_recorder_strips_ids_and_dedupes(hub):
    buf = io.StringIO()
    st, evs = signals_state()
    hub.signals = st
    hub.on_signals = srv.signals_recorder(buf)
    hub.set_session(*full_session())
    hub.set_session(hub.session, evs)  # same public view -> written once
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert len(lines) == 1 and set(lines[0]) == {"t", "signals"} and lines[0]["signals"]["armistice"]
    assert not any(x in buf.getvalue() for x in FAKE_IDS)
    assert list(srv.replay_records(buf.getvalue().splitlines())) == []  # replay skips them
