"""Star Citizen install discovery (sctelemetry/paths.py) on simulated Windows / Linux / macOS layouts.
Everything lives under tmp_path; no real /home, /mnt or C:\\ path is ever read."""
import os
import pathlib

import pytest

from sctelemetry import gamelog, paths, schema

WINE = ("drive_c", "Program Files", "Roberts Space Industries", "StarCitizen")


def install(base: pathlib.Path, *channels, log=True, exe=True, t=None):
    for i, c in enumerate(channels):
        d = base / c
        if exe:
            (d / "Bin64").mkdir(parents=True, exist_ok=True)
            (d / "Bin64" / "StarCitizen.exe").write_bytes(b"MZ")
        if log:
            d.mkdir(parents=True, exist_ok=True)
            (d / "Game.log").write_text("<2026-01-01T00:00:00.000Z> Log started\n", encoding="utf-8")
            if t is not None:
                os.utime(d / "Game.log", (t + i, t + i))
                if exe:
                    os.utime(d / "Bin64" / "StarCitizen.exe", (t - 100, t - 100))
    return base


def kw(tmp_path, platform="linux", env=None, **extra):
    return {"env": env or {}, "platform": platform, "home": tmp_path / "home",
            "mounts": (str(tmp_path / "mnt"), str(tmp_path / "media")), **extra}


def test_env_sc_dir_wins_even_if_missing(tmp_path):
    other = install(tmp_path / "home" / "Games" / "star-citizen" / pathlib.Path(*WINE), "LIVE")
    got = paths.find_sc_dir(env={"SC_DIR": str(tmp_path / "nope")}, **{k: v for k, v in kw(tmp_path).items() if k != "env"})
    assert got == tmp_path / "nope" and got != other
    assert paths.find_sc_dir(str(tmp_path / "cli"), env={"SC_DIR": "x"}, platform="linux") == tmp_path / "cli"


def test_linux_lug_default_then_wineprefix_then_mount_glob(tmp_path):
    assert paths.find_sc_dir(**kw(tmp_path)) is None
    mnt = install(tmp_path / "mnt" / "games" / "star-citizen" / pathlib.Path(*WINE), "PTU")
    assert paths.find_sc_dir(**kw(tmp_path)) == mnt  # generic /mnt/*/star-citizen* glob finds a custom drive
    lug = install(tmp_path / "home" / "Games" / "star-citizen" / pathlib.Path(*WINE), "LIVE")
    assert paths.find_sc_dir(**kw(tmp_path)) == lug  # LUG/Lutris default beats the glob
    wp = install(tmp_path / "pfx" / pathlib.Path(*WINE), "LIVE")
    assert paths.find_sc_dir(**kw(tmp_path, env={"WINEPREFIX": str(tmp_path / "pfx")})) == wp
    media = install(tmp_path / "media" / "user" / "ssd" / "star-citizen-2" / pathlib.Path(*WINE), "LIVE")
    assert media in paths.candidates(**kw(tmp_path))


def test_linux_candidate_without_channel_is_skipped(tmp_path):
    empty = tmp_path / "home" / "Games" / "star-citizen" / pathlib.Path(*WINE)
    (empty / "LIVE").mkdir(parents=True)  # dir exists but no exe / Game.log yet
    assert not paths.is_install(empty)
    real = install(tmp_path / "home" / "Games" / "sc-other" / pathlib.Path(*WINE), "LIVE", exe=False)
    assert paths.find_sc_dir(**kw(tmp_path)) == real  # ~/Games/* glob


