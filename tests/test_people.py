"""People (Game.log handles) + opt-in RSI org lookup. Synthetic lines and pages only: handles are obviously
fake (TestPilot_*), ids are _0000000000NN, HTML fixtures are hand-written in the public page's layout. No test
touches the network: every OrgLookup gets a fake fetcher."""
import asyncio
import json
import os
import pathlib
import stat

import pytest
from fastapi.testclient import TestClient

from sctelemetry import gamelog as gl
from sctelemetry import people as P
from sctelemetry import server as srv

from .test_gamelog import write

FIX = pathlib.Path(__file__).parent / "fixtures" / "rsi"
SELF = "TestPilot_Self"
TAIL = ': " [7] to queue. New queue size: 1, MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] [Team_X]'


def ts(sec):
    return f"<2026-01-01T10:{sec // 60:02d}:{sec % 60:02d}.000Z> "


def note(title, *body, sec=0):
    """A HUD toast as the game logs it: one physical line, or title + continuation line(s)."""
    head = ts(sec) + '[Notice] <SHUDEvent_OnNotification> Added notification "' + title
    if not body:
        return [head + TAIL]
    return [head] + [ts(sec) + b for b in body[:-1]] + [ts(sec) + body[-1] + TAIL]


def login(handle=SELF, sec=0):
    return ts(sec) + f"[Notice] <Legacy login response> [CIG-net] User Login Success - Handle[{handle}] - Time[1] [Team_X]"


def destroyed(killer, kid, owner, cls="AEGS_Gladius", sec=0, reason="Combat"):
    return (ts(sec) + f"[Notice] <Vehicle Destroyed> Vehicle '{cls}_000000000031' [000000000031] class '{cls}' "
            f"(owner player [{owner}], driver [0]) destroyed: reason [{reason}] level [0] -> [2] flags [4066] "
            f"killer '{killer}' [{kid}] authority [0] [Team_CGP4][Vehicle]")


def feed(lines, st=None):
    st = st or P.PeopleState()
    for ln in lines:
        st.feed(ln)
    return st


def names(st, rel=None):
    return sorted(p["name"] for p in st.people.values() if rel is None or p["relations"][0] == rel)


# ---- parsing -------------------------------------------------------------------------------------------

def test_party_toasts_multi_line_and_relations():
    st = feed([login(),
               *note("New Member Joined", "TestPilot_B has joined the party."),
               *note("New Party Leader", "TestPilot_B is now party leader."),
               *note("Party", "TestPilot_E connected."),
               *note("You have joined", "TestPilot_Lead"),
               *note("Initiated by TestPilot_L2", "Party Launching: Join Party In PU?"),
               *note("Party Launch", "Initiated by party leader TestPilot_L3."),
               *note("Party", "TestPilot_Q has started to QT to your current location."),
               ts(1) + "[Notice] <party-launch> [notification] party-launch from leader[TestPilot_L4] : pendingId[0] [Team_X]"])
    assert names(st, "party") == ["TestPilot_B", "TestPilot_E", "TestPilot_L2", "TestPilot_L3", "TestPilot_L4",
                                  "TestPilot_Lead", "TestPilot_Q"]
    b = st.people["testpilot_b"]
    assert b["leader"] and not b["left"] and b["count"] == 2 and b["sources"] == ["party_toast"]
    assert st.people["testpilot_q"]["sources"] == ["qt_incoming"]


def test_member_left_name_with_spaces_is_shown_but_never_linked():
    st = feed(note("Member Left", "Test Pilot Spaced has left the party."))
    row = st.public(names=True)["people"][0]
    assert row["name"] == "Test Pilot Spaced" and row["left"] and not row["valid_handle"] and row["url"] is None


