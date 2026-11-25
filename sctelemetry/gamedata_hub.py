"""Opt-in fetch of sc-gamedata datasets from the public dumps hub (voidput/sc-gamedata-dumps).

The hub has one git tag per game build, named by its build string (e.g. `4.10.201.48085`, `5.0.202.35026`;
PTU tags are GitHub prereleases), with datasets at `data/game-*.json` and, on newer tags, `build.json` at the
root. This module reads the installed build of a channel from `<SC>/<CHANNEL>/build_manifest.id`
(`Data.RequestedP4kVersion`, else `Data.Version`: the same string sc-gamedata stamps as `_build`) and
downloads that tag's datasets into `<repo>/.tmp/gamedata/<channel>/`. Never another build: a missing tag or
dataset only leaves gating off, as without the hub.

Off by default (`--gamedata-fetch` / `$SCTELEMETRY_GAMEDATA_FETCH=1`). `--gamedata PATH` wins and disables the
fetch. Manual run: `uv run python -m sctelemetry.gamedata_hub [--channel PTU] [--sc-dir DIR]`.

Checks per dataset: valid JSON object, `_build == tag`, `_schema` (when present) names the dataset
(`game-ships@1`), and size / sha256 against `build.json.files[]` when the tag lists them. `build.json.build`
must equal the tag. Unknown keys are ignored. Writes are atomic (temp file + rename); a dataset whose local
copy already has `_build == tag` is not downloaded again.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

from . import __version__, envcompat, stubs

HUB_REPO = "voidput/sc-gamedata-dumps"
RAW_BASE = "https://raw.githubusercontent.com/" + HUB_REPO
DATASETS = ("game-ships.json", "game-salvage.json", "game-keybinds.json")
TIMEOUT_S = 20.0
MAX_BYTES = 64 * 1024 * 1024
USER_AGENT = f"sc-telemetry/{__version__} (gamedata fetch)"
_TAG_RE = re.compile(r"^\d+(?:\.\d+){2,3}$")


def installed_build(sc_dir, channel) -> str | None:
    """Build string of an installed channel, or None (no install / unreadable manifest)."""
    if not sc_dir or not channel:
        return None
    try:
        m = json.loads((pathlib.Path(sc_dir) / str(channel).upper() / "build_manifest.id")
                       .read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    data = m.get("Data", m) if isinstance(m, dict) else {}
    if not isinstance(data, dict):
        return None
    for key in ("RequestedP4kVersion", "Version"):
        v = data.get(key)
        if isinstance(v, str) and _TAG_RE.match(v.strip()):
            return v.strip()
    return None


def dest_dir(channel, root: pathlib.Path = stubs.ROOT) -> pathlib.Path:
    return root / ".tmp" / "gamedata" / str(channel).lower()


def urllib_get(url: str, timeout: float = TIMEOUT_S) -> tuple[int, bytes]:
    """Blocking GET -> (status, body). 4xx/5xx come back as their status with an empty body."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError(f"response over {MAX_BYTES} bytes")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, b""


def local_build(p: pathlib.Path) -> str | None:
    with contextlib.suppress(OSError, ValueError):
        d = json.loads(p.read_text(encoding="utf-8"))
        b = d.get("_build") if isinstance(d, dict) else None
        return b if isinstance(b, str) else None
    return None


def _file_meta(build_json: dict | None, name: str) -> dict:
    files = build_json.get("files") if isinstance(build_json, dict) else None
    for f in files if isinstance(files, list) else []:
        if isinstance(f, dict) and f.get("name") in (name, f"data/{name}"):
            return f
    return {}


