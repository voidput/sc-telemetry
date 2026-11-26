"""Ship capabilities (can this ship mine / salvage / refuel ...) from sc-gamedata datasets.

The game returns status OK for v_set_mining_mode / _salvage_mode / _refuel_mode even on ships without
that hardware, and telemetry `weapons[]` lists only guns, so capability comes from GAME DATA: the JSON
datasets an sc-gamedata export of the installed game produces (`game-ships.json`, `game-salvage.json`). Optional:
without a dataset, capability gating is simply off.

Source priority (first existing file wins), per channel:
  --gamedata PATH / $SCTELEMETRY_GAMEDATA (file or directory)
  <repo>/.tmp/gamedata/<channel>/game-ships.json   (e.g. .tmp/gamedata/live/ or ptu/; filled by hand or by
                                                    the opt-in dumps-hub fetch, gamedata_hub.py)
A vehicle's upstream `capabilities` object (when sc-gamedata emits one) wins per key over derive_caps.
The derived table is cached in <repo>/.tmp/ship-capabilities.json keyed by source path/mtime/size/build
and reloaded when the source file's mtime changes. Pure functions below; ShipCaps holds the state.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time

from . import envcompat, stubs

CAPS = ("mining", "salvage", "refuel", "tractor", "guns", "scan", "quantum", "qed", "emp", "missiles", "cargo")
# not a capability flag: cargo_scu is a number | None riding alongside the booleans in `caps`
SHIPS_FILE = "game-ships.json"
SALVAGE_FILE = "game-salvage.json"
CACHE_PATH = stubs.ROOT / ".tmp" / "ship-capabilities.json"
CACHE_VERSION = 5  # 3: + manufacturer / size / crew per class (Nearby lookup info); 4: upstream `capabilities`; 5: upstream manufacturer name
RECHECK_S = 5.0  # min seconds between mtime checks of the source file
RECORD_PREFIX = "EntityClassDefinition."
# dataset `manufacturer` codes -> names (the dataset carries only the code; `_2025`-style suffixes stripped)
MANUFACTURERS = {
    "aegs": "Aegis Dynamics", "anvl": "Anvil Aerospace", "argo": "ARGO Astronautics", "banu": "Banu",
    "cnou": "Consolidated Outland", "crus": "Crusader Industries", "drak": "Drake Interplanetary",
    "espr": "Esperia", "gama": "Gatac Manufacture", "grey": "Grey's Market", "grin": "Greycat Industrial",
    "krig": "Kruger Intergalactic", "misc": "MISC", "mrai": "Mirai", "orig": "Origin Jumpworks",
    "rsi": "Roberts Space Industries", "tmbl": "Tumbril Land Systems", "vncl": "Vanduul", "xnaa": "Aopoa",
}


def class_of(record_name) -> str | None:
    """'EntityClassDefinition.RSI_Perseus' -> 'RSI_Perseus' (== telemetry vehicle.state.class_name)."""
    if not isinstance(record_name, str) or not record_name.strip():
        return None
    return record_name.strip().removeprefix(RECORD_PREFIX)


def _items(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _present(v) -> bool:
    """Loadout slot present: a non-empty list, or a positive count (turrets / missileRacks are ints)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, list):
        return len(v) > 0
    return isinstance(v, dict) and bool(v)


def salvage_classes(salvage: dict | None) -> set[str]:
    """Classes the salvage dataset names. Current datasets (4.10 / 5.0) carry only beam constants, no
    ship list; accept a future `vehicles` / `ships` list of class or record names (or dicts with one)."""
    out = set()
    if not isinstance(salvage, dict):
        return out
    for key in ("vehicles", "ships"):
        for x in salvage.get(key) or []:
            name = x if isinstance(x, str) else (x.get("recordName") or x.get("class") or x.get("className")) \
                if isinstance(x, dict) else None
            c = class_of(name)
            if c:
                out.add(c)
    return out


def derive_caps(vehicle: dict, salvage_set: frozenset | set = frozenset()) -> dict:
    """One dataset vehicle -> {cap: bool, ..., cargo: bool, cargo_scu: float | None}. `cargo` = SCU total > 0
    or `loadout.cargoGrids` present; False means "not in the dataset", not "has none". `cargo_scu` is
    `totals.cargoScu` when the dataset has it, else None."""
    lo = vehicle.get("loadout") if isinstance(vehicle.get("loadout"), dict) else {}
    role = str(vehicle.get("role") or "").lower()
    wtypes = {str(w.get("type") or "") for w in _items(lo.get("weapons"))}
    cls = class_of(vehicle.get("recordName"))
    totals = vehicle.get("totals") if isinstance(vehicle.get("totals"), dict) else {}
    scu = totals.get("cargoScu")
    has_grid = _present(lo.get("cargoGrids"))
    cargo_scu = _num(scu)
    return {
        "mining": "WeaponMining" in wtypes or "mining" in role,
        # not weapon names: "Ardor-2 Salvaged Repeater" is a gun
        "salvage": "salvage" in role or cls in salvage_set,
        "refuel": "refuel" in role,
        "tractor": "TractorBeam" in wtypes,
        "guns": "WeaponGun" in wtypes or _present(lo.get("turrets")),
        "scan": _present(lo.get("radars")),
        "quantum": _present(lo.get("quantumDrives")),
        "qed": _present(lo.get("quantumEnforcement")),
        "emp": "EMP" in wtypes,
        "missiles": _present(lo.get("missileRacks")),
        "cargo": cargo_scu is not None or has_grid,
        "cargo_scu": cargo_scu,
    }