def test_voice_channel_owner_and_members_are_crew():
    st = feed([login(),
               *note("You have joined channel 'Drake Cutlass Black : TestPilot_Owner'.", ""),
               *note("New Member Joined", "TestPilot_M has joined the channel 'Drake Cutlass Black : TestPilot_Owner'."),
               *note("Member Left", "TestPilot_N has left the channel 'RSI Perseus : TestPilot_Self'.")])
    assert names(st, "crew") == ["TestPilot_M", "TestPilot_N", "TestPilot_Owner"]  # own channel: owner = self, skipped
    assert st.people["testpilot_owner"]["ship"] == "Drake Cutlass Black" and st.people["testpilot_n"]["left"]


def test_own_voice_channel_join_body_line_starts_with_colon():
    # the real continuation of an empty-body toast is `<ts> : " [n] to queue...`
    lines = [ts(0) + "[Notice] <SHUDEvent_OnNotification> Added notification \"You have joined channel 'RSI Meteor : TestPilot_Own2'.",
             ts(0) + TAIL.lstrip()]
    assert names(feed(lines), "crew") == ["TestPilot_Own2"]


def test_qt_calibration_party_likely_skips_empty_npc_and_self():
    st = feed([login(),
               *note("Quantum Travel Calibration Started By TestPilot_C"),
               *note("Quantum Travel Calibration Complete By TestPilot_C"),
               *note("Quantum Travel Calibration Started By "),
               *note("Quantum Travel Calibration Started By PU_Human-NineTails-Pilot-Male-Light_01_000000000123"),
               *note("Quantum Travel Calibration Started By TestPilot_Self")])
    assert names(st) == ["TestPilot_C"] and st.people["testpilot_c"]["relations"] == ["party_likely"]
    assert st.people["testpilot_c"]["count"] == 2


def test_vehicle_destroyed_killer_victim_map_npc_and_self():
    st = feed([login(),
               ts(0) + "[Notice] <AccountLoginCharacterStatus_Character> Character: createdAt 1 - updatedAt 2 - geid "
                       "000000000099 - accountId 000000000099 - name TestPilot_Self - state STATE_CURRENT [Team_X]",
               destroyed("TestPilot_K", "000000000050", "0"),                      # AI victim, player killer
               destroyed("PU_Pilots-Human-Criminal-Fighter_000000000060", "000000000060", "000000000050"),  # K's ship lost to NPC
               destroyed("TestPilot_Self", "000000000099", "000000000051"),        # you killed an unknown id
               destroyed("TestPilot_V", "000000000051", "000000000051", reason="SelfDestruct"),
               destroyed("none", "0", "000000000099"),                             # your ship, no killer
               destroyed("Citizen-12345", "000000000061", "0")])
    assert names(st) == ["TestPilot_K", "TestPilot_V"] and names(st, "nearby") == names(st)
    assert st.people["testpilot_k"]["count"] == 2   # as killer, then as victim via the id map
    assert "000000000099" in st.self_ids


def test_self_aliases_and_geid_never_listed():
    st = feed([*note("New Member Joined", "TestPilot_Alt has joined the party."),
               ts(1) + "[Notice] <CLoginSession> [CompleteAuthentication] {success} Fully authenticated : handle[TestPilot_Alt] hub[1] in 1s",
               login("TestPilot_Self", 2),
               *note("New Member Joined", "TestPilot_Self has joined the party.", sec=3),
               ts(4) + "[Notice] <CEntityComponentFrontendPUUIProvider::AddAccessibleDestinations> [3] destinations received for player [TestPilot_Third]"])
    assert names(st) == [] and st.self_handles == {"testpilot_alt", "testpilot_self", "testpilot_third"}
    assert st.public(names=True)["self"]["name"] == "TestPilot_Alt" and st.public(names=True)["self"]["label"] == "you"


