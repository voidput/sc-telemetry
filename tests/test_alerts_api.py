"""Bridge alert endpoints + SSE alert lines + hub hooks. Dry-run service only: nothing is ever played."""
import json

import pytest
from fastapi.testclient import TestClient

from sctelemetry import alerts as A
from sctelemetry import server as srv

from .test_alerts import BOOST, frame

BAD_HEADERS = ({"host": "evil.example:8766"}, {"host": "127.0.0.1:8766", "origin": "http://evil.example:8766"},
               {"host": "192.0.2.5:8766"})


@pytest.fixture
def svc(monkeypatch, tmp_path):
    h = srv.Hub()
    monkeypatch.setattr(srv, "hub", h)
    monkeypatch.setattr(srv.CFG, "host", "127.0.0.1", raising=False)
    monkeypatch.setattr(srv.CFG, "port", 8766, raising=False)
    cfg = A.merge(A.load_defaults(), None)
    cfg["settings"].update(settle_s=0, max_eval_hz=1e6)
    s = A.AlertService(cfg, user_path=tmp_path / "alerts.json", dry_run=True, emit_event=srv._alert_event,
                       log=lambda m: None, player=None, tts=None)
    monkeypatch.setattr(srv, "ALERTS", s)
    h.on_frame = lambda d: s.on_frame(d)
    h.on_event = lambda ev: s.on_event(ev)
    return s


@pytest.fixture
def client(svc):
    return TestClient(srv.app, base_url="http://127.0.0.1:8766")


def test_frames_through_hub_become_alert_events(svc):
    srv.hub.push(frame(1.0))
    srv.hub.push(frame(0.2))
    ev = srv.hub.events[0]
    assert ev["type"] == "ALERT" and ev["rule"] == "boost_low" and ev["severity"] == "warn" and ev["detail"] == "Boost 20%"
    assert svc.recent[0]["rule"] == "boost_low"
    srv.hub.add_event("ZONE", None, "Leaving Armistice Zone - Caution Advised", kind="other", text="x")
    assert srv.hub.events[0]["type"] == "ALERT" and srv.hub.events[0]["rule"] == "leaving_armistice"
    assert srv.hub.snapshot()["events"][0]["type"] == "ALERT"


def test_get_alerts_and_rules(client, svc):
    j = client.get("/api/alerts").json()
    assert j["recent"] == [] and j["audio"]["mode"] == "auto" and j["audio"]["route"] == {"to": "bridge"}
    assert j["audio"]["bridge"]["sink"] == "dry-run" and any(r["id"] == "boost_low" for r in j["rules"])
    assert j["config"]["user_file"]
    r = client.get("/api/alerts/rules").json()
    assert len(r["rules"]) == len(j["rules"]) and r["rules"][0]["message"]


def test_mutating_endpoints_are_guarded(client):
    calls = [("put", "/api/alerts/rules", {"rules": []}), ("post", "/api/alerts/test", {"id": "boost_low"}),
             ("post", "/api/alerts/mute", {"minutes": 5}), ("put", "/api/alerts/audio", {"mode": "off"}),
             ("post", "/api/alerts/audio-client", {"client_id": "tab-aaaaaaaa", "enabled": True})]
    for method, url, body in calls:
        for h in BAD_HEADERS:
            r = getattr(client, method)(url, json=body, headers=h)
            assert r.status_code == 403, (url, h)
        r = getattr(client, method)(url, content=json.dumps(body), headers={"content-type": "text/plain"})
        assert r.status_code == 415, url
        r = getattr(client, method)(url, content=b"[1]", headers={"content-type": "application/json"})
        assert r.status_code == 400, url
    big = {"rules": [{"x": "y" * 70000}]}
    assert client.put("/api/alerts/rules", json=big).status_code == 413


def test_put_rules_validates_and_saves(client, svc, tmp_path):
    rules = client.get("/api/alerts/rules").json()["rules"]
    bad = [dict(r) for r in rules]
    bad[0]["op"] = "=<"
    r = client.put("/api/alerts/rules", json={"rules": bad})
    assert r.status_code == 400 and r.json()["status"] == "INVALID" and not (tmp_path / "alerts.json").exists()
    assert client.put("/api/alerts/rules", json={"rules": "x"}).status_code == 400
    for x in rules:
        if x["id"] == "boost_low":
            x["enabled"] = False
    r = client.put("/api/alerts/rules", json={"rules": rules})
    assert r.status_code == 200 and r.json()["rules"] == len(rules)
    assert json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))["rules"] == [{"id": "boost_low", "enabled": False}]
    assert not next(x for x in client.get("/api/alerts").json()["rules"] if x["id"] == "boost_low")["enabled"]


