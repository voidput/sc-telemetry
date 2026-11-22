"""Ship capabilities from sc-gamedata (synthetic fixtures only, never real game data) + the bridge's
/api/ship, /api/ships, /api/action-states, /api/shield-faces endpoints and snapshot.capabilities."""
import asyncio
import json
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

from sctelemetry import server as srv
from sctelemetry import shipcaps as sc

MINER = {"name": "TEST Miner", "displayName": "TEST Miner", "recordName": "EntityClassDefinition.TEST_Miner",
         "role": "Light Mining", "career": "Industrial",
         "loadout": {"weapons": [{"type": "WeaponMining", "item": "Mining_Laser_X"},
                                 {"type": "WeaponGun", "item": "Gun_X", "count": 2}],
                     "radars": [{"item": "RADR_X"}], "quantumDrives": [{"item": "QDRV_X"}], "turrets": 3}}
GUNSHIP = {"name": "TEST Gunship", "recordName": "EntityClassDefinition.TEST_Gunship", "role": "Heavy Gunship",
           "loadout": {"weapons": [{"type": "WeaponGun", "name": "Salvaged Repeater"}, {"type": "EMP"}],
                       "missileRacks": 2, "quantumEnforcement": [{"item": "QED_X"}], "radars": [{"item": "R"}]}}
SALVAGER = {"name": "TEST Salvager", "recordName": "EntityClassDefinition.TEST_Salvager", "role": "Light Salvage",
            "loadout": {"weapons": [{"type": "TractorBeam"}], "cargoGrids": [{"item": "GRID"}]},
            "totals": {"cargoScu": 12}}
TANKER = {"name": "TEST Tanker", "recordName": "EntityClassDefinition.TEST_Tanker", "role": "Heavy Refueling",
          "loadout": {"cargoGrids": [{"item": "GRID"}]}}
BARE = {"name": "TEST Bare", "recordName": "EntityClassDefinition.TEST_Bare"}  # template without loadout


def dataset(*vehicles, build="9.9.9.1"):
    return {"_build": build, "vehicles": list(vehicles)}


def caps_of(v, sal=frozenset()):
    return sc.derive_caps(v, sal)


def test_class_of():
    assert sc.class_of("EntityClassDefinition.RSI_Perseus") == "RSI_Perseus"
    assert sc.class_of("ARGO_MOLE") == "ARGO_MOLE"
    assert sc.class_of(None) is None and sc.class_of("  ") is None


def test_derive_caps_miner_gunship_salvager_tanker():
    m = caps_of(MINER)
    assert m["mining"] and m["guns"] and m["scan"] and m["quantum"]
    assert not (m["salvage"] or m["refuel"] or m["tractor"] or m["qed"] or m["emp"] or m["missiles"] or m["cargo"])
    assert m["cargo"] is False and m["cargo_scu"] is None
    g = caps_of(GUNSHIP)
    assert g["guns"] and g["emp"] and g["missiles"] and g["qed"] and not g["mining"]
    assert not g["salvage"]  # "Salvaged Repeater" is a gun, not a salvage head
    s = caps_of(SALVAGER)
    assert s["salvage"] and s["tractor"] and s["cargo"] is True and s["cargo_scu"] == 12.0 and not s["guns"]
    t = caps_of(TANKER)
    assert t["refuel"] and t["cargo"] is True and t["cargo_scu"] is None  # grid without SCU total
    b = caps_of(BARE)
    assert set(b) == set(sc.CAPS) | {"cargo_scu"} and not any(b.values())


def test_role_only_mining_and_salvage_dataset_list():
    v = {"recordName": "EntityClassDefinition.TEST_Roc", "role": "Mining", "loadout": {}}
    assert caps_of(v)["mining"]
    w = {"recordName": "EntityClassDefinition.TEST_Hauler", "role": "Freight", "loadout": {}}
    sal = sc.salvage_classes({"vehicles": ["EntityClassDefinition.TEST_Hauler", {"class": "TEST_Other"}]})
    assert sal == {"TEST_Hauler", "TEST_Other"}
    assert caps_of(w, sal)["salvage"]
    assert sc.salvage_classes({"structural": {}}) == set() and sc.salvage_classes(None) == set()


def test_build_table_merges_variants_by_class():
    a = dict(MINER, loadout={"weapons": [{"type": "WeaponGun"}]})
    b = dict(MINER, displayName="TEST Miner Variant", role="Starter", totals={"cargoScu": 4})
    t = sc.build_table(dataset(a, b, GUNSHIP, {"bad": 1}, "junk"))
    assert set(t) == {"TEST_Miner", "TEST_Gunship"}
    e = t["TEST_Miner"]
    assert e["variants"] == 2 and e["display_name"] == "TEST Miner"
    assert e["caps"]["guns"] and e["caps"]["mining"] is True and e["caps"]["cargo"] is True and e["caps"]["cargo_scu"] == 4.0


