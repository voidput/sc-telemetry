"""Game.log parsing + tailer. All lines are synthetic (TEST-NET IPs, fake ids), never real log content."""
import os

import pytest

from sctelemetry import gamelog as gl

T0 = "<2026-01-01T10:00:00.000Z> "
LINES = [
    T0 + 'BackupNameAttachment=" Build(1) 01 Jan 26"  -- used by backup system',
    T0 + "Log started on Thu Jan  1 10:00:00 2026",
    T0 + r"Executable: Z:\games\StarCitizen\PTU\Bin64\StarCitizen.exe",
    T0 + "FileVersion: 5.0.1.2",
    T0 + "[Trace] @session:      '11111111-2222-3333-4444-555555555555'",
    T0 + "[Trace] @env_session:  'ptu-sc-alpha-500-1-2'",
    "<2026-01-01T10:00:10.000Z> Loading screen for Frontend_Main : SC_Frontend closed after 2.34 seconds",
    ("<2026-01-01T10:00:20.000Z> [Notice] <Join PU> address[192.0.2.7] port[64000] shard[ptu_use1b_1_020] "
     "locationId[-123456789012345] [Team_GameServices][GIM][Matchmaking]"),
    ("<2026-01-01T10:00:21.000Z> [Notice] <Update Shard Id> New Shard Id: ptu_use1b_1_020. Old Shard Id "
     "[Team_OnlineTech][Telemetry][Services]"),
    "<2026-01-01T10:02:00.000Z> Loading screen for Nyx : SC_Frontend closed after 99.10 seconds",
]


def write(p, lines, mode="w", end="\r\n"):
    with open(p, mode, encoding="utf-8", newline="") as f:
        f.write("".join(line + end for line in lines))


# ---- pure parsing ------------------------------------------------------------------


def test_parse_join():
    ev = gl.parse_line(LINES[7] + "\r\n")
    assert ev == {"kind": "join", "ts": "2026-01-01T10:00:20.000Z", "address": "192.0.2.7", "port": 64000,
                  "shard": "ptu_use1b_1_020", "location_id": "-123456789012345"}


@pytest.mark.parametrize("line,kind,key,val", [
    (LINES[1], "log_started", "when", "Thu Jan  1 10:00:00 2026"),
    (LINES[2], "executable", "channel", "PTU"),
    (LINES[3], "build", "build", "5.0.1.2"),
    ("FileVersion: 5.0.9.9", "build", "build", "5.0.9.9"),  # no timestamp prefix
    (LINES[4], "session_id", "value", "11111111-2222-3333-4444-555555555555"),
    (LINES[5], "env_session", "value", "ptu-sc-alpha-500-1-2"),
    (LINES[8], "shard", "shard", "ptu_use1b_1_020"),
    (LINES[9], "loading", "system", "Nyx"),
    (LINES[6], "loading", "system", "Main menu"),
])
def test_parse_kinds(line, kind, key, val):
    ev = gl.parse_line(line)
    assert ev["kind"] == kind and ev[key] == val


@pytest.mark.parametrize("line", ["", "garbage", T0 + "[Notice] something else", T0 + "Built on Jan 1 2026"])
def test_parse_ignores(line):
    assert gl.parse_line(line) is None


@pytest.mark.parametrize("shard,code,name,short", [
    ("ptu_use1b_13248297_020", "use1b", "US East", "USE1B · 020"),
    ("pub_euw1a_1_003", "euw1a", "EU West", "EUW1A · 003"),
    ("pub_apse2c_1_9", "apse2c", "Asia-Pacific SE (Sydney)", "APSE2C · 9"),
    ("pub_zzz9_1_001", "zzz9", "ZZZ9", "ZZZ9 · 001"),  # unknown region -> raw
    ("weird", None, None, "WEIRD"),
    (None, None, None, None),
])
def test_parse_shard(shard, code, name, short):
    assert gl.parse_shard(shard) == {"region_code": code, "region_name": name, "shard_short": short}


def test_state_and_events():
    st = gl.SessionState("LIVE")
    evs = []
    for ev in filter(None, map(gl.parse_line, LINES)):
        evs += st.apply(ev)
    f = st.as_dict()
    assert f["channel"] == "PTU" and f["build"] == "5.0.1.2" and f["system"] == "Nyx"
    assert f["shard"] == "ptu_use1b_1_020" and f["region_name"] == "US East" and f["shard_short"] == "USE1B · 020"
    assert f["server_address"] == "192.0.2.7" and f["server_port"] == 64000
    assert f["joined_at"] == "2026-01-01T10:00:20.000Z" and f["log_started_at"] == "2026-01-01T10:00:00.000Z"
    # same shard in Update Shard Id -> no change event; menu + Nyx -> two system events
    assert [e["type"] for e in evs] == ["SESSION_SYSTEM", "SESSION_JOIN", "SESSION_SYSTEM"]
    assert all("192.0.2.7" not in e["detail"] and "1111" not in e["detail"] for e in evs)
    evs = st.apply(gl.parse_line(T0 + "[Notice] <Update Shard Id> New Shard Id: ptu_euc1a_1_004. Old"))
    assert evs[0]["type"] == "SESSION_SHARD_CHANGE" and st.as_dict()["region_name"] == "EU Central"


