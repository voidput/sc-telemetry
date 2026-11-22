"""Game.log signals (nearby ships, QT, zone, vehicle control, corpse). Synthetic lines only: entity ids
are obviously fake (_0000000000NN), never copied from a real log."""
import json
import re

import pytest

from sctelemetry import gamelog as gl

from .test_gamelog import LINES, write


def ts(sec):
    return f"<2026-01-01T10:{sec // 60:02d}:{sec % 60:02d}.000Z> "


def nav(ent, sec=0, tag="Server Rerouted", auth="NOT AUTH"):
    return (ts(sec) + f"[Notice] <Local Route Guard - {tag}> [ItemNavigation][CL][1] | {auth} | {ent}|"
            "CSCItemNavigation::PostInitialize::<lambda_1>::operator ()|FinalStop=0 [Team_X][QuantumTravel]")


def qt(ent, sec=0):
    return (ts(sec) + "[Notice] <Quantum Drive Arrived - Arrived at Final Destination> [ItemNavigation][CL][1] | "
            f"NOT AUTH | {ent}|CSCItemNavigation::OnQuantumDriveArrived|Quantum Drive has arrived [Team_X]")


def hud(text, sec=0):
    return (ts(sec) + f'[Notice] <SHUDEvent_OnNotification> Added notification "{text}: " [0] to queue. '
            "New queue size: 1, MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] [Team_X]")


def vcf(ent, sec=0):
    return (ts(sec) + "[Notice] <Vehicle Control Flow> CVehicleMovementBase::ClearDriver: Local client node "
            f"[000000000099] releasing control token for '{ent}' [000000000098] [Team_X][Vehicle]")


def corpse(item, sec=0):
    return (ts(sec) + "[Notice] <Adding non kept item [CSCActorCorpseUtils::PopulateItemPortForItemRecoveryEntitlement]> "
            f"Item '{item}_000000000077 - Class({item}) - Context(Streamable Runtime-spawned) - Socpak()', "
            "Recorded data is: Port Name 'Body_ItemPort', KeptId: '000000000076' [Team_X][Unknown]")


FAKE_ID = re.compile(r"0000000000\d\d")


def feed(st, lines, now=1e12):
    evs = []
    for line in lines:
        ev = gl.parse_signal(line)
        if ev:
            evs += st.apply(ev)
    return evs + st.drain(now)


def leaks(obj):
    return FAKE_ID.findall(json.dumps(obj))


# ---- pure parsers ------------------------------------------------------------------------------


def test_parse_nearby_and_variants():
    ev = gl.parse_signal(nav("DRAK_Cutlass_Black_000000000011") + "\r\n")
    assert ev == {"kind": "nearby", "ts": "2026-01-01T10:00:00.000Z", "class": "DRAK_Cutlass_Black",
                  "eid": "000000000011"}
    assert gl.parse_signal(nav("RSI_Perseus_000000000012[000000000012]", tag="Server Reset Blocked"))["class"] == "RSI_Perseus"
    assert gl.parse_signal(nav("ARGO_RAFT_000000000013", auth="NOT INIT"))["class"] == "ARGO_RAFT"
    assert gl.parse_signal(nav("KRIG_L22_AlphaWolf_000000000014"))["class"] == "KRIG_L22_AlphaWolf"


@pytest.mark.parametrize("line", [
    "", "garbage",
    ts(0) + "[Notice] <Local Route Guard - Server Rerouted> [CL][1] | NULL ENTITY|CSCItemNavigation::PostInitialize|x",
    ts(0) + "[Notice] <Local Route Guard - Server Rerouted> [CL][1] | NOT AUTH | NoIdHere|CSCItemNavigation::PostInitialize|x",
    ts(0) + "[Notice] <Adding Instanced Destinations To Panel> x",
    LINES[7],  # session lines are not signals
])
def test_parse_signal_ignores(line):
    assert gl.parse_signal(line) is None


def test_parse_qt_vcf_hud_corpse():
    q = gl.parse_signal(qt("ANVL_Lightning_F8C_Exec_000000000021[000000000021]"))
    assert q["kind"] == "qt_arrived" and q["class"] == "ANVL_Lightning_F8C_Exec" and q["eid"] == "000000000021"
    v = gl.parse_signal(vcf("RSI_Perseus_000000000001"))
    assert v["kind"] == "vehicle_released" and v["class"] == "RSI_Perseus" and v["eid"] == "000000000001"
    h = gl.parse_signal(hud("Entered Monitored Space"))
    assert h == {"kind": "notification", "ts": "2026-01-01T10:00:00.000Z", "text": "Entered Monitored Space: "}
    c = gl.parse_signal(corpse("rsi_undersuit_01"))
    assert c == {"kind": "corpse_item", "ts": "2026-01-01T10:00:00.000Z"}  # no item names / ids kept