def test_self_only_lines_never_add_people():
    st = feed([login(),
               ts(0) + "[Notice] <CommsNotifications> SendCommsNotification +Missions.X - [00000000-0000-0000-0000-000000000000] - Mission: [00000000-0000-0000-0000-000000000000], Player: TestPilot_Other[000000000099]",
               ts(0) + "[Notice] <SHUDEvent_OnCommsNotification> TestPilot_Other[000000000099] - CSCLocalPlayerNotifications::HandleComponentEvent - Adding",
               ts(0) + "[Notice] <CEntityComponentShipListProvider::SetVehicleSpawningInformations> SetVehicleSpawningInformations - VehicleEntityId: [1], LandingArea: TestPilot_Other's Hangar",
               ts(0) + "[Error] <Too many actions> Attempting to queue too many actions (11) on entity 'TestPilot_Other'. Can't remove anything [Team_ActorTech][Mannequin]"])
    assert names(st) == []


def test_toast_joiner_survives_poll_boundary_and_abandons_on_tagged_line():
    st = P.PeopleState()
    a, b = note("New Member Joined", "TestPilot_B has joined the party.")
    st.feed(a)            # poll ends here: the toast stays buffered
    assert names(st) == []
    st.feed(b)
    assert names(st) == ["TestPilot_B"]
    a, _ = note("New Member Joined", "TestPilot_X has joined the party.")
    st.feed(a)
    st.feed(ts(0) + "[Notice] <SomethingElse> unrelated")   # tagged line: the open toast is dropped
    st.feed(ts(0) + 'TestPilot_X has joined the party.' + TAIL)
    st.feed(ts(0) + '   "New Member Joined')                     # queue dump line: never a start/continuation
    assert names(st) == ["TestPilot_B"]


def test_toast_hits_pure_rules():
    assert P.toast_hits("Quantum Travel Calibration Started By TestPilot_A") == [
        ("party_likely", "TestPilot_A", "qt_calibration", {"phase": "started"})]
    assert P.toast_hits("Entered Armistice Zone - Combat Prohibited") == []
    assert P.toast_hits("Invitation Declined\n") == []


def test_is_npc_and_clean_name():
    for n in ("", "none", "PU_Human-X", "Citizen-42", "Ship_000000000123", "AI_Turret"):
        assert P.is_npc(n), n
    for n in ("TestPilot_A", "ABCD_Pilot", "x-y_z"):   # underscores / prefixes are real handles
        assert not P.is_npc(n), n
    assert P.clean_name("bad\x07name") is None and P.clean_name("x" * 65) is None and P.clean_name("ok'q") is None


def test_clear_nearby_keeps_party_and_crew_reset_forgets_all():
    st = feed([login(), destroyed("TestPilot_K", "000000000050", "0"),
               *note("New Member Joined", "TestPilot_B has joined the party.")])
    st.clear_nearby()
    assert names(st) == ["TestPilot_B"]
    st.reset()
    assert names(st) == [] and not st.self_handles


def test_public_without_names_is_counts_only():
    st = P.mock_people()
    pub = st.public(names=False)
    blob = json.dumps(pub)
    assert pub["people"] is None and pub["self"] is None and "TestPilot" not in blob
    assert pub["counts"] == {"crew": 1, "party": 1, "party_likely": 1, "nearby": 1, "total": 4, "self_known": True}
    assert P.mock_people().handles()[0] == SELF   # lookup order: self, then strongest relation


def test_people_cap_evicts_nearby_first():
    st = feed([*note("New Member Joined", "TestPilot_Keep has joined the party.")]
              + [destroyed(f"TestPilot_N{i}", f"{100000000 + i}", "0", sec=i % 60) for i in range(P.PEOPLE_CAP + 5)])
    assert len(st.people) == P.PEOPLE_CAP and "testpilot_keep" in st.people


def test_tailer_feeds_people_and_resets_on_new_log(tmp_path):
    log = tmp_path / "Game.log"
    write(log, [login(), *note("New Member Joined", "TestPilot_B has joined the party.")])
    t = gl.GameLogTailer(log)
    t.step()
    assert names(t.people) == ["TestPilot_B"]
    log.write_text("<2026-01-02T00:00:00.000Z> Log started on Fri Jan  2 00:00:00 2026\n", encoding="utf-8")
    t.step()
    assert names(t.people) == [] and not t.people.self_handles