def test_public_strips_private():
    st = gl.SessionState()
    for ev in filter(None, map(gl.parse_line, LINES)):
        st.apply(ev)
    pub = gl.public(st.as_dict())
    assert not set(gl.PRIVATE_FIELDS) & set(pub)
    assert "192.0.2.7" not in repr(pub) and "11111111" not in repr(pub) and "ptu-sc-alpha" not in repr(pub)
    assert pub["shard"] == "ptu_use1b_1_020"
    assert gl.public(None) is None


def test_default_path():
    assert gl.default_log_path("/sc", "hotfix") == gl.pathlib.Path("/sc/HOTFIX/Game.log")


def test_mock_session():
    s, evs = gl.mock_session()
    assert s["shard"] == "mock_use1b_0_001" and s["system"] == "Stanton" and s["source"] == "mock"
    assert {e["type"] for e in evs} == {"SESSION_JOIN", "SESSION_SYSTEM"}


# ---- tailer ------------------------------------------------------------------------


def make_tailer(p):
    calls = []
    t = gl.GameLogTailer(p, channel="PTU", on_change=lambda s, e: calls.append((s, e)))
    return t, calls


def test_tailer_missing_then_appears(tmp_path):
    p = tmp_path / "Game.log"
    t, calls = make_tailer(p)
    assert t.step() == [] and not t.found and calls == []
    write(p, LINES)
    evs = t.step()
    assert t.found and len(evs) == 3 and calls[-1][0]["system"] == "Nyx" and calls[-1][0]["log_found"]
    n = len(calls)
    assert t.step() == [] and len(calls) == n  # nothing new -> no notify


def test_tailer_partial_line(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES[:7])
    t, _ = make_tailer(p)
    t.step()
    join = LINES[7]
    with open(p, "a", encoding="utf-8", newline="") as f:
        f.write(join[:40])  # game mid-write: no newline yet
    assert t.step() == [] and t.state.as_dict()["shard"] is None
    with open(p, "a", encoding="utf-8", newline="") as f:
        f.write(join[40:] + "\r\n")
    evs = t.step()
    assert [e["type"] for e in evs] == ["SESSION_JOIN"] and t.state.as_dict()["shard"] == "ptu_use1b_1_020"


def test_tailer_rotation_new_file(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES)
    t, _ = make_tailer(p)
    t.step()
    # relaunch: old file moved away, new (shorter) file at the same path
    os.rename(p, tmp_path / "old.log")
    write(p, [LINES[0].replace("10:00", "11:00"), "<2026-01-01T11:00:00.000Z> Log started on X", LINES[3]])
    t.step()
    s = t.state.as_dict()
    assert t.rotations == 1 and s["shard"] is None and s["system"] is None and s["build"] == "5.0.1.2"
    assert s["log_started_at"] == "2026-01-01T11:00:00.000Z" and s["channel"] == "PTU"


def test_tailer_rotation_inplace_rewrite_grown(tmp_path):
    """Same inode, truncated and rewritten past our offset before we polled: head fingerprint catches it."""
    p = tmp_path / "Game.log"
    write(p, LINES[:3])
    t, _ = make_tailer(p)
    t.step()
    new = ["<2026-02-02T09:00:00.000Z> HEADER CHANGED"] + LINES[1:8] + LINES[3:5]
    write(p, new)  # same path, mode "w" keeps the inode
    t.step()
    s = t.state.as_dict()
    assert t.rotations == 1 and s["shard"] == "ptu_use1b_1_020"


def test_tailer_log_started_midstream_resets(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES)
    t, _ = make_tailer(p)
    t.step()
    write(p, ["<2026-01-01T12:00:00.000Z> Log started on Y"], mode="a")
    t.step()
    assert t.state.as_dict()["shard"] is None and t.state.as_dict()["log_started_at"] == "2026-01-01T12:00:00.000Z"


def test_tailer_vanish(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES)
    t, calls = make_tailer(p)
    t.step()
    p.unlink()
    t.step()
    assert not t.found and calls[-1][0]["shard"] is None and not calls[-1][0]["log_found"]


