"""Dashboard state logic (the <script id="state-logic"> block of scgameapi/static/index.html), run in node on
synthetic frames against the real action_state_map.json / shield_faces.json. Skipped without node."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

PKG = pathlib.Path(__file__).resolve().parents[1] / "scgameapi"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def logic_js() -> str:
    html = (PKG / "static" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script id="state-logic">(.*?)</script>', html, re.DOTALL)
    assert m, "state-logic script block missing"
    return m.group(1)


def run(tmp_path, body: str):
    """Run `body` (JS that returns a JSON-able value) with StateLogic + MAP + FACES loaded; returns it."""
    js = (logic_js() + "\nconst MAP = " + (PKG / "data" / "action_state_map.json").read_text(encoding="utf-8")
          + ";\nconst FACES = " + (PKG / "data" / "shield_faces.json").read_text(encoding="utf-8")
          + ";\nconst L = globalThis.StateLogic, IDX = L.indexMap(MAP);\n"
          + "const out = (() => {" + body + "})();\nprocess.stdout.write(JSON.stringify(out));\n")
    f = tmp_path / "t.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([NODE, str(f)], check=False, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


VIEW = """
const V = (name, frame, extra) => { const v = L.buttonView(name, Object.assign({ index: IDX, frame, stale: false, caps: null,
  unsupported: new Set() }, extra || {})); return v.mapped ? [v.text, v.tone, v.lit, v.unverified] : null; };