def _num(v) -> float | None:
    return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def upstream_caps(vehicle: dict) -> dict | None:
    """`vehicle.capabilities` as sc-gamedata emits it (same keys as derive_caps, plus `cargoKnown`), or None
    when absent / not an object. Only boolean flags count; a missing or non-bool flag stays None so the
    caller falls back to the derived value for that key. `cargoKnown: false` -> cargo False, cargo_scu None."""
    c = vehicle.get("capabilities")
    if not isinstance(c, dict):
        return None
    out = {k: c[k] if isinstance(c.get(k), bool) else None for k in CAPS}
    out["cargo_scu"] = _num(c.get("cargo_scu"))
    if c.get("cargoKnown") is False:
        out["cargo"], out["cargo_scu"] = False, None
    return out


def vehicle_caps(vehicle: dict, salvage_set: frozenset | set = frozenset()) -> dict:
    """Upstream `capabilities` when present (per key), else derive_caps. The salvage dataset's ship list
    still adds salvage; `cargo_scu` falls back to the derived total only when upstream has no cargo data
    either way (no `cargo_scu` key and no `cargoKnown: false`)."""
    derived = derive_caps(vehicle, salvage_set)
    up = upstream_caps(vehicle)
    if up is None:
        return derived
    out = {k: derived[k] if up[k] is None else up[k] for k in CAPS}
    if class_of(vehicle.get("recordName")) in salvage_set:
        out["salvage"] = True
    raw = vehicle["capabilities"]
    out["cargo_scu"] = up["cargo_scu"] if ("cargo_scu" in raw or raw.get("cargoKnown") is False) \
        else derived["cargo_scu"]
    return out