def test_leave_to_menu_ends_shard_session():
    st = gl.SessionState()
    for ev in filter(None, map(gl.parse_line, LINES)):
        st.apply(ev)
    evs = st.apply(gl.parse_line("<2026-01-01T10:30:00.000Z> Loading screen for Frontend_Main : X closed after 1.0 seconds"))
    assert [e["type"] for e in evs] == ["SESSION_LEAVE", "SESSION_SYSTEM"]
    assert "192.0.2.7" not in evs[0]["detail"] and "ptu_use1b_1_020" in evs[0]["detail"]
    f = st.as_dict()
    assert f["system"] == "Main menu" and f["build"] == "5.0.1.2"
    assert all(f[k] is None for k in ("shard", "region_code", "region_name", "server_address", "server_port",
                                      "joined_at", "location_id", "shard_short"))


def test_menu_before_join_is_not_a_leave():
    st = gl.SessionState()
    assert [e["type"] for e in st.apply(gl.parse_line(LINES[6]))] == ["SESSION_SYSTEM"]


def test_public_view_flags_without_values():
    st = gl.SessionState()
    for ev in filter(None, map(gl.parse_line, LINES)):
        st.apply(ev)
    v = gl.public_view(st.as_dict())
    assert v["has_server_address"] and v["has_session_ids"] and not set(gl.PRIVATE_FIELDS) & set(v)
    assert gl.private(st.as_dict())["server_address"] == "192.0.2.7" and gl.public_view(None) is None


def test_tailer_lf_only_and_bad_utf8(tmp_path):
    p = tmp_path / "Game.log"
    write(p, LINES[:7], end="\n")
    with open(p, "ab") as f:
        f.write(b"<2026-01-01T10:00:15.000Z> junk \xff\xfe\xc3 bytes\n" + LINES[7].encode() + b"\n")
    t, _ = make_tailer(p)
    evs = t.step()
    assert t.state.as_dict()["shard"] == "ptu_use1b_1_020" and evs[-1]["type"] == "SESSION_JOIN"
    assert t.log_age() is not None and t.log_age() >= 0


def test_tailer_raising_callback_keeps_tailing(tmp_path, capsys):
    p = tmp_path / "Game.log"
    write(p, LINES[:7])

    def boom(s, e):
        raise OSError("disk full")

    t = gl.GameLogTailer(p, on_change=boom)
    t.step()  # must not raise
    write(p, LINES[7:8], mode="a")
    assert [e["type"] for e in t.step()] == ["SESSION_JOIN"]
    assert "on_change failed: OSError" in capsys.readouterr().err


def test_tailer_run_off_loop_dispatches_on_loop(tmp_path):
    import asyncio
    import threading
    p = tmp_path / "Game.log"
    write(p, LINES)
    seen = []

    async def main():
        t = gl.GameLogTailer(p, on_change=lambda s, e: seen.append((threading.current_thread(), s["system"])),
                             poll_s=0.01)
        task = asyncio.create_task(t.run())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if seen:
                break
        task.cancel()
        with gl.contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert seen and seen[0][0] is threading.main_thread() and seen[0][1] == "Nyx"


def test_tailer_windows_mode_no_inode_and_capped_reads(tmp_path, monkeypatch):
    """Windows semantics: inode numbers ignored, one poll reads at most READ_MAX_PER_POLL bytes."""
    monkeypatch.setattr(gl, "USE_INODE", False)
    monkeypatch.setattr(gl, "READ_MAX_PER_POLL", 64)
    monkeypatch.setattr(gl, "READ_CHUNK", 16)
    p = tmp_path / "Game.log"
    write(p, LINES)
    t, _ = make_tailer(p)
    for _ in range(200):  # the whole file arrives over several capped polls
        t.step()
        if t._pos >= p.stat().st_size:
            break
    assert t.state.as_dict()["shard"] == "ptu_use1b_1_020" and t.rotations == 0
    os.rename(p, tmp_path / "old.log")  # relaunch: new file, bigger than our offset, new head
    write(p, [LINES[0].replace("10:00", "11:00")] + LINES[1:] + LINES[1:])
    for _ in range(400):
        t.step()
        if t._pos >= p.stat().st_size:
            break
    assert t.rotations == 1 and t.state.as_dict()["log_started_at"] is not None


# ---- login queue, hub link, game-server disconnects -------------------------------------

QUEUE = T0 + ("[Notice] <AccountLoginQueueStatus> Received a queue status update message with values: "
              "queue name global - position {pos} - eta (seconds) {eta} [Team_GameServices][Login]")