@pytest.mark.parametrize("text,kind,arm,jur", [
    ("Entering Armistice Zone - Combat Prohibited: ", "armistice", True, None),
    ("Leaving Armistice Zone - Caution Advised: ", "other", False, None),
    ("Entered Monitored Space: ", "monitored", None, None),
    ("Entered Ungoverned Jurisdiction: ", "ungoverned", None, "Ungoverned"),
    ("Entered People's Alliance Jurisdiction: ", "jurisdiction", None, "People's Alliance"),
])
def test_classify_zone(text, kind, arm, jur):
    z = gl.classify_zone(text)
    assert z["kind"] == kind and z["armistice"] == arm and z["jurisdiction"] == jur and not z["text"].endswith(":")


@pytest.mark.parametrize("text", ["Hangar Request Completed: ", "Quantum Travel Calibration Started By : ",
                                  "Minor Injury Detected - Torso - Tier 3 Treatment Required : ", ""])
def test_classify_zone_ignores_other_notices(text):
    assert gl.classify_zone(text) is None


def test_display_class():
    assert gl.display_class("RSI_Constellation_Mk5_Centaurus") == "RSI Constellation Mk5 Centaurus"
    assert gl.split_entity("ANVL_Hornet_F7CS_Mk2_000000000031") == ("ANVL_Hornet_F7CS_Mk2", "000000000031")
    assert gl.split_entity("Mock Gladius") is None and gl.split_entity(None) is None


# ---- state -------------------------------------------------------------------------------------


def test_nearby_dedupe_counts_and_times():
    st = gl.SignalsState()
    evs = feed(st, [nav("DRAK_Corsair_000000000011", 0), nav("DRAK_Corsair_000000000011", 30),  # same entity
                    nav("DRAK_Corsair_000000000012", 10), nav("GAMA_Hyun_000000000013", 20)])
    p = st.public()
    assert p["nearby_count"] == 3 and p["nearby_classes"] == 2
    cor = next(n for n in p["nearby"] if n["class"] == "DRAK_Corsair")
    assert cor["count"] == 2 and cor["display"] == "DRAK Corsair"
    assert cor["last_seen"] - cor["first_seen"] == 30
    assert p["nearby"][0]["class"] == "DRAK_Corsair"  # most recently seen first
    nb = [e for e in evs if e["type"] == "NEARBY_SHIP"]
    assert len(nb) == 1 and nb[0]["count"] == 3 and nb[0]["detail"].startswith("3 new ships")
    assert not leaks(p) and not leaks(evs)


def test_nearby_first_sighting_only():
    st = gl.SignalsState()
    assert [e["type"] for e in feed(st, [nav("GAMA_Hyun_000000000013", 0)], now=100)] == ["NEARBY_SHIP"]
    assert feed(st, [nav("GAMA_Hyun_000000000013", 5)], now=200) == []  # re-seen: no new event
    one = feed(st, [nav("AEGS_Redeemer_000000000014", 6)], now=300)
    assert one[0]["detail"] == "AEGS Redeemer" and one[0]["class"] == "AEGS_Redeemer" and one[0]["count"] == 1


def test_nearby_burst_rate_limited():
    st = gl.SignalsState()
    first = feed(st, [nav("GAMA_Hyun_000000000013", 0)], now=100.0)
    assert len(first) == 1
    # more sightings 0.4 s later (wall): held back, then aggregated into one event
    assert feed(st, [nav("AEGS_Redeemer_000000000014", 1), nav("RSI_Galaxy_000000000015", 2)], now=100.4) == []
    assert st.drain(100.7) == []
    later = st.drain(101.0)
    assert len(later) == 1 and later[0]["count"] == 2 and later[0]["detail"] == "2 new ships: AEGS Redeemer, RSI Galaxy"
    assert later[0]["classes"] == ["AEGS_Redeemer", "RSI_Galaxy"]


def test_own_ship_excluded_and_flagged():
    st = gl.SignalsState()
    feed(st, [nav("RSI_Perseus_000000000001", 0), nav("RSI_Perseus_000000000002", 1)])
    assert st.public()["nearby_count"] == 2
    evs = feed(st, [vcf("RSI_Perseus_000000000001", 2), nav("RSI_Perseus_000000000001", 3)])
    p = st.public()
    assert p["nearby_count"] == 1 and p["nearby"][0]["count"] == 1  # the other Perseus stays
    assert p["own_ship"] == {"class": "RSI_Perseus", "display": "RSI Perseus", "source": "game_log"}
    assert [e["type"] for e in evs] == ["VEHICLE_RELEASED"] and evs[0]["class"] == "RSI_Perseus"
    # switching ships: the earlier one is still yours, never "nearby"
    feed(st, [vcf("MISC_Prospector_000000000003", 4), nav("RSI_Perseus_000000000001", 5)])
    assert st.public()["nearby_count"] == 1 and st.public()["own_ship"]["class"] == "MISC_Prospector"
    assert not leaks(st.public()) and not leaks(evs)


