"""Star Citizen install discovery (Windows, Linux/Wine, macOS/CrossOver) in one place.

Used by schema.py (StarCitizen.exe for the telemetry schema), gamelog.py / server.py (Game.log) and the
scripts. Nothing here is specific to one machine: the user's own install is found by env override or by
the generic candidate list / Wine-prefix globs below.

Install dir = the `StarCitizen` folder that holds one directory per channel (LIVE, PTU, ...), each with
`Bin64/StarCitizen.exe` and `Game.log`.

Order (first install dir that holds a channel wins):
  1. explicit argument (--sc-dir) or $SC_DIR
  2. Windows: <drive>:\\Program Files\\Roberts Space Industries\\StarCitizen, <drive>:\\Roberts Space
     Industries\\StarCitizen, %ProgramFiles%, plus a best-effort parse of the RSI Launcher log
  3. Linux / macOS: $WINEPREFIX, ~/Games/star-citizen (LUG Helper / Lutris default), ~/.wine, CrossOver
     bottles, then globs: ~/Games/*, Steam Proton prefixes (<steam>/steamapps/compatdata/*/pfx under
     ~/.steam/steam, ~/.local/share/Steam, Flatpak Steam), /mnt/*/star-citizen*, /mnt/*/*/star-citizen*, /media/<user>/*/...,
     /run/media/<user>/*/... (each + drive_c/Program Files/Roberts Space Industries/StarCitizen)
Channel: $SCTELEMETRY_CHANNEL, else the channel whose Game.log (or exe) was written last, else LIVE.
"""
from __future__ import annotations

import os
import pathlib
import re
import string
import sys

from . import envcompat

CHANNELS = ("LIVE", "PTU", "HOTFIX", "EPTU", "TECH-PREVIEW")
DEFAULT_CHANNEL = "LIVE"
RSI_REL = ("Roberts Space Industries", "StarCitizen")
WINE_REL = ("drive_c", "Program Files", *RSI_REL)
STEAM_ROOTS = (".steam/steam", ".local/share/Steam", ".var/app/com.valvesoftware.Steam/.local/share/Steam")
_LAUNCHER_PATH = re.compile(r"([A-Za-z]:[\\/][^\"'\r\n<>|*?]*?[\\/]StarCitizen)[\\/](?:" + "|".join(CHANNELS) + r")\b",
                            re.IGNORECASE)


def _env(env):
    return os.environ if env is None else env


def is_install(p: pathlib.Path) -> bool:
    """True if p holds at least one channel dir with StarCitizen.exe or Game.log."""
    try:
        return any((p / c / "Bin64" / "StarCitizen.exe").is_file() or (p / c / "Game.log").is_file()
                   for c in CHANNELS)
    except OSError:
        return False


def _launcher_dirs(appdata: str | None) -> list[pathlib.Path]:
    """Install dirs named in the RSI Launcher's log (best effort; format is not documented)."""
    if not appdata:
        return []
    out: list[pathlib.Path] = []
    for log in (pathlib.Path(appdata) / "rsilauncher" / "logs" / "log.log",
                pathlib.Path(appdata) / "rsilauncher" / "log.log"):
        try:
            text = log.read_text(encoding="utf-8", errors="replace")[-2_000_000:]
        except OSError:
            continue
        for m in _LAUNCHER_PATH.finditer(text.replace("\\\\", "\\")):
            p = pathlib.Path(m.group(1))
            if p not in out:
                out.append(p)
    return out