# ---- RSI page parsing ----------------------------------------------------------------------------------

def test_parse_citizen_fixtures():
    a = P.parse_citizen((FIX / "citizen_org.html").read_text(encoding="utf-8"))
    assert a == {"handle": "TestPilot_A", "moniker": "Test & Pilot A", "enlisted": "Mar 3, 2950", "no_main_org": False,
                 "org": {"name": "Synthetic Test Org", "sid": "TSTORG", "rank": "Officer", "visibility": "public"}}
    b = P.parse_citizen((FIX / "citizen_noorg.html").read_text(encoding="utf-8"))
    assert b["handle"] == "TestPilot_B" and b["org"] is None and b["no_main_org"] and b["enlisted"] == "Jan 1, 2951"
    c = P.parse_citizen((FIX / "citizen_redacted.html").read_text(encoding="utf-8"))
    assert c["org"] == {"name": None, "sid": None, "rank": None, "visibility": "redacted"}
    assert P.parse_citizen((FIX / "not_a_profile.html").read_text(encoding="utf-8")) is None
    assert P.parse_citizen("") is None and P.parse_citizen("<html><p class='entry'><b>") is None


def test_parse_affiliations_and_synthetic_generator_roundtrip():
    assert P.parse_affiliations((FIX / "orgs_two_affiliations.html").read_text(encoding="utf-8")) == 2
    assert P.parse_affiliations(P.synthetic_orgs_html(0, main=False)) == 0
    assert P.parse_affiliations((FIX / "not_a_profile.html").read_text(encoding="utf-8")) is None
    page = P.synthetic_citizen_html("TestPilot_Z", org={"name": "Zed Org", "sid": "ZED", "rank": "Founder"})
    assert P.parse_citizen(page)["org"] == {"name": "Zed Org", "sid": "ZED", "rank": "Founder", "visibility": "public"}


# ---- OrgLookup (fake fetcher, fake clock) --------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s
        await asyncio.sleep(0)   # yield like a real sleep


def recorder(pages):
    calls = []

    def fetch(url, timeout):
        calls.append((url, timeout))
        for k, v in pages.items():
            if url.endswith(k):
                return v
        return 500, ""
    return fetch, calls


def run(coro):
    return asyncio.run(coro)


def test_lookup_ok_rate_limited_cached_and_file_private(tmp_path):
    clk = Clock()
    fetch, calls = recorder({"/TestPilot_A": (200, (FIX / "citizen_org.html").read_text(encoding="utf-8")),
                             "/TestPilot_A/organizations": (200, (FIX / "orgs_two_affiliations.html").read_text(encoding="utf-8"))})
    cache = tmp_path / "rsi-cache.json"
    lk = P.OrgLookup(cache, fetch=fetch, enabled=True, clock=clk, sleep=clk.sleep)
    assert run(lk.lookup_one("TestPilot_A")) == "ok"
    assert [u for u, _ in calls] == ["https://robertsspaceindustries.com/citizens/TestPilot_A",
                                     "https://robertsspaceindustries.com/citizens/TestPilot_A/organizations"]
    assert all(t == P.TIMEOUT_S for _, t in calls) and clk.slept == [P.MIN_INTERVAL_S]  # >= 3 s apart
    g = lk.get("testpilot_a")
    assert g["status"] == "ok" and g["org"]["sid"] == "TSTORG" and g["affiliations"] == 2 and g["valid"]
    if os.name == "posix":  # Windows: mode bits are not a privacy control (profile ACL is)
        assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    lk2 = P.OrgLookup(cache, fetch=fetch, enabled=True, clock=clk, sleep=clk.sleep)  # reloads from disk
    assert lk2.get("TestPilot_A")["org"]["name"] == "Synthetic Test Org"
    assert lk2.next_handle(["TestPilot_A"]) is None
    clk.t += P.TTL_S + 1
    assert lk2.next_handle(["TestPilot_A"]) == "TestPilot_A"   # 7-day TTL expired
    assert lk2.clear() == 1 and not cache.exists() and lk2.get("TestPilot_A")["status"] == "pending"