def test_own_ship_from_telemetry():
    st = gl.SignalsState()
    feed(st, [nav("AEGS_Gladius_000000000005", 0), nav("AEGS_Gladius_000000000006", 1)])
    st.set_telemetry_vehicle("AEGS_Gladius_000000000005", "AEGS_Gladius")
    p = st.public()
    assert p["nearby_count"] == 1 and p["own_ship"]["source"] == "telemetry"
    v = st.version
    st.set_telemetry_vehicle("AEGS_Gladius_000000000005", "AEGS_Gladius")  # per-frame repeat: no-op
    st.set_telemetry_vehicle("", "")                                       # on foot: keeps the last ship
    assert st.version == v and st.public()["own_ship"]["class"] == "AEGS_Gladius"
    st.set_telemetry_vehicle("Mock Gladius", "AEGS_Gladius")  # no id in name: class only
    assert st.public()["own_ship"]["class"] == "AEGS_Gladius" and st.public()["nearby_count"] == 1


def test_qt_arrived_self_and_other():
    st = gl.SignalsState()
    feed(st, [vcf("RSI_Perseus_000000000001", 0)])
    evs = feed(st, [qt("RSI_Perseus_000000000001[000000000001]", 1), qt("ANVL_Gladiator_000000000041", 2)])
    q = [e for e in evs if e["type"] == "QT_ARRIVED"]
    assert [(e["class"], e["self"]) for e in q] == [("RSI_Perseus", True), ("ANVL_Gladiator", False)]
    assert st.public()["nearby_count"] == 1  # the other arrival counts as a sighting, yours does not
    assert not leaks(evs)


def test_zone_state_and_events():
    st = gl.SignalsState()
    evs = feed(st, [hud("Entered Monitored Space", 0), hud("Entering Armistice Zone - Combat Prohibited", 1),
                    hud("Entering Armistice Zone - Combat Prohibited", 2),  # repeat: no event, since kept
                    hud("Hangar Request Completed", 3)])
    p = st.public()
    assert p["armistice"] and p["zone"]["kind"] == "armistice" and p["zone"]["since"] == gl.parse_ts("2026-01-01T10:00:01.000Z")
    assert [(e["type"], e["kind"]) for e in evs] == [("ZONE", "monitored"), ("ZONE", "armistice")]
    feed(st, [hud("Entered People's Alliance Jurisdiction", 4)])
    assert st.public()["armistice"] and st.public()["jurisdiction"] == "People's Alliance"  # still inside
    feed(st, [hud("Leaving Armistice Zone - Caution Advised", 5)])
    p = st.public()
    assert not p["armistice"] and p["zone"]["kind"] == "other" and p["jurisdiction"] == "People's Alliance"


def test_corpse_items_aggregated_without_names():
    st = gl.SignalsState()
    lines = [corpse("body_01", 0), corpse("rsi_undersuit_01", 0), corpse("gmni_sniper_01", 1)]
    evs = []
    for line in lines:
        evs += st.apply(gl.parse_signal(line))
    assert evs == [] and st.drain(1) == []  # lines arrived this poll: wait one more poll
    out = st.drain(2)
    assert out[0]["type"] == "CORPSE_ITEMS" and out[0]["count"] == 3
    assert "undersuit" not in json.dumps(out) and not leaks(out)
    # a second death much later is its own list
    evs = st.apply(gl.parse_signal(corpse("body_01", 0))) + st.apply(gl.parse_signal(corpse("body_01", 600)))
    assert [e["count"] for e in evs] == [1]


def test_nearby_list_capped_and_entity_cap(monkeypatch):
    monkeypatch.setattr(gl, "ENTITY_CAP", 70)
    st = gl.SignalsState()
    feed(st, [nav(f"TEST_Ship{i:03d}_0000000{i:05d}", i) for i in range(80)])
    p = st.public()
    assert len(p["nearby"]) == gl.NEARBY_CAP and p["nearby"][0]["class"] == "TEST_Ship079"
    assert p["nearby_count"] == 70  # oldest-seen entities evicted


