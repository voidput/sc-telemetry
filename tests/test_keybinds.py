"""Trigger action -> keybind info (sctelemetry/keybinds.py) + /api/actions `keybind`. Synthetic data only."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from sctelemetry import keybinds as kb
from sctelemetry import server as srv


def action(name, label=None, kbd=None, mode="press", **extra):
    return {"name": name, "label": label, "activationMode": mode,
            "defaults": {"keyboard": kbd or [], "mouse": None, "gamepad": [], "joystick": None}, "flags": {}, **extra}


DATA = {"_build": "9.9.9.1", "actionmaps": [
    {"name": "player", "actions": [action("mobiglas", "mobiGlas (Toggle)", ["f1"]), action("visor_wipe")]},
    {"name": "spaceship_hud", "actions": [action("mobiglas", "mobiGlas (Toggle)", ["f1"])]},
    {"name": "lights_controller", "actions": [action("v_lights", "Headlights (Toggle)", ["l"], future=1), "junk",
                                              {"label": "no name"}]},
    {"name": "vehicle_general", "actions": [action("visor_wipe", "Wipe Visor", ["lalt+x"], mode="tap")]},
    "junk"]}


def write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_build_index_lists_every_actionmap_and_first_label():
    idx = kb.build_index(DATA)
    assert set(idx) == {"mobiglas", "visor_wipe", "v_lights"}
    assert idx["mobiglas"] == {"label": "mobiGlas (Toggle)", "activation_mode": "press",
                               "actionmaps": ["player", "spaceship_hud"], "defaults": {"keyboard": ["f1"]}}
    # first actionmap had no label / no keys -> later one fills them
    assert idx["visor_wipe"]["label"] == "Wipe Visor" and idx["visor_wipe"]["defaults"] == {"keyboard": ["lalt+x"]}
    assert idx["visor_wipe"]["actionmaps"] == ["player", "vehicle_general"]
    assert kb.build_index(None) == {} and kb.build_index({"actionmaps": "x"}) == {}


def test_candidates_and_reload(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    assert kb.candidates("PTU", None, root=root) == [root / ".tmp/gamedata/ptu/game-keybinds.json"]
    d = tmp_path / "custom"
    ships = write(d / "game-ships.json", {})
    assert kb.candidates("LIVE", ships, root=root)[0] == d / "game-keybinds.json"  # file override -> its dir
    k = kb.Keybinds([d / "game-keybinds.json"])
    assert k.get("v_lights") is None and k.status()["loaded"] is False and k.status()["error"] is None
    src = write(d / "game-keybinds.json", DATA)
    monkeypatch.setattr(kb, "RECHECK_S", 0.0)
    assert k.get("v_lights")["defaults"] == {"keyboard": ["l"]}
    assert k.status() == {"loaded": True, "source": "custom/game-keybinds.json", "source_build": "9.9.9.1",
                          "actions": 3, "error": None}
    src.write_text("{half", encoding="utf-8")
    os.utime(src, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns + 10**9))
    assert k.get("v_lights")["label"] == "Headlights (Toggle)"  # broken file keeps the previous index
    assert "JSONDecodeError" in k.status()["error"] and str(tmp_path) not in k.status()["error"]


@pytest.fixture
def client(monkeypatch, tmp_path):
    h = srv.Hub()
    monkeypatch.setattr(srv, "hub", h)
    monkeypatch.setattr(srv.CFG, "host", "127.0.0.1", raising=False)
    monkeypatch.setattr(srv.CFG, "port", 8766, raising=False)
    monkeypatch.setattr(srv.app.state, "fake", srv.FakeTrigger(mutate_mock=False), raising=False)
    h.keybinds = kb.Keybinds([write(tmp_path / "game-keybinds.json", DATA)])
    return h, TestClient(srv.app, base_url="http://127.0.0.1:8766")


def test_api_actions_carries_keybind(client):
    h, c = client
    acts = c.get("/api/actions").json()["actions"]
    by = {a["action_name"]: a for a in acts}
    assert by["v_lights"]["keybind"]["label"] == "Headlights (Toggle)"
    assert all("keybind" in a for a in acts)
    unknown = [a for a in acts if a["action_name"] not in kb.build_index(DATA)]
    assert unknown and all(a["keybind"] is None for a in unknown)
    h.keybinds = None
    assert all(a["keybind"] is None for a in c.get("/api/actions").json()["actions"])