def test_lookup_404_negative_cache_one_day(tmp_path):
    clk = Clock()
    fetch, calls = recorder({"/TestPilot_Gone": (404, "")})
    lk = P.OrgLookup(None, fetch=fetch, enabled=True, clock=clk, sleep=clk.sleep)
    assert run(lk.lookup_one("TestPilot_Gone")) == "not_found" and len(calls) == 1
    assert lk.get("TestPilot_Gone")["status"] == "not_found"
    clk.t += P.NEG_TTL_S - 10
    assert lk.next_handle(["TestPilot_Gone"]) is None
    clk.t += 20
    assert lk.next_handle(["TestPilot_Gone"]) == "TestPilot_Gone"


def test_lookup_blocked_backs_off_and_unrecognised_page_is_error(tmp_path):
    clk = Clock()
    fetch, _ = recorder({"/TestPilot_A": (429, ""), "/TestPilot_B": (200, (FIX / "not_a_profile.html").read_text(encoding="utf-8"))})
    lk = P.OrgLookup(None, fetch=fetch, enabled=True, clock=clk, sleep=clk.sleep)
    assert run(lk.lookup_one("TestPilot_A")) == "blocked"
    st = lk.status()
    assert st["blocked_for_s"] == 60 and st["last_status"] == "blocked (HTTP 429)"
    assert run(lk.lookup_one("TestPilot_B")) == "error"
    assert lk.get("TestPilot_B") == {"status": "error"} and "not recognised" in lk.status()["last_status"]
    assert lk.next_handle(["TestPilot_B"]) is None   # retried after ERROR_RETRY_S, not in a loop


def test_lookup_disabled_never_fetches(tmp_path):
    clk = Clock()
    fetch, calls = recorder({})
    lk = P.OrgLookup(None, fetch=fetch, enabled=False, clock=clk, sleep=clk.sleep)

    async def go():
        task = asyncio.ensure_future(lk.run(lambda: ["TestPilot_A"], poll_s=0.01))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
    run(go())
    assert calls == [] and lk.get("TestPilot_A") is None
    lk.enabled = True
    lk.last_request = clk.t   # a request just went out; then the toggle goes off during the wait
    lk.enabled = False
    assert run(lk.lookup_one("TestPilot_A")) == "disabled" and calls == []


def test_fake_fetch_variants():
    st, page = P.fake_fetch(P.citizen_url("TestPilot_A"))
    assert st == 200 and P.parse_citizen(page)["org"]["sid"] == "TSTA"
    assert P.fake_fetch(P.citizen_url("GhostPilot"))[0] == 404
    assert P.fake_fetch(P.citizen_url("BlockedPilot"))[0] == 429
    assert P.parse_citizen(P.fake_fetch(P.citizen_url("NoOrgPilot"))[1])["org"] is None
    assert P.parse_citizen(P.fake_fetch(P.citizen_url("RedactedPilot"))[1])["org"]["visibility"] == "redacted"


def test_user_agent_has_no_personal_data():
    from sctelemetry import __version__
    assert P.USER_AGENT == f"sc-telemetry/{__version__} (local telemetry dashboard)"
    assert __version__ != "0.0.0+unknown"  # installed metadata is found under the new dist name


# ---- bridge API / snapshot privacy --------------------------------------------------------------------

@pytest.fixture
def bridge(monkeypatch):
    h = srv.Hub()
    monkeypatch.setattr(srv, "hub", h)
    monkeypatch.setattr(srv.CFG, "host", "127.0.0.1", raising=False)
    monkeypatch.setattr(srv.CFG, "port", 8766, raising=False)
    h.people = P.mock_people()
    h.orgs = P.OrgLookup(None, fetch=P.fake_fetch)
    return h, TestClient(srv.app, base_url="http://127.0.0.1:8766")