def _merge(a: dict, b: dict) -> dict:
    """Two variants sharing a class: a capability is present if either has it; cargo_scu = the larger."""
    out = {k: bool(a.get(k) or b.get(k)) for k in CAPS}
    nums = [v for v in (a.get("cargo_scu"), b.get("cargo_scu")) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    out["cargo_scu"] = max(nums) if nums else None
    return out


def manufacturer_of(code) -> tuple[str | None, str | None]:
    """Dataset manufacturer code -> (code, name). "rsi_2025" -> ("rsi", "Roberts Space Industries");
    an unknown code keeps the code upper-cased as its name."""
    if not isinstance(code, str) or not code.strip():
        return None, None
    c = code.strip().lower().split("_")[0]
    return c, MANUFACTURERS.get(c, c.upper())


def vehicle_manufacturer(v: dict) -> tuple[str | None, str | None]:
    """(code, name) for a dataset vehicle. Upstream `manufacturerCode` / `manufacturerName` (sc-gamedata,
    e.g. "RSI_2025" / "Roberts Space Industries") win; the code keeps this API's form (lower case, `_2025`
    suffix stripped). Fallback: the `manufacturer` code + the MANUFACTURERS table."""
    code, name = manufacturer_of(v.get("manufacturerCode") if isinstance(v.get("manufacturerCode"), str)
                                 and v["manufacturerCode"].strip() else v.get("manufacturer"))
    up = v.get("manufacturerName")
    return code, (up.strip() if isinstance(up, str) and up.strip() else name)


def _pos_int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def build_table(ships: dict, salvage: dict | None = None) -> dict[str, dict]:
    """game-ships.json dict -> {CLASS: {class, display_name, role, career, manufacturer, manufacturer_code,
    size, crew, caps}}. Variants sharing a class keep the first variant's descriptive fields."""
    sal = salvage_classes(salvage)
    table: dict[str, dict] = {}
    for v in ships.get("vehicles") or [] if isinstance(ships, dict) else []:
        if not isinstance(v, dict):
            continue
        cls = class_of(v.get("recordName"))
        if not cls:
            continue
        caps = vehicle_caps(v, sal)
        name = v.get("displayName") or v.get("name") or cls
        if cls in table:
            table[cls]["caps"] = _merge(table[cls]["caps"], caps)
            table[cls]["variants"] += 1
            continue
        table[cls] = {"class": cls, "display_name": name if isinstance(name, str) else cls,
                      "role": v.get("role") if isinstance(v.get("role"), str) else None,
                      "career": v.get("career") if isinstance(v.get("career"), str) else None,
                      "manufacturer_code": vehicle_manufacturer(v)[0],
                      "manufacturer": vehicle_manufacturer(v)[1],
                      "size": _pos_int(v.get("size")), "crew": _pos_int(v.get("crewSize")),
                      "caps": caps, "variants": 1}
    return table


def candidates(channel: str = "PTU", override: str | os.PathLike | None = None,
               root: pathlib.Path = stubs.ROOT) -> list[pathlib.Path]:
    """Ordered game-ships.json candidates. `override` may be a file or a directory holding one."""
    out = []
    if override:
        p = pathlib.Path(override).expanduser()
        out.append(p / SHIPS_FILE if p.is_dir() else p)
    out.append(root / ".tmp" / "gamedata" / str(channel or "PTU").lower() / SHIPS_FILE)
    return out


def resolve(cands: list[pathlib.Path]) -> pathlib.Path | None:
    for p in cands:
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_size > 0:
                return p
    return None


def source_label(p: pathlib.Path | None) -> str | None:
    """Public name of a dataset file: `<parent dir>/<file>` (e.g. `ptu/game-ships.json`), never the absolute
    path (home dir / username stay out of the snapshot)."""
    return f"{p.parent.name}/{p.name}" if p is not None else None


class ShipCaps:
    """Loaded capability table; `get(cls)` re-checks the source mtime at most every RECHECK_S."""

    def __init__(self, cands: list[pathlib.Path], cache_path: pathlib.Path | None = CACHE_PATH):
        self.cands = cands
        self.cache_path = cache_path
        self.table: dict[str, dict] = {}
        self.source: pathlib.Path | None = None
        self.build: str | None = None
        self.error: str | None = None
        self._sig = None
        self._checked = 0.0
        self.reload()

    # ---- loading ---------------------------------------------------------------------------
    @staticmethod
    def _stat_sig(p: pathlib.Path):
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)

    def reload(self, force: bool = False) -> None:
        self._checked = time.monotonic()
        src = resolve(self.cands)
        if src is None:
            self.table, self.source, self.build, self._sig = {}, None, None, None
            self.error = "no game-ships.json found (" + ", ".join(source_label(p) for p in self.cands) + ")"
            return
        try:
            sig = self._stat_sig(src)
        except OSError as e:
            self.error = f"{source_label(src)}: {type(e).__name__}"
            return
        if sig == self._sig and not force:
            return
        cached = self._read_cache(sig)
        if cached is not None:
            self.table, self.build = cached["ships"], cached.get("build")
        else:
            try:
                ships = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:  # half-written extract: keep the previous table
                # OSError text carries the absolute path; a JSON error's text (line/col) does not
                self.error = f"{source_label(src)}: {type(e).__name__}" + (f": {e}" if isinstance(e, ValueError) else "")
                return
            salvage = None
            with contextlib.suppress(OSError, ValueError):
                salvage = json.loads((src.parent / SALVAGE_FILE).read_text(encoding="utf-8"))
            self.table = build_table(ships, salvage)
            b = ships.get("_build") if isinstance(ships, dict) else None
            self.build = b if isinstance(b, str) else None
            self._write_cache(sig)
        self.source, self._sig, self.error = src, sig, None

    def _read_cache(self, sig):
        if not self.cache_path:
            return None
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            c = json.loads(self.cache_path.read_text(encoding="utf-8"))
            key = c.get("key") or {}
            if c.get("version") == CACHE_VERSION and [key.get("path"), key.get("mtime_ns"), key.get("size")] == list(sig) \
                    and isinstance(c.get("ships"), dict):
                return c
        return None

    def _write_cache(self, sig):
        if not self.cache_path:
            return
        with contextlib.suppress(OSError):
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "build": self.build,
                                       "key": {"path": sig[0], "mtime_ns": sig[1], "size": sig[2]},
                                       "ships": self.table}, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.cache_path)

    def maybe_reload(self) -> None:
        if time.monotonic() - self._checked >= RECHECK_S:
            self.reload()

    # ---- queries ---------------------------------------------------------------------------
    def status(self) -> dict:
        return {"loaded": bool(self.table), "source": source_label(self.source),
                "source_build": self.build, "ships": len(self.table), "error": self.error}

    def get(self, cls) -> dict | None:
        """Public capabilities for a telemetry class_name, or None (no vehicle / unknown class)."""
        if not isinstance(cls, str) or not cls:
            return None
        self.maybe_reload()
        e = self.table.get(cls)
        if e is None:
            return None
        return {"class": e["class"], "display_name": e["display_name"], "role": e["role"],
                "caps": dict(e["caps"]), "source_build": self.build,
                "source": source_label(self.source)}


    def info(self, cls) -> dict | None:
        """Descriptive lookup info for a ship class (Nearby panel): game data only, no network. None when
        the class is unknown to the dataset."""
        if not isinstance(cls, str) or not cls:
            return None
        self.maybe_reload()
        e = self.table.get(cls)
        if e is None:
            return None
        return {"display_name": e["display_name"], "manufacturer": e.get("manufacturer"),
                "manufacturer_code": e.get("manufacturer_code"), "role": e["role"], "career": e["career"],
                "size": e.get("size"), "crew": e.get("crew"), "cargo_scu": e["caps"].get("cargo_scu")}


def default(channel: str = "PTU", override=None) -> ShipCaps:
    return ShipCaps(candidates(channel, override or envcompat.getenv("GAMEDATA") or None))
