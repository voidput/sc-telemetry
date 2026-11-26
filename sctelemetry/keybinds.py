"""Trigger action -> game keybind info from sc-gamedata `game-keybinds.json` (optional).

ListTriggerableActions / TriggerGameAction names are the game's actionmap action names verbatim (checked:
all 112 of a PTU list exist in game-keybinds 4.10.193), so no alias table. Some names live in more than one
actionmap (e.g. `mobiglas` in player + spaceship_hud + vehicle_general; `v_open_all_doors` in
spaceship_general + vehicle_general), so an entry lists every actionmap it appears in.

Source (first existing file wins): the `--gamedata` directory (or the directory of a `--gamedata` file),
then `<repo>/.tmp/gamedata/<channel>/game-keybinds.json`. Missing / broken file -> no keybind info, nothing
else changes. Reloaded when the file's mtime/size changes (checked at most every RECHECK_S).
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time

from . import envcompat, shipcaps, stubs

KEYBINDS_FILE = "game-keybinds.json"
DEVICES = ("keyboard", "mouse", "gamepad", "joystick")
RECHECK_S = 5.0


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _defaults(d) -> dict:
    """{device: [input, ...]} with only non-empty string lists kept."""
    out = {}
    for dev in DEVICES:
        v = d.get(dev) if isinstance(d, dict) else None
        keys = [k for k in v if isinstance(k, str) and k] if isinstance(v, list) else []
        if keys:
            out[dev] = keys
    return out


def build_index(data) -> dict[str, dict]:
    """game-keybinds.json -> {action_name: {label, activation_mode, actionmaps: [name...], defaults}}.
    Label / activation mode / defaults come from the first actionmap that has them (duplicates across
    actionmaps carry the same binding in current data); unknown keys are ignored."""
    idx: dict[str, dict] = {}
    for am in data.get("actionmaps") or [] if isinstance(data, dict) else []:
        if not isinstance(am, dict):
            continue
        am_name = _str(am.get("name"))
        for a in am.get("actions") or [] if isinstance(am.get("actions"), list) else []:
            if not isinstance(a, dict) or not _str(a.get("name")):
                continue
            e = idx.setdefault(a["name"], {"label": None, "activation_mode": None, "actionmaps": [], "defaults": {}})
            if am_name and am_name not in e["actionmaps"]:
                e["actionmaps"].append(am_name)
            e["label"] = e["label"] or _str(a.get("label"))
            e["activation_mode"] = e["activation_mode"] or _str(a.get("activationMode"))
            if not e["defaults"]:
                e["defaults"] = _defaults(a.get("defaults"))
    return idx


def candidates(channel: str = "PTU", override: str | os.PathLike | None = None,
               root: pathlib.Path = stubs.ROOT) -> list[pathlib.Path]:
    out = []
    if override:
        p = pathlib.Path(override).expanduser()
        out.append((p if p.is_dir() else p.parent) / KEYBINDS_FILE)
    out.append(root / ".tmp" / "gamedata" / str(channel or "PTU").lower() / KEYBINDS_FILE)
    return out


class Keybinds:
    def __init__(self, cands: list[pathlib.Path]):
        self.cands = cands
        self.index: dict[str, dict] = {}
        self.source: pathlib.Path | None = None
        self.build: str | None = None
        self.error: str | None = None
        self._sig = None
        self._checked = 0.0
        self.reload()

    def reload(self, force: bool = False) -> None:
        self._checked = time.monotonic()
        src = shipcaps.resolve(self.cands)
        if src is None:
            self.index, self.source, self.build, self._sig, self.error = {}, None, None, None, None
            return
        try:
            st = src.stat()
            sig = (str(src), st.st_mtime_ns, st.st_size)
        except OSError as e:
            self.error = f"{shipcaps.source_label(src)}: {type(e).__name__}"
            return
        if sig == self._sig and not force:
            return
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # half-written: keep the previous index
            self.error = f"{shipcaps.source_label(src)}: {type(e).__name__}"
            return
        self.index = build_index(data)
        b = data.get("_build") if isinstance(data, dict) else None
        self.build = b if isinstance(b, str) else None
        self.source, self._sig, self.error = src, sig, None

    def maybe_reload(self) -> None:
        if time.monotonic() - self._checked >= RECHECK_S:
            with contextlib.suppress(Exception):
                self.reload()

    def get(self, name) -> dict | None:
        if not isinstance(name, str):
            return None
        self.maybe_reload()
        e = self.index.get(name)
        return {**e, "actionmaps": list(e["actionmaps"]), "defaults": {k: list(v) for k, v in e["defaults"].items()}} \
            if e else None

    def status(self) -> dict:
        return {"loaded": bool(self.index), "source": shipcaps.source_label(self.source),
                "source_build": self.build, "actions": len(self.index), "error": self.error}


def default(channel: str = "PTU", override=None) -> Keybinds:
    return Keybinds(candidates(channel, override or envcompat.getenv("GAMEDATA") or None))