def test_windows_drives_program_files_and_launcher_log(tmp_path):
    c, d = tmp_path / "C", tmp_path / "D"
    env = {"ProgramFiles": str(c / "Program Files"), "APPDATA": str(tmp_path / "appdata")}
    k = {"env": env, "platform": "win32", "drives": [str(c), str(d)]}
    cands = paths.candidates(**k)
    assert cands[0] == c / "Program Files" / "Roberts Space Industries" / "StarCitizen"
    assert d / "Roberts Space Industries" / "StarCitizen" in cands
    assert paths.find_sc_dir(**k) is None
    alt = install(d / "Roberts Space Industries" / "StarCitizen", "LIVE")
    assert paths.find_sc_dir(**k) == alt
    pf = install(c / "Program Files" / "Roberts Space Industries" / "StarCitizen", "PTU")
    assert paths.find_sc_dir(**k) == pf  # %ProgramFiles% first
    log = tmp_path / "appdata" / "rsilauncher" / "logs" / "log.log"
    log.parent.mkdir(parents=True)
    log.write_text('{"t":"x","msg":"Launching E:\\\\Games\\\\RSI\\\\StarCitizen\\\\LIVE\\\\Bin64\\\\StarCitizen.exe"}\n'
                   "info: install path F:\\SC\\StarCitizen\\PTU ok\n", encoding="utf-8")
    got = [str(p).replace("/", "\\") for p in paths.candidates(**k)]
    assert "E:\\Games\\RSI\\StarCitizen" in got and "F:\\SC\\StarCitizen" in got  # best effort, de-escaped


def test_macos_crossover_bottle(tmp_path):
    b = install(tmp_path / "home" / "Library/Application Support/CrossOver/Bottles/SC" / pathlib.Path(*WINE), "LIVE")
    assert paths.find_sc_dir(**kw(tmp_path, platform="darwin")) == b


def test_default_channel_env_then_last_played_then_live(tmp_path):
    sc = install(tmp_path / "sc", "LIVE", "PTU", t=1_700_000_000)  # PTU Game.log written last
    assert paths.default_channel(sc, env={}) == "PTU"
    assert paths.default_channel(sc, env={"SCTELEMETRY_CHANNEL": "hotfix"}) == "HOTFIX"
    assert paths.default_channel(None, env={}) == "LIVE"
    assert paths.default_channel(tmp_path / "missing", env={}) == "LIVE"


def test_error_lists_searched_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "CACHE", tmp_path / "cache")
    for v in ("SCTELEMETRY_PROTO_DIR", "SCTELEMETRY_EXE", "SCTELEMETRY_CHANNEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SC_DIR", str(tmp_path / "no-install"))
    with pytest.raises(RuntimeError) as e:
        schema.resolve(log=lambda m: None)
    msg = str(e.value)
    assert str(tmp_path / "no-install") in msg and "SC_DIR" in msg and "SCTELEMETRY_EXE" in msg


def test_schema_uses_detected_install_and_channel(tmp_path, monkeypatch):
    sc = install(tmp_path / "sc", "LIVE", t=1_700_000_000)
    seen = {}
    monkeypatch.setattr(schema, "_ensure", lambda exe, key, log: seen.setdefault("exe", exe))
    for v in ("SCTELEMETRY_PROTO_DIR", "SCTELEMETRY_EXE", "SCTELEMETRY_CHANNEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SC_DIR", str(sc))
    schema.resolve(log=lambda m: None)
    assert seen["exe"] == sc / "LIVE" / "Bin64" / "StarCitizen.exe"


def test_gamelog_default_path_lazy(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_DIR", str(tmp_path / "sc"))
    assert gamelog.default_log_path(None, "ptu") == tmp_path / "sc" / "PTU" / "Game.log"
    monkeypatch.delenv("SC_DIR")
    monkeypatch.setattr(paths, "find_sc_dir", lambda explicit=None, **k: None)
    assert gamelog.default_log_path(None, "LIVE") is None


def test_no_private_install_path_in_code():
    src = pathlib.Path(paths.__file__).parent
    for f in src.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "/mnt/games" not in text and "git/sc-gamedata" not in text, f.name


def test_linux_steam_proton_prefix(tmp_path):
    pfx = tmp_path / "home" / ".local" / "share" / "Steam" / "steamapps" / "compatdata" / "4242" / "pfx"
    sc = install(pfx / pathlib.Path(*WINE), "LIVE")
    assert paths.find_sc_dir(**kw(tmp_path)) == sc
    flat = tmp_path / "home" / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    assert flat / "steamapps" / "compatdata" / "1" / "pfx" / pathlib.Path(*WINE) not in paths.candidates(**kw(tmp_path))
    install(flat / "steamapps" / "compatdata" / "1" / "pfx" / pathlib.Path(*WINE), "PTU")
    assert flat / "steamapps" / "compatdata" / "1" / "pfx" / pathlib.Path(*WINE) in paths.candidates(**kw(tmp_path))