const ship = (vs, rest) => Object.assign({ vehicle: { state: Object.assign({ class_name: "TEST_Ship" }, vs) } }, rest || {});
const avail = (en, ac) => ({ enabled: en, active: ac });
"""


def test_bool_toggle_on_off_lit(tmp_path):
    r = run(tmp_path, VIEW + """
      const on = ship({ lights_enabled: true }), off = ship({ lights_enabled: false });
      return [V("v_lights", on), V("v_lights_on", on), V("v_lights_off", on), V("v_lights", off), V("v_lights_off", off)];""")
    assert r == [["ON", "lit", True, False], ["ON", "lit", True, False], ["ON", "muted", False, False],
                 ["OFF", "muted", False, False], ["OFF", "lit", True, False]]


def test_enum_gear_and_unverified_vtol(tmp_path):
    r = run(tmp_path, VIEW + """
      const P = "VEHICLE_COMPONENT_DEPLOYMENT_STATE_", fr = g => ship({ flight: { state: { gear: P + g, vtol: P + "RETRACTED" } } });
      return [V("v_deploy_landing_system", fr("DEPLOYED")), V("v_retract_landing_system", fr("DEPLOYED")),
              V("v_retract_landing_system", fr("RETRACTED")), V("v_toggle_landing_system", fr("DEPLOYING")),
              V("v_vtol_on", fr("RETRACTED")), V("v_toggle_landing_system", fr("UNSPECIFIED"))];""")
    assert r == [["DOWN", "lit", True, False], ["DOWN", "muted", False, False], ["UP", "lit", True, False],
                 ["DEPLOYING", "lit", True, False], ["OFF", "muted", False, True], ["N/A", "na", False, False]]


def test_availability_gsafe_and_inverted_decoupled(tmp_path):
    r = run(tmp_path, VIEW + """
      const fr = (gs, cp) => ship({ flight: { settings: { ifcs_g_safe: gs, ifcs_coupled: cp } } });
      return [V("v_ifcs_toggle_gforce_safety", fr(avail(true, true), avail(true, true))),
              V("v_ifcs_gsafe_on", fr(avail(true, false), avail(true, true))),
              V("v_ifcs_gsafe_off", fr(avail(false, false), avail(true, true))),
              V("v_ifcs_vector_decoupling_on", fr(avail(true, true), avail(true, true))),
              V("v_ifcs_vector_decoupling_on", fr(avail(true, true), avail(false, false))),
              V("v_ifcs_vector_decoupling_off", fr(avail(true, true), avail(true, false)))];""")
    assert r == [["ON", "lit", True, False], ["ARMED", "lit", True, False], ["OFF", "lit", True, False],
                 ["COUPLED", "muted", False, False], ["DECOUPLED", "lit", True, False], ["COUPLED", "lit", True, False]]


def test_master_mode_active_when_and_spooling(tmp_path):
    r = run(tmp_path, VIEW + """
      const fr = (m, sp) => ship({ master_mode: { mode: "VEHICLE_MASTER_MODE_" + m, spooled: sp } });
      return [V("v_master_mode_set_scm", fr("SCM", true)), V("v_master_mode_set_nav", fr("SCM", true)),
              V("v_master_mode_cycle", fr("NAVIGATION", false)), V("v_master_mode_set_nav", fr("NAVIGATION", false))];""")
    assert r == [["SCM", "lit", True, False], ["SCM", "muted", False, False],
                 ["NAV · SPOOLING", "warn", False, False], ["NAV · SPOOLING", "lit", True, False]]


def test_value_annunciator_path_by_master_alt_path(tmp_path):
    r = run(tmp_path, VIEW + """
      const fr = ship({ master_mode: { mode: "VEHICLE_MASTER_MODE_SCM" }, annunciators: { qdrv: "ANNUNCIATOR_STATE_MAJOR_FLASHING" },
        flight: { instruments: { g_meter: { max: 12.14 } }, settings: { advanced_hud_enabled_scm: true, advanced_hud_enabled_nav: false } } },
        { turret: { light_amplification: true } });
      return [V("v_ifcs_reset_gmeter_max", fr), V("v_toggle_qdrive_engagement", fr), V("v_flight_advanced_hud_on", fr),
              V("v_light_amplification_toggle", fr)];""")
    assert r == [["peak 12.1 G", "muted", False, False], ["MAJOR!", "crit", False, True],
                 ["ON", "lit", True, True], ["ON", "lit", True, True]]


def test_doors_absent_and_unsupported_after_failed(tmp_path):
    r = run(tmp_path, VIEW + """
      const none = ship({}), closed = ship({ doors: { open_state: "DOOR_OPEN_STATE_CLOSED", lock_state: "DOOR_LOCK_STATE_LOCKED" } });
      const uns = { unsupported: new Set(["doors|TEST_Ship"]) };
      const v = L.buttonView("v_open_all_doors", Object.assign({ index: IDX, frame: closed, caps: null }, uns));
      return [V("v_open_all_doors", none), V("v_close_all_doors", closed), V("v_lock_all_doors", closed),
              V("v_open_all_doors", closed, uns), v.notes.some(n => n.startsWith("Known issue:")), V("v_lock_all_doors", closed, uns)];""")
    assert r == [["N/A", "na", False, True], ["CLOSED", "lit", True, True], ["LOCKED", "lit", True, True],
                 ["N/A", "na", False, True], True, ["LOCKED", "lit", True, True]]


def test_operator_modes_any_bool_and_caps_gating(tmp_path):
    r = run(tmp_path, VIEW + """
      const fr = ship({}, { weapons: { weapons: [{ ready_to_fire: false }, { ready_to_fire: true }] } });
      const idle = ship({}, { weapons: { weapons: [] } });
      const caps = { caps: { mining: false, salvage: true, guns: true }, source_build: "5.0.1", display_name: "Test" };
      const g = L.buttonView("v_set_mining_mode", { index: IDX, frame: fr, caps });
      return [V("v_set_guns_mode", fr), V("v_set_guns_mode", idle), V("v_set_mining_mode", fr, { caps }),
              V("v_set_salvage_mode", fr, { caps }), V("v_set_mining_mode", fr), V("v_set_flight_mode", fr, { caps }),
              g.nocap, g.notes[0].includes("5.0.1"), V("v_set_mining_mode", ship({}), { caps })];""")
    assert r == [["GUNS", "lit", True, False], ["NOT GUNS", "muted", False, False],
                 ["N/A · no mining", "nocap", False, False], ["", "muted", False, False],
                 ["", "muted", False, True], None, "mining", True,
                 ["N/A · no mining", "nocap", False, False]]


def test_stale_no_ship_and_stateless(tmp_path):
    r = run(tmp_path, VIEW + """
      return [V("v_lights", ship({ lights_enabled: true }), { stale: true }), V("v_lights", {}), V("v_lights", null),
              V("emote_wave", ship({})), V("v_eject", ship({})), V("toggle_flashlight", {}),
              L.isStateless(IDX, "emote_dance"), L.isStateless(IDX, "v_lights")];""")
    assert r == [["STALE", "na", False, False], ["NO SHIP", "na", False, False], ["NO SHIP", "na", False, False],
                 None, None, None, True, False]


def test_every_group_kind_is_supported(tmp_path):
    r = run(tmp_path, """
      const kinds = new Set(["bool", "enum", "availability", "annunciator", "value", "any_bool"]);
      return MAP.groups.filter(g => !kinds.has(g.kind)).map(g => g.id).concat(Object.keys(IDX.byAction).length > 50 ? [] : ["few"]);""")
    assert r == []


def test_face_layouts_by_count(tmp_path):
    r = run(tmp_path, """
      const s = n => { const x = L.faceLayout(n, FACES, "X"); return [x.layout, x.labels, x.ring, x.pips, x.segs.length, x.verified]; };
      const q = L.faceLayout(4, FACES, "X").segs.map(g => [Math.round(g.a0), Math.round(g.a1)]);
      const ov = L.faceLayout(2, { by_count: FACES.by_count, overrides_by_class: { Y: { layout: "front_back", labels: ["FRONT", "BACK"], verified: true } } }, "Y");
      const bad = L.faceLayout(2, { by_count: { "2": { labels: ["only one"] } } }, "X");
      return [s(1), s(2), s(4), s(6), s(5), s(0), q, L.faceLayout(1, null, "X").segs[0].full, [ov.labels, ov.verified], bad.labels];""")
    assert r[0] == ["bubble", ["ALL"], [0], [], 1, True]
    assert r[1] == ["front_back", ["F1", "F2"], [0, 1], [], 2, False]
    assert r[2] == ["quadrant", ["F1", "F2", "LEFT", "F4"], [0, 1, 2, 3], [], 4, False]
    assert r[3] == ["quadrant_top_bottom", ["F1", "F2", "F3", "F4", "F5", "F6"], [0, 1, 2, 3], [4, 5], 4, False]
    assert r[4] == ["segments", ["F1", "F2", "F3", "F4", "F5"], [0, 1, 2, 3, 4], [], 5, False]
    assert r[5][0] == "none" and r[5][4] == 0
    assert r[6] == [[-132, -48], [-42, 42], [48, 132], [138, 222]]  # quadrant 0 centred on the top (front)
    assert r[7] is True
    assert r[8] == [["FRONT", "BACK"], True]
    assert r[9] == ["F1", "F2"]  # invalid meta (label count mismatch) falls back to built-in defaults


def test_face_states_down_never_colour_only(tmp_path):
    r = run(tmp_path, """
      return [L.faceStates([0, 0.2, 0.4, 0.9, null], 0.5), L.faceStates([0, 0], 0)];""")
    assert [(x["text"], x["sev"]) for x in r[0]] == [("DOWN", "critical"), ("20%", "critical"), ("40%", "warning"),
                                                       ("90%", "nominal"), ("—", "none")]
    assert [x["text"] for x in r[1]] == ["0%", "0%"]  # everything down = shields off, not "DOWN"


# ---- alerts (<script id="alert-logic">) ------------------------------------------------------------

def run_alerts(tmp_path, body: str):
    html = (PKG / "static" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script id="alert-logic">(.*?)</script>', html, re.DOTALL)
    assert m, "alert-logic script block missing"
    f = tmp_path / "a.js"
    f.write_text(m.group(1) + "\nconst L = globalThis.AlertLogic;\nconst out = (() => {" + body
                 + "})();\nprocess.stdout.write(JSON.stringify(out));\n", encoding="utf-8")
    r = subprocess.run([NODE, str(f)], check=False, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_toast_stack_sticky_critical_coalesce_and_expiry(tmp_path):
    r = run_alerts(tmp_path, """
      const s = new L.ToastStack(3), a = (id, rule, severity) => ({ id, rule, severity, text: rule + " " + id });
      s.add(a(1, "boost_low", "warn"), 0); s.add(a(2, "missile", "critical"), 100); s.add(a(3, "qt", "info"), 200);
      const again = s.add(a(4, "boost_low", "warn"), 300);           // same rule on screen -> updated, not stacked
      const n1 = s.list().length;
      s.add(a(5, "prox", "warn"), 400);                                // overflow drops the oldest NON-sticky
      const afterOverflow = s.list().map(t => t.rule);
      const gone = s.expire(6300).map(t => t.rule);                    // info ttl 6 s
      const gone2 = s.expire(20000).map(t => t.rule);                  // warns gone, critical sticky stays
      const left = s.list().map(t => [t.rule, t.sticky, t.label]);
      const acked = s.ack(s.list()[0].key);
      return [again.n, again.text, n1, afterOverflow, gone, gone2, left, acked, s.list().length];""")
    assert r[0] == 2 and r[1] == "boost_low 4" and r[2] == 3
    assert r[3] == ["prox", "qt", "missile"]  # boost_low (oldest non-sticky) dropped, critical kept
    assert r[4] == ["qt"] and r[5] == ["prox"]
    assert r[6] == [["missile", True, "CRITICAL"]] and r[7] is True and r[8] == 0


def test_tones_play_decision_and_route_text(tmp_path):
    r = run_alerts(tmp_path, """
      const msg = (kind, play) => ({ kind, play, alert: { rule: "x" } });
      return [L.tonesFor("warn", "info").length, L.tonesFor("nope", "critical").length, L.tonesFor(null, "warn").length,
              Math.round(L.toneLength(L.TONES.critical) * 100),
              L.shouldPlay(msg("play", true), true), L.shouldPlay(msg("play", true), false),
              L.shouldPlay(msg("play", false), true), L.shouldPlay(msg("toast", true), true),
              L.routeText({ route: { to: "browser" } }, true, true, false),
              L.routeText({ route: { to: "bridge" }, bridge: { player: "pw-play", tts: null, sink: "dry-run" } }, false, false, false),
              L.routeText({ muted: true, route: { to: "bridge" } }), L.routeText({ route: { to: "none" } })];""")
    assert r[:4] == [3, 5, 0, 58]
    assert r[4:8] == [True, False, False, False]
    assert r[8] == ["AUDIO · THIS TAB (TONES)", "nominal"]
    assert r[9] == ["AUDIO · BRIDGE (SOUNDS) DRY RUN", "info"]
    assert r[10] == ["MUTED", "warning"] and r[11] == ["AUDIO OFF", "muted"]


def test_alert_key_survives_bridge_restart_and_audio_claim(tmp_path):
    r = run_alerts(tmp_path, """
      const seen = new Set(), add = a => { const k = L.alertKey(a); if (seen.has(k)) return false; seen.add(k); return true; };
      const before = [1, 2, 3].map(id => add({ id, boot: "aaa" }));
      const dup = add({ id: 2, boot: "aaa" });
      const after = [1, 2].map(id => add({ id, boot: "bbb" }));          // restarted bridge: ids start at 1 again
      const s = new L.ToastStack(5);
      const k1 = s.add({ id: 1, boot: "aaa", rule: "x", severity: "critical", text: "a" }, 0).key;
      const k2 = s.add({ id: 1, boot: "bbb", rule: "y", severity: "critical", text: "b" }, 0).key;
      return [before, dup, after, k1 !== k2,
              L.canClaim(true, "running", false), L.canClaim(true, "suspended", false), L.canClaim(true, null, true),
              L.canClaim(false, "running", true), L.canClaim(true, null, false)];""")
    assert r[0] == [True, True, True] and r[1] is False and r[2] == [True, True] and r[3] is True
    assert r[4:] == [True, False, True, False, False]


def test_toast_critical_ttl_ack_all_and_narrow_cap(tmp_path):
    r = run_alerts(tmp_path, """
      const s = new L.ToastStack(5), c = (id, rule) => ({ id, boot: "b", rule, severity: "critical", text: rule });
      for (let i = 1; i <= 5; i++) s.add(c(i, "r" + i), 0);
      s.setMax(3);                                                       // phone width: 3 max
      const n3 = s.list().length;
      const left59 = s.expire(59000).length, left61 = s.expire(61000).map(t => t.rule);
      s.add(c(6, "a"), 0); s.add({ id: 7, boot: "b", rule: "w", severity: "warn", text: "w" }, 0);
      const all = s.ackAll();
      return [n3, left59, left61, all, s.list().length, s.list().length === 0 && s.ackAll() === 0];""")
    assert r[0] == 3 and r[1] == 0 and sorted(r[2]) == ["r3", "r4", "r5"]
    assert r[3] == 2 and r[4] == 0 and r[5] is True