def check_dataset(name: str, body: bytes, tag: str, meta: dict) -> str | None:
    """None when `body` is a valid dataset for `tag`, else the reason it is rejected."""
    if isinstance(meta.get("bytes"), int) and meta["bytes"] != len(body):
        return f"size {len(body)} != build.json {meta['bytes']}"
    sha = meta.get("sha256")
    if isinstance(sha, str) and hashlib.sha256(body).hexdigest() != sha.lower():
        return "sha256 mismatch"
    try:
        d = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        return f"invalid JSON: {type(e).__name__}"
    if not isinstance(d, dict):
        return "not a JSON object"
    if d.get("_build") != tag:
        return f"_build {d.get('_build')!r} != tag {tag}"
    schema = d.get("_schema")
    stem = name.removesuffix(".json")
    if schema is not None and not (isinstance(schema, str) and schema.split("@")[0] == stem):
        return f"_schema {schema!r} is not {stem}"
    return None


def _write_atomic(p: pathlib.Path, body: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".partial")
    tmp.write_bytes(body)
    os.replace(tmp, p)


def sync(sc_dir, channel, root: pathlib.Path = stubs.ROOT, get=urllib_get, datasets=DATASETS) -> dict:
    """Fetch the installed build's datasets for `channel`. Never raises for network / data problems:
    returns {build, channel, fetched, current, missing, rejected: {name: reason}, error}."""
    rep = {"build": None, "channel": str(channel or "").upper() or None, "fetched": [], "current": [],
           "missing": [], "rejected": {}, "error": None}
    tag = installed_build(sc_dir, channel)
    rep["build"] = tag
    if tag is None:
        rep["error"] = f"no installed build for {rep['channel']} (build_manifest.id)"
        return rep
    out = dest_dir(channel, root)
    todo = [n for n in datasets if local_build(out / n) != tag]
    rep["current"] = [n for n in datasets if n not in todo]
    if not todo:
        return rep
    try:
        status, body = get(f"{RAW_BASE}/{tag}/build.json")
        build_json = None
        if status == 200:
            with contextlib.suppress(UnicodeDecodeError, ValueError):
                build_json = json.loads(body.decode("utf-8"))
            if isinstance(build_json, dict) and build_json.get("build") not in (None, tag):
                rep["error"] = f"build.json build {build_json.get('build')!r} != tag {tag}"
                return rep
        elif status != 404:
            rep["error"] = f"build.json: HTTP {status}"
            return rep
        for name in todo:
            status, body = get(f"{RAW_BASE}/{tag}/data/{name}")
            if status == 404:
                rep["missing"].append(name)
                continue
            if status != 200:
                rep["rejected"][name] = f"HTTP {status}"
                continue
            why = check_dataset(name, body, tag, _file_meta(build_json, name))
            if why:
                rep["rejected"][name] = why
                continue
            _write_atomic(out / name, body)
            rep["fetched"].append(name)
    except (OSError, ValueError) as e:  # URLError / timeout / oversize
        rep["error"] = f"{type(e).__name__}: {e}"
    return rep


def summary(rep: dict) -> str:
    if rep.get("error") and not rep.get("fetched"):
        return f"[gamedata] fetch skipped: {rep['error']}"
    parts = [f"build {rep['build']} ({rep['channel']})"]
    for key in ("fetched", "current", "missing"):
        if rep.get(key):
            parts.append(f"{key}: {', '.join(rep[key])}")
    if rep.get("rejected"):
        parts.append("rejected: " + ", ".join(f"{k} ({v})" for k, v in rep["rejected"].items()))
    if rep.get("error"):
        parts.append(f"error: {rep['error']}")
    return "[gamedata] " + "; ".join(parts)


def enabled_by_env() -> bool:
    return envcompat.getenv("GAMEDATA_FETCH") == "1"


def main(argv=None) -> int:
    from . import paths
    ap = argparse.ArgumentParser(prog="python -m sctelemetry.gamedata_hub", description=__doc__.split("\n")[0])
    ap.add_argument("--channel", type=str.upper, default=(envcompat.getenv("CHANNEL") or "").upper() or None)
    ap.add_argument("--sc-dir", default=os.environ.get("SC_DIR") or None)
    a = ap.parse_args(argv)
    sc_dir = paths.find_sc_dir(a.sc_dir)
    channel = a.channel or paths.default_channel(sc_dir)
    rep = sync(sc_dir, channel)
    print(summary(rep), file=sys.stderr)
    return 0 if not rep["error"] and not rep["rejected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