def test_snapshot_names_only_when_on_and_same_origin(bridge):
    h, c = bridge
    assert "TestPilot" not in json.dumps(h.snapshot(names_ok=True))   # Show names off
    h.people_settings["show_names"] = True
    assert "TestPilot" not in json.dumps(h.snapshot(names_ok=False))  # foreign origin
    assert h.snapshot(names_ok=False)["people"]["names_blocked"]
    snap = h.snapshot(names_ok=True)
    assert snap["people"]["self"]["name"] == SELF and len(snap["people"]["people"]) == 4
    assert "TestPilot" in c.get("/api/snapshot").text
    assert "TestPilot" not in c.get("/api/snapshot", headers={"Origin": "http://evil.example"}).text
    assert "TestPilot" not in c.get("/api/snapshot", headers={"Host": "evil.example:8766"}).text


def test_api_people_guard_settings_and_cache(bridge):
    h, c = bridge
    assert c.get("/api/people", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/people", headers={"Host": "rebind.example:8766"}).status_code == 403
    r = c.get("/api/people").json()
    assert r["people"]["people"] is None and r["settings"] == {"show_names": False, "lookup_orgs": False}
    put = lambda body, **kw: c.put("/api/people/settings", json=body, **kw)  # noqa: E731
    assert put({"show_names": True}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert put({"show_names": "yes"}).status_code == 400 and put({"other": True}).status_code == 400
    assert put({}).status_code == 400
    assert c.put("/api/people/settings", content=b'{"show_names": true}').status_code == 415
    r = put({"show_names": True, "lookup_orgs": True})
    assert r.status_code == 200 and r.json()["settings"] == {"show_names": True, "lookup_orgs": True}
    assert h.orgs.enabled and h.lookup_handles()[0] == SELF
    assert c.get("/api/people").json()["people"]["people"][0]["org"] == {"status": "pending"}
    run(h.orgs.lookup_one("TestPilot_B"))
    assert c.get("/api/people").json()["people"]["people"][1]["org"]["org"]["sid"] == "TSTB"
    assert c.delete("/api/people/cache", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.delete("/api/people/cache").json() == {"status": "OK", "removed": 1}
    put({"show_names": False})
    assert h.lookup_handles() == []   # no names shown -> nothing to look up


def test_signals_view_adds_ship_info_but_recording_stays_plain(bridge, tmp_path):
    from .test_shipcaps import MINER, dataset
    from .test_shipcaps import write as wjson
    h, _ = bridge
    from sctelemetry import shipcaps as sc
    miner = dict(MINER, manufacturer="rsi_2025", size=3, crewSize=2, totals={"cargoScu": 32})
    h.shipcaps = sc.ShipCaps([wjson(tmp_path / "game-ships.json", dataset(miner))], cache_path=None)
    h.signals = gl.SignalsState()
    h.signals.apply({"kind": "nearby", "ts": None, "class": "TEST_Miner", "eid": "000000000011"})
    h.signals.apply({"kind": "nearby", "ts": None, "class": "TEST_Unknown", "eid": "000000000012"})
    by = {n["class"]: n for n in h.signals_view()["nearby"]}
    assert by["TEST_Miner"]["info"] == {"display_name": "TEST Miner", "manufacturer": "Roberts Space Industries",
                                        "manufacturer_code": "rsi", "role": "Light Mining", "career": "Industrial",
                                        "size": 3, "crew": 2, "cargo_scu": 32.0}
    assert by["TEST_Unknown"]["info"] is None
    assert all("info" not in n for n in h.public_signals()["nearby"])   # what --record writes


def test_recorders_never_carry_handles(bridge, tmp_path):
    import io
    h, _ = bridge
    h.people_settings["show_names"] = True
    buf = io.StringIO()
    h.signals = gl.SignalsState()
    h.on_signals = srv.signals_recorder(buf)
    h.on_session = srv.session_recorder(buf)
    h.set_session({"source": "game_log", "log_found": True})
    assert buf.getvalue() and "TestPilot" not in buf.getvalue()


def test_sse_snapshot_names_follow_origin(bridge):
    from starlette.requests import Request
    h, _ = bridge
    h.people_settings["show_names"] = True

    def req(headers):
        hdrs = [(b"host", b"127.0.0.1:8766")] + [(k.encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "method": "GET", "path": "/events", "query_string": b"", "headers": hdrs})

    async def first(headers):
        resp = await srv.events(req(headers))
        it = resp.body_iterator
        try:
            return await it.__anext__()
        finally:
            await it.aclose()
    ok, evil = run(first({})), run(first({"origin": "http://evil.example"}))
    assert ok.startswith("data:") and "TestPilot" in ok and "TestPilot" not in evil


# ---- review fixes: toast gating, breaker, strict parse, pruning, clear race, queue, caps, partial --------

def test_you_have_joined_party_word_is_not_a_person():
    st = feed([*note("You have joined", "party"), *note("You have joined", "The Party", sec=1)])
    assert names(st) == []
    assert names(feed(note("You have joined", "TestPilot_Lead"))) == ["TestPilot_Lead"]


def test_party_body_rules_need_their_titles():
    st = feed([*note("Mission Update", "TestPilot_Cargo has left the party."),
               *note("Incoming Transmission", "TestPilot_Some is now party leader.", sec=1),
               *note("Random", "TestPilot_J has joined the party.", sec=2)])
    assert names(st) == []
    st = feed([*note("Member Left", "TestPilot_Cargo has left the party."),
               *note("New Party Leader", "TestPilot_Some is now party leader.", sec=1)])
    assert names(st, "party") == ["TestPilot_Cargo", "TestPilot_Some"]


def _counting(status, page="", exc=None):
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if exc:
            raise exc
        return status, page
    return fetch, calls


@pytest.mark.parametrize("kind", ["challenge", "timeout", "http500"])
def test_circuit_breaker_pauses_after_consecutive_errors(kind):
    clk = Clock()
    fetch, calls = {"challenge": _counting(200, "<html><title>Just a moment...</title></html>"),
                    "timeout": _counting(0, exc=TimeoutError()), "http500": _counting(500)}[kind]
    lk = P.OrgLookup(None, fetch=fetch, enabled=True, min_interval=0, clock=clk, sleep=clk.sleep)
    hs = [f"TestPilot_{i:02d}" for i in range(80)]

    async def go():
        for _ in range(20):   # like run(): only when not blocked
            if clk() >= lk.blocked_until and (h := lk.next_handle(hs)):
                await lk.lookup_one(h)
    run(go())
    assert len(calls) == P.ERROR_TRIP
    st = lk.status()
    assert st["blocked_for_s"] == 60 and st["last_status"].startswith("paused (")


def test_breaker_resets_on_success():
    clk = Clock()
    pages = {"/TestPilot_A": (500, ""), "/TestPilot_B": (500, ""),
             "/TestPilot_C": (200, P.synthetic_citizen_html("TestPilot_C")),
             "/TestPilot_C/organizations": (200, P.synthetic_orgs_html(0, main=False)),
             "/TestPilot_D": (500, ""), "/TestPilot_E": (500, "")}
    fetch, _ = recorder(pages)
    lk = P.OrgLookup(None, fetch=fetch, enabled=True, min_interval=0, clock=clk, sleep=clk.sleep)
    for h in "ABCDE":
        run(lk.lookup_one("TestPilot_" + h))
    assert lk.blocked_until <= clk() and lk.fail_streak == 2


def test_parse_citizen_needs_handle_and_lookup_checks_it():
    assert P.parse_citizen("<div class='profile'><div><div></div></div></div>") is None
    assert P.parse_citizen("<div class=profile><p class=entry><strong class=value>Evil</strong></p></div>") is None
    clk = Clock()
    fetch, _ = recorder({"/TestPilot_A": (200, P.synthetic_citizen_html("TestPilot_Other"))})
    lk = P.OrgLookup(None, fetch=fetch, enabled=True, clock=clk, sleep=clk.sleep)
    assert run(lk.lookup_one("TestPilot_A")) == "error" and lk.cache == {}
    assert "different handle" in lk.status()["last_status"]


def test_expired_entries_pruned_on_load_and_save(tmp_path):
    clk = Clock()
    cache = tmp_path / "rsi-cache.json"
    fetch = P.fake_fetch
    lk = P.OrgLookup(cache, fetch=fetch, enabled=True, min_interval=0, clock=clk, sleep=clk.sleep)
    for h in ("TestPilot_A", "TestPilot_B", "GhostPilot_C"):
        run(lk.lookup_one(h))
    assert len(json.loads(cache.read_text(encoding="utf-8"))["entries"]) == 3
    clk.t += P.NEG_TTL_S + 1   # the 404 entry is past its 1-day TTL
    lk2 = P.OrgLookup(cache, fetch=fetch, enabled=True, clock=clk, sleep=clk.sleep)
    assert sorted(lk2.cache) == ["testpilot_a", "testpilot_b"]
    clk.t += P.TTL_S
    run(lk2.lookup_one("TestPilot_D"))
    assert list(json.loads(cache.read_text(encoding="utf-8"))["entries"]) == ["testpilot_d"]


def test_clear_during_inflight_lookup_stays_cleared(tmp_path):
    cache = tmp_path / "rsi-cache.json"
    import threading
    gate = threading.Event()

    def slow(url, timeout):
        gate.wait(5)
        return P.fake_fetch(url, timeout)
    lk = P.OrgLookup(cache, fetch=slow, enabled=True, min_interval=0)

    async def go():
        t = asyncio.ensure_future(lk.lookup_one("TestPilot_A"))
        await asyncio.sleep(0.05)
        lk.clear()
        gate.set()
        return await t
    assert run(go()) == "cleared"
    assert lk.cache == {} and not cache.exists()


def test_next_handle_scans_past_first_fifty():
    clk = Clock()
    lk = P.OrgLookup(None, fetch=P.fake_fetch, enabled=True, clock=clk, sleep=clk.sleep)
    hs = [f"TestPilot_{i:02d}" for i in range(80)]
    for h in hs[:70]:
        lk.cache[h.casefold()] = {"t": clk(), "status": "ok", "data": {}}
    assert lk.next_handle(hs) == "TestPilot_70"


def test_parser_bails_on_pathological_markup_and_caps_fields():
    import time as _t
    t0 = _t.monotonic()
    assert P.parse_citizen("<p class='entry'><span class='value'>a</span>" * 50000) is None
    assert P.parse_citizen("<div>" * 200000) is None
    assert _t.monotonic() - t0 < 5
    d = P.parse_citizen(P.synthetic_citizen_html("TestPilot_A", moniker="M" * 500,
                                                 org={"name": "O" * 500, "sid": "S" * 500, "rank": "R" * 500}))
    assert len(d["moniker"]) == P.FIELD_MAX and len(d["org"]["name"]) == P.FIELD_MAX
    assert len(d["org"]["sid"]) <= P.FIELD_MAX and len(d["org"]["rank"]) == P.FIELD_MAX


def test_partial_lookup_short_ttl():
    clk = Clock()
    fetch, _ = recorder({"/TestPilot_A": (200, P.synthetic_citizen_html("TestPilot_A")),
                         "/TestPilot_A/organizations": (500, "")})
    lk = P.OrgLookup(None, fetch=fetch, enabled=True, clock=clk, sleep=clk.sleep)
    assert run(lk.lookup_one("TestPilot_A")) == "ok"
    g = lk.get("TestPilot_A")
    assert g["partial"] and g["affiliations"] is None
    assert lk.next_handle(["TestPilot_A"]) is None
    clk.t += P.ERROR_RETRY_S + 1
    assert lk.next_handle(["TestPilot_A"]) == "TestPilot_A"