def write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_candidates_priority_and_override(tmp_path):
    root = tmp_path / "repo"
    c = sc.candidates("PTU", None, root=root)
    assert c == [root / ".tmp/gamedata/ptu/game-ships.json"]  # no home-dir fallback: env/CLI or .tmp only
    assert sc.resolve(c) is None
    write(root / ".tmp/gamedata/ptu/game-ships.json", dataset(GUNSHIP))
    assert sc.resolve(c) == root / ".tmp/gamedata/ptu/game-ships.json"
    d = tmp_path / "custom"
    write(d / "game-ships.json", dataset(TANKER))
    c = sc.candidates("LIVE", d, root=root)
    assert c[0] == d / "game-ships.json" and c[1] == root / ".tmp/gamedata/live/game-ships.json"
    assert sc.resolve(c) == d / "game-ships.json"  # override wins
    assert sc.candidates("LIVE", d / "game-ships.json", root=root)[0] == d / "game-ships.json"  # file form


def test_shipcaps_load_cache_and_mtime_reload(tmp_path, monkeypatch):
    src = write(tmp_path / "data" / "game-ships.json", dataset(MINER, build="1.0.0.1"))
    cache = tmp_path / "cache.json"
    s = sc.ShipCaps([tmp_path / "missing.json", src], cache_path=cache)
    got = s.get("TEST_Miner")
    assert got["caps"]["mining"] and got["source_build"] == "1.0.0.1" and got["source"] == "data/game-ships.json"
    assert s.status()["source"] == "data/game-ships.json" and str(tmp_path) not in json.dumps(s.status())
    assert got["display_name"] == "TEST Miner" and got["role"] == "Light Mining"
    assert s.get("TEST_Unknown") is None and s.get(None) is None
    c = json.loads(cache.read_text(encoding="utf-8"))
    assert c["build"] == "1.0.0.1" and c["key"]["path"] == str(src) and "TEST_Miner" in c["ships"]
    # second instance uses the cache (source unreadable as JSON but same stat key)
    s2 = sc.ShipCaps([src], cache_path=cache)
    assert s2.get("TEST_Miner")["caps"]["mining"]
    # new extract: mtime changes -> reloaded on the next check
    write(src, dataset(GUNSHIP, build="2.0.0.2"))
    os.utime(src, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns + 10**9))
    monkeypatch.setattr(sc, "RECHECK_S", 0.0)
    assert s.get("TEST_Gunship")["source_build"] == "2.0.0.2" and s.get("TEST_Miner") is None


def test_shipcaps_missing_and_broken_sources(tmp_path):
    s = sc.ShipCaps([tmp_path / "nope.json"], cache_path=None)
    assert s.status()["loaded"] is False and "no game-ships.json" in s.status()["error"]
    assert str(tmp_path) not in s.status()["error"]
    bad = tmp_path / "game-ships.json"
    bad.write_text("{half written", encoding="utf-8")
    s = sc.ShipCaps([bad], cache_path=None)
    assert s.get("TEST_Miner") is None and "JSONDecodeError" in s.status()["error"]


def test_salvage_file_next_to_ships(tmp_path):
    d = tmp_path / "data"
    write(d / "game-ships.json", dataset(TANKER))
    write(d / "game-salvage.json", {"ships": ["TEST_Tanker"]})
    assert sc.ShipCaps([d / "game-ships.json"], cache_path=None).get("TEST_Tanker")["caps"]["salvage"]


# ---- bridge endpoints ------------------------------------------------------------------------
@pytest.fixture
def bridge(monkeypatch, tmp_path):
    h = srv.Hub()
    monkeypatch.setattr(srv, "hub", h)
    monkeypatch.setattr(srv.CFG, "host", "127.0.0.1", raising=False)
    monkeypatch.setattr(srv.CFG, "port", 8766, raising=False)
    h.shipcaps = sc.ShipCaps([write(tmp_path / "game-ships.json", dataset(MINER, GUNSHIP))], cache_path=None)
    return h, TestClient(srv.app, base_url="http://127.0.0.1:8766")


def frame(cls):
    return {"vehicle": {"state": {"class_name": cls, "name": cls + "_000000000001"}}}


def test_snapshot_and_api_ship_capabilities(bridge):
    h, c = bridge
    assert h.snapshot()["capabilities"] is None  # no frame yet
    assert c.get("/api/ship").json()["capabilities"] is None
    h.push(frame("TEST_Miner"))
    cap = h.snapshot()["capabilities"]
    assert cap["class"] == "TEST_Miner" and cap["caps"]["mining"] and cap["source_build"] == "9.9.9.1"
    # public snapshot carries a short dataset label, never the absolute path (home dir / username)
    assert cap["source"].endswith("/game-ships.json") and not cap["source"].startswith("/")
    assert str(pathlib.Path.home()) not in json.dumps(h.snapshot()) + c.get("/api/ship").text
    r = c.get("/api/ship").json()
    assert r["class"] == "TEST_Miner" and r["capabilities"] == cap and r["gamedata"]["loaded"]
    h.push(frame("TEST_Unknown"))
    assert h.snapshot()["capabilities"] is None
    assert c.get("/api/ship").json()["class"] == "TEST_Unknown"