def candidates(env=None, platform: str | None = None, home: pathlib.Path | None = None,
               mounts: tuple[str, ...] = ("/mnt", "/media", "/run/media"),
               drives: list[str] | None = None) -> list[pathlib.Path]:
    """Ordered install-dir candidates for this OS (not checked for existence, except glob results)."""
    env = _env(env)
    platform = platform or sys.platform
    home = pathlib.Path(home) if home is not None else pathlib.Path.home()
    out: list[pathlib.Path] = []

    def add(p):
        p = pathlib.Path(p)
        if p not in out:
            out.append(p)

    if platform == "win32":
        if drives is None:
            drives = [f"{d}:\\" for d in string.ascii_uppercase[2:] if os.path.exists(f"{d}:\\")]  # C..Z
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if env.get(var):
                add(pathlib.Path(env[var]).joinpath(*RSI_REL))
        for d in drives:
            add(pathlib.Path(d, "Program Files", *RSI_REL))
            add(pathlib.Path(d).joinpath(*RSI_REL))
            add(pathlib.Path(d, "Games", *RSI_REL))
        for p in _launcher_dirs(env.get("APPDATA")):
            add(p)
        return out

    if env.get("WINEPREFIX"):
        add(pathlib.Path(env["WINEPREFIX"]).expanduser().joinpath(*WINE_REL))
    add(home.joinpath("Games", "star-citizen", *WINE_REL))
    add(home.joinpath(".wine", *WINE_REL))
    if platform == "darwin":
        for b in sorted(home.glob("Library/Application Support/CrossOver/Bottles/*")):
            add(b.joinpath(*WINE_REL))
    for pat in ("Games/*",):
        for p in sorted(home.glob(pat)):
            add(p.joinpath(*WINE_REL))
    for steam in STEAM_ROOTS:  # Steam Proton: <steam>/steamapps/compatdata/<appid>/pfx
        try:
            hits = sorted(home.joinpath(steam, "steamapps", "compatdata").glob("*/pfx"))
        except OSError:
            hits = []
        for p in hits:
            add(p.joinpath(*WINE_REL))
    for m in mounts:
        root = pathlib.Path(m)
        for pat in ("star-citizen*", "*/star-citizen*", "*/*/star-citizen*", "*/Games/star-citizen*"):
            try:
                hits = sorted(root.glob(pat))
            except OSError:
                hits = []
            for p in hits:
                add(p.joinpath(*WINE_REL))
    return out


def find_sc_dir(explicit=None, env=None, **kw) -> pathlib.Path | None:
    """Install dir: explicit / $SC_DIR (returned even if it does not exist yet, so errors name it),
    else the first auto-detected candidate that holds a channel, else None."""
    env = _env(env)
    chosen = explicit or env.get("SC_DIR")
    if chosen:
        return pathlib.Path(chosen).expanduser()
    for p in candidates(env=env, **kw):
        if is_install(p):
            return p
    return None


def searched(explicit=None, env=None, **kw) -> list[pathlib.Path]:
    """What find_sc_dir looked at (for error messages)."""
    env = _env(env)
    chosen = explicit or env.get("SC_DIR")
    return [pathlib.Path(chosen).expanduser()] if chosen else candidates(env=env, **kw)


def _mtime(p: pathlib.Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return -1.0


def default_channel(sc_dir: pathlib.Path | None, env=None) -> str:
    """$SCTELEMETRY_CHANNEL (upper-cased, not validated here), else the channel last played (newest
    Game.log, then newest exe), else LIVE."""
    env = _env(env)
    ch = envcompat.getenv("CHANNEL", env=env)
    if ch:
        return ch.upper()
    if sc_dir is None:
        return DEFAULT_CHANNEL
    best, best_t = DEFAULT_CHANNEL, -1.0
    for c in CHANNELS:
        t = max(_mtime(sc_dir / c / "Game.log"), _mtime(sc_dir / c / "Bin64" / "StarCitizen.exe"))
        if t > best_t:
            best, best_t = c, t
    return best


def describe_search(explicit=None, env=None, limit: int = 12, **kw) -> str:
    s = [str(p) for p in searched(explicit, env, **kw)]
    more = f" (+{len(s) - limit} more)" if len(s) > limit else ""
    return "; ".join(s[:limit]) + more if s else "(no candidates)"


def setup_hint() -> str:
    if sys.platform == "win32":
        return ('set SC_DIR, e.g. PowerShell: $env:SC_DIR = "C:\\Program Files\\Roberts Space Industries\\StarCitizen"')
    return ('set SC_DIR, e.g. export SC_DIR="$HOME/Games/star-citizen/drive_c/Program Files/'
            'Roberts Space Industries/StarCitizen"')