def test_reset_clears():
    st = gl.SignalsState()
    feed(st, [nav("GAMA_Hyun_000000000013", 0), hud("Entering Armistice Zone - Combat Prohibited", 1),
              vcf("RSI_Perseus_000000000001", 2)])
    st.reset()
    assert st.public() == {"zone": None, "armistice": False, "jurisdiction": None, "nearby": [], "nearby_count": 0,
                           "nearby_classes": 0, "own_ship": None}


def test_mock_signals():
    st, evs = gl.mock_signals()
    p = st.public()
    assert p["armistice"] and p["nearby_count"] >= 3 and p["own_ship"]["class"] == "AEGS_Gladius"
    assert {"ZONE", "NEARBY_SHIP", "QT_ARRIVED"} <= {e["type"] for e in evs}
    assert not leaks(p) and not leaks(evs)


# ---- tailer integration ------------------------------------------------------------------------

SIG = [nav("DRAK_Corsair_000000000011", 130), hud("Entered Monitored Space", 131), qt("GAMA_Hyun_000000000012", 132)]


def test_tailer_feeds_signals(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES + SIG)
    t = gl.GameLogTailer(p)
    evs = t.step()
    types = [e["type"] for e in evs]
    assert "ZONE" in types and "QT_ARRIVED" in types and types.count("NEARBY_SHIP") == 1
    assert t.signals.public()["nearby_count"] == 2 and t.signals.public()["zone"]["kind"] == "monitored"


def test_tailer_signals_reset_on_rotation_and_leave(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES + SIG)
    t = gl.GameLogTailer(p)
    t.step()
    # back to the menu: SESSION_LEAVE -> ships/zone of that shard are gone
    write(p, ["<2026-01-01T10:30:00.000Z> Loading screen for Frontend_Main : X closed after 1.0 seconds"], mode="a")
    t.step()
    assert t.signals.public()["nearby_count"] == 0 and t.signals.public()["zone"] is None
    # rejoin + ships, then the game relaunches (new file): reset again
    write(p, [LINES[7]] + SIG, mode="a")
    t.step()
    assert t.signals.public()["nearby_count"] == 2
    p.rename(tmp_path / "old.log")
    write(p, ["<2026-01-01T11:00:00.000Z> Log started on X"])
    t.step()
    assert t.rotations == 1 and t.signals.public()["nearby_count"] == 0


def test_tailer_join_resets_signals(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES + SIG)
    t = gl.GameLogTailer(p)
    t.step()
    write(p, [LINES[7].replace("10:00:20", "10:40:00").replace("ptu_use1b_1_020", "ptu_use1b_1_021")], mode="a")
    t.step()
    assert t.signals.public()["nearby_count"] == 0


def test_tailer_notifies_on_signal_change_only(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES)
    calls = []
    t = gl.GameLogTailer(p, on_change=lambda s, e: calls.append(e))
    t.step()
    n = len(calls)
    t.step()
    assert len(calls) == n
    write(p, [nav("DRAK_Corsair_000000000011", 130)], mode="a")
    t.step()
    assert len(calls) == n + 1 and calls[-1][0]["type"] == "NEARBY_SHIP"


def test_telemetry_ship_switch_keeps_old_ship_own():
    st = gl.SignalsState()
    st.set_telemetry_vehicle("RSI_Perseus_000000000001", "RSI_Perseus")
    st.set_telemetry_vehicle("MISC_Prospector_000000000002", "MISC_Prospector")
    feed(st, [nav("RSI_Perseus_000000000001", 0)])  # ship A again after telemetry moved to B
    p = st.public()
    assert p["nearby_count"] == 0 and p["own_ship"]["class"] == "MISC_Prospector"


def test_own_ship_most_recent_source_wins():
    st = gl.SignalsState()
    st.set_telemetry_vehicle("RSI_Perseus_000000000001", "RSI_Perseus")
    feed(st, [vcf("MISC_Prospector_000000000002", 0)])  # later seat release beats sticky telemetry
    assert st.public()["own_ship"] == {"class": "MISC_Prospector", "display": "MISC Prospector", "source": "game_log"}
    st.set_telemetry_vehicle("AEGS_Gladius_000000000003", "AEGS_Gladius")  # telemetry changes again: it wins
    assert st.public()["own_ship"]["source"] == "telemetry" and st.public()["own_ship"]["class"] == "AEGS_Gladius"
    assert not leaks(st.public())


def test_corpse_items_without_timestamp_one_burst():
    st = gl.SignalsState()
    lines = [corpse(f"item_{i:02d}", 0).split("[Notice] ", 1)[1] for i in range(5)]  # no <ts> prefix
    evs = []
    for line in lines:
        evs += st.apply(gl.parse_signal(line))
    assert evs == []
    st.drain(1)
    out = st.drain(2)
    assert [e["count"] for e in out] == [5] and out[0]["t"] is None