def test_api_ships_query(bridge):
    _, c = bridge
    r = c.get("/api/ships", params={"class": "TEST_Gunship"})
    assert r.status_code == 200 and r.json()["capabilities"]["caps"]["emp"]
    r = c.get("/api/ships", params={"class": "NOPE"})
    assert r.status_code == 404 and r.json()["capabilities"] is None
    assert c.get("/api/ships").json()["classes"] == ["TEST_Gunship", "TEST_Miner"]


def test_api_action_states_and_shield_faces(bridge, monkeypatch, tmp_path):
    _, c = bridge
    monkeypatch.setattr(srv, "_data_cache", {})
    r = c.get("/api/action-states")
    assert r.status_code == 200
    m = r.json()
    assert m["version"] == 1 and any(g["id"] == "lights" for g in m["groups"]) and "emote_*" in m["stateless"]
    f = c.get("/api/shield-faces").json()
    assert f["by_count"]["1"]["layout"] == "bubble" and f["by_count"]["4"]["labels"] == ["F1", "F2", "LEFT", "F4"]
    monkeypatch.setattr(srv, "_data_cache", {})
    monkeypatch.setattr(srv, "DATA", tmp_path / "nowhere")
    r = c.get("/api/action-states")
    assert r.status_code == 500 and "action_state_map.json unavailable" in r.json()["message"]


def test_fake_trigger_flips_mock_state(monkeypatch):
    monkeypatch.setattr(srv, "ALLOWLIST_JSON", srv.stubs.ROOT / ".tmp" / "__none__.json")
    monkeypatch.setattr(srv.mock, "STATE", {"lights_enabled": True, "gear_deployed": False, "shield_generators": True})
    ft = srv.FakeTrigger(mutate_mock=True)
    ft.by_name.update({n: {"action_name": n, "holdable": False} for n in
                       ("v_deploy_landing_system", "v_power_set_shields_off")})
    assert ft.trigger("v_lights", None)[0].endswith("_OK") and srv.mock.STATE["lights_enabled"] is False
    ft.trigger("v_deploy_landing_system", None)
    ft.trigger("v_power_set_shields_off", None)
    assert srv.mock.STATE == {"lights_enabled": False, "gear_deployed": True, "shield_generators": False}


def test_fake_fail_switch_forces_failed(monkeypatch):
    monkeypatch.setattr(srv, "ALLOWLIST_JSON", srv.stubs.ROOT / ".tmp" / "__none__.json")
    ft = srv.FakeTrigger(mutate_mock=False)
    ft.by_name.update({n: {"action_name": n, "holdable": False} for n in ("v_open_all_doors", "v_lights")})
    monkeypatch.setattr(srv.app.state, "fake", ft, raising=False)
    monkeypatch.setattr(srv.CFG, "fake_delay", 0.0, raising=False)
    monkeypatch.setattr(srv.CFG, "fake_fail", r"v_open_all_doors|v_lock_.*", raising=False)
    reply = lambda http, status, message, name, hold_ms: (http, status)  # noqa: E731
    run = lambda n: asyncio.run(srv._forward(n, None, reply))  # noqa: E731
    assert run("v_open_all_doors") == (200, "TRIGGER_GAME_ACTION_STATUS_FAILED")
    assert run("v_lights") == (200, "TRIGGER_GAME_ACTION_STATUS_OK")
    monkeypatch.setattr(srv.CFG, "fake_fail", None)
    ft._last.clear()
    assert run("v_open_all_doors")[1].endswith("_OK")


def test_manufacturer_codes_and_info_fields(tmp_path):
    assert sc.manufacturer_of("rsi_2025") == ("rsi", "Roberts Space Industries")
    assert sc.manufacturer_of("zzzz") == ("zzzz", "ZZZZ") and sc.manufacturer_of(None) == (None, None)
    v = dict(GUNSHIP, manufacturer="aegs", size=4, crewSize=5, career="Combat")
    caps = sc.ShipCaps([write(tmp_path / "game-ships.json", dataset(v, BARE))], cache_path=None)
    assert caps.info("TEST_Gunship") == {"display_name": "TEST Gunship", "manufacturer": "Aegis Dynamics",
                                         "manufacturer_code": "aegs", "role": "Heavy Gunship", "career": "Combat",
                                         "size": 4, "crew": 5, "cargo_scu": None}
    assert caps.info("TEST_Bare")["size"] is None and caps.info("NOPE") is None and caps.info(None) is None