HUB_DOWN = T0 + "[Notice] <Hub> disconnected - status[-3] [Team_GameServices][Login]"
HUB_UP = "<2026-01-01T10:00:05.000Z> [Notice] <Hub> connected - initiate[1] [Team_GameServices][Login]"


def dc(reason, rules="SC_Default", host="Replicant", ts="2026-01-01T11:00:00.000Z", up="3725.5"):
    return (f'<{ts}> [Notice] <Channel Disconnected> cause=1 reason="{reason}" frame=9 isRemote=1 '
            f'viewState=eCVS_InGame map="megamap" gamerules="{rules}" hostType="{host}" remoteAddr=192.0.2.7:64000 '
            f'localAddr=198.51.100.2:5000 connection={{3, 0}} session=abc123 node_id=11111111-2222-3333-4444-555555555555 '
            f'nickname="SomeHandle" playerGEID=123456789 uptime_secs={up} [Team_Network][Network][Replicant]')


def test_parse_queue_hub_disconnect():
    q = gl.parse_line(QUEUE.format(pos=42, eta=180))
    assert q == {"kind": "queue", "ts": "2026-01-01T10:00:00.000Z", "name": "global", "position": 42, "eta_s": 180}
    assert gl.parse_line(HUB_DOWN) == {"kind": "hub", "ts": "2026-01-01T10:00:00.000Z", "state": "disconnected", "code": -3}
    assert gl.parse_line(HUB_UP)["state"] == "connected"
    d = gl.parse_line(dc("Remote Disconnect - player inactive"))
    assert d["kind"] == "disconnect" and d["reason"] == "Remote Disconnect - player inactive"
    assert d["gamerules"] == "SC_Default" and d["uptime_s"] == 3725.5
    # never carries the player's handle, addresses or ids
    assert not {"SomeHandle", "192.0.2.7", "198.51.100.2", "abc123", "123456789"} & {str(v) for v in d.values()}


def test_queue_events_and_cleared_on_join():
    st = gl.SessionState()
    evs = st.apply(gl.parse_line(QUEUE.format(pos=42, eta=180)))
    evs += st.apply(gl.parse_line(QUEUE.format(pos=42, eta=170)))  # same position: no new event
    evs += st.apply(gl.parse_line(QUEUE.format(pos=7, eta=30)))
    assert [e["detail"] for e in evs] == ["position 42, ETA 3m 00s", "position 7, ETA 30s"]
    assert st.as_dict()["queue_position"] == 7 and st.as_dict()["queue_eta_s"] == 30
    st.apply(gl.parse_line(LINES[7]))
    assert st.as_dict()["queue_position"] is None and st.as_dict()["queue_name"] is None


def test_hub_down_then_up():
    st = gl.SessionState()
    assert st.apply(gl.parse_line(HUB_UP)) == []  # first connect at login: not news
    evs = st.apply(gl.parse_line(HUB_DOWN)) + st.apply(gl.parse_line(HUB_DOWN))
    evs += st.apply(gl.parse_line(HUB_UP))
    assert [e["type"] for e in evs] == ["SESSION_HUB_DOWN", "SESSION_HUB_UP"]
    assert evs[0]["detail"] == "lost (status -3)" and st.as_dict()["hub"] == "up"


def test_disconnects_expected_dropped_frontend_and_dedup():
    st = gl.SessionState()
    assert st.apply(gl.parse_line(dc("Nub destroyed", rules="SC_Frontend"))) == []  # menu channel: ignored
    evs = st.apply(gl.parse_line(dc("DisconnectCmd: disconnect light ExitToMenu")))
    evs += st.apply(gl.parse_line(dc("DisconnectCmd: disconnect light ExitToMenu", host="GameClient")))  # same one
    evs += st.apply(gl.parse_line(dc("Remote Disconnect - Dup Login", ts="2026-01-01T12:00:00.000Z", up="59")))
    assert [(e["type"], e["detail"]) for e in evs] == [
        ("SESSION_DISCONNECT", "DisconnectCmd: disconnect light ExitToMenu (after 1h 02m)"),
        ("SESSION_DROPPED", "Remote Disconnect - Dup Login (after 59s)")]
    f = st.as_dict()
    assert f["disconnects"] == 2 and f["last_disconnect_expected"] is False and f["last_disconnect_uptime_s"] == 59.0
    pub = gl.public(f)
    assert "SomeHandle" not in repr(pub) and "192.0.2.7" not in repr(pub)


@pytest.mark.parametrize("s,out", [(None, None), (-1, None), (0, "0s"), (59.9, "59s"), (60, "1m 00s"),
                                   (3725.5, "1h 02m")])
def test_fmt_secs(s, out):
    assert gl.fmt_secs(s) == out