def test_test_mute_mode_and_audio_client(client, svc):
    r = client.post("/api/alerts/test", json={"id": "missile_warning"})
    assert r.status_code == 200 and r.json()["alert"]["text"] == "TEST: Missile warning"
    assert srv.hub.events[0]["type"] == "ALERT" and srv.hub.events[0]["test"] is True
    assert client.post("/api/alerts/test", json={"id": "nope"}).status_code == 404
    assert client.post("/api/alerts/test", json={"id": "Bad Id"}).status_code == 400
    assert client.post("/api/alerts/mute", json={"minutes": 999}).status_code == 400
    assert client.post("/api/alerts/mute", json={"minutes": True}).status_code == 400
    assert client.post("/api/alerts/mute", json={"minutes": 15}).json()["audio"]["muted"] is True
    assert client.post("/api/alerts/mute", json={"minutes": 0}).json()["audio"]["muted"] is False
    assert client.put("/api/alerts/audio", json={"mode": "loud"}).status_code == 400
    assert client.put("/api/alerts/audio", json={"mode": "browser"}).json()["audio"]["route"]["to"] == "none"
    client.put("/api/alerts/audio", json={"mode": "auto"})
    assert client.post("/api/alerts/audio-client", json={"client_id": "short", "enabled": True}).status_code == 400
    j = client.post("/api/alerts/audio-client", json={"client_id": "tab-aaaaaaaa", "enabled": True, "speech": False}).json()
    assert j["target"] is False and j["audio"]["route"]["to"] == "bridge"  # no /events stream open: not a target
    import time
    stream = svc.router.sse_open_stream("tab-aaaaaaaa", time.time())
    j = client.post("/api/alerts/audio-client", json={"client_id": "tab-aaaaaaaa", "enabled": True, "speech": False}).json()
    assert j["target"] is True and j["audio"]["route"]["to"] == "browser"
    j = client.post("/api/alerts/audio-client", json={"client_id": "tab-aaaaaaaa", "enabled": False}).json()
    assert j["target"] is False and j["audio"]["route"]["to"] == "bridge"
    svc.router.sse_close("tab-aaaaaaaa", stream, time.time())


def test_sse_alert_lines_are_named_events_per_client(svc):
    import asyncio
    import time
    svc.router.heartbeat("tab-aaaaaaaa", True, time.time())
    stream = svc.router.sse_open_stream("tab-aaaaaaaa", time.time())
    svc.fire_test("boost_low")
    asyncio.run(svc.tick())
    lines, last = srv.sse_alert_lines("tab-aaaaaaaa", 0, stream)
    assert len(lines) == 2 and all(x.startswith("event: alert\ndata: ") and x.endswith("\n\n") for x in lines)
    toast, play = (json.loads(x.split("data: ", 1)[1]) for x in lines)
    assert toast["kind"] == "toast" and play["kind"] == "play" and play["play"] is True and "target" not in play
    other, _ = srv.sse_alert_lines("tab-bbbbbbbb", 0)
    assert json.loads(other[1].split("data: ", 1)[1])["play"] is False
    assert srv.sse_alert_lines("tab-aaaaaaaa", last, stream) == ([], last)
    assert toast["alert"]["boot"] == svc.boot


def test_two_streams_same_client_id_only_newest_plays(svc):
    """A duplicated tab can clone the client id: exactly one connection gets play:true."""
    import asyncio
    import time
    now = time.time()
    s1 = svc.router.sse_open_stream("tab-dupdupdup1", now)
    s2 = svc.router.sse_open_stream("tab-dupdupdup1", now)
    svc.router.heartbeat("tab-dupdupdup1", True, now)
    svc.fire_test("hull_critical")
    asyncio.run(svc.tick())
    plays = []
    for st in (s1, s2):
        lines, _ = srv.sse_alert_lines("tab-dupdupdup1", 0, st)
        plays += [json.loads(x.split("data: ", 1)[1])["play"] for x in lines
                  if json.loads(x.split("data: ", 1)[1])["kind"] == "play"]
    assert sorted(plays) == [False, True] and svc.sink.played == []
    svc.router.sse_close("tab-dupdupdup1", s2, now)  # newest closes: the older stream takes over
    assert svc.router.is_newest("tab-dupdupdup1", s1)


def test_hub_hook_errors_never_stop_the_stream(monkeypatch, capsys):
    h = srv.Hub()
    monkeypatch.setattr(srv, "hub", h)

    def boom(_):
        raise ValueError("bad settings")
    h.on_frame = h.on_event = boom
    for _ in range(3):
        h.push(frame(0.5))
    h.add_event("ZONE", None, "x")
    assert h.frames_total == 3 and h.frame is not None and h.events[0]["type"] == "ZONE"
    err = capsys.readouterr().err
    assert err.count("on_frame hook failed") == 1 and err.count("on_event hook failed") == 1


def test_run_alerts_survives_a_failing_tick(monkeypatch, capsys):
    import asyncio
    calls = []

    class Bad:
        def on_source(self, s):
            pass

        async def tick(self):
            calls.append(1)
            raise TypeError("sounds")
    monkeypatch.setattr(srv, "ALERTS", Bad())
    monkeypatch.setattr(srv, "ALERTS_TICK_S", 0.001)

    async def go():
        t = asyncio.create_task(srv.run_alerts())
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(go())
    assert len(calls) > 2 and capsys.readouterr().err.count("tick failed") == 1


def test_put_rules_null_is_a_clean_400(client):
    r = client.put("/api/alerts/rules", json={"rules": None})
    assert r.status_code == 400 and r.json()["message"] == "rules must be a list"


def test_endpoints_503_without_service(monkeypatch):
    monkeypatch.setattr(srv, "ALERTS", None)
    monkeypatch.setattr(srv.CFG, "host", "127.0.0.1", raising=False)
    monkeypatch.setattr(srv.CFG, "port", 8766, raising=False)
    c = TestClient(srv.app, base_url="http://127.0.0.1:8766")
    assert c.get("/api/alerts").status_code == 503 and srv.sse_alert_lines(None, 3) == ([], 3)


def test_bad_env_audio_mode_rejected(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(srv, "CFG", argparse.Namespace())  # argparse keeps attributes already on CFG
    monkeypatch.setenv("SCTELEMETRY_ALERTS_AUDIO", "loud")
    monkeypatch.setattr("sys.argv", ["server"])
    monkeypatch.setattr(srv.uvicorn, "run", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as e:
        srv.main()
    assert e.value.code == 2 and "loud" in capsys.readouterr().err


def test_boost_rule_fixture_matches_defaults():
    d = {r["id"]: r for r in A.load_defaults()["rules"]}
    assert d["boost_low"]["value"] == BOOST["value"] and d["boost_low"]["rearm"] == BOOST["rearm"]
