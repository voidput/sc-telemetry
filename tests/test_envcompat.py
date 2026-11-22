"""SCTELEMETRY_* env vars with the deprecated SCGAMEAPI_* fallback (scgameapi -> sc-telemetry rename)."""
import pytest

from sctelemetry import envcompat, paths, server as srv


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(envcompat, "_warned", set())


def test_new_name_wins_without_warning(capsys):
    env = {"SCTELEMETRY_CHANNEL": "ptu", "SCGAMEAPI_CHANNEL": "hotfix"}
    assert envcompat.getenv("CHANNEL", env=env) == "ptu"
    assert capsys.readouterr().err == ""


def test_legacy_name_falls_back_and_warns_once(capsys):
    env = {"SCGAMEAPI_GAMEDATA": "/x/game-ships.json"}
    assert envcompat.getenv("GAMEDATA", env=env) == "/x/game-ships.json"
    assert envcompat.getenv("GAMEDATA", env=env) == "/x/game-ships.json"
    err = capsys.readouterr().err
    assert err.count("deprecated") == 1
    assert "$SCGAMEAPI_GAMEDATA" in err and "$SCTELEMETRY_GAMEDATA" in err


def test_unset_and_empty_use_default(capsys):
    assert envcompat.getenv("ALERTS", env={}) is None
    assert envcompat.getenv("ALERTS_AUDIO", "auto", env={"SCTELEMETRY_ALERTS_AUDIO": "", "SCGAMEAPI_ALERTS_AUDIO": ""}) == "auto"
    assert capsys.readouterr().err == ""


def test_reads_os_environ_by_default(monkeypatch):
    monkeypatch.delenv("SCTELEMETRY_SHOW_NAMES", raising=False)
    monkeypatch.setenv("SCGAMEAPI_SHOW_NAMES", "1")
    assert envcompat.getenv("SHOW_NAMES") == "1"


def test_default_channel_honours_legacy_name(tmp_path):
    assert paths.default_channel(tmp_path, env={"SCGAMEAPI_CHANNEL": "eptu"}) == "EPTU"
    assert paths.default_channel(tmp_path, env={"SCTELEMETRY_CHANNEL": "ptu", "SCGAMEAPI_CHANNEL": "eptu"}) == "PTU"


def test_alerts_user_path_legacy_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(srv.CFG, "alerts", None, raising=False)
    monkeypatch.delenv("SCTELEMETRY_ALERTS", raising=False)
    monkeypatch.setenv("SCGAMEAPI_ALERTS", str(tmp_path / "old.json"))
    assert srv.alerts_user_path() == tmp_path / "old.json"
    monkeypatch.setenv("SCTELEMETRY_ALERTS", str(tmp_path / "new.json"))
    assert srv.alerts_user_path() == tmp_path / "new.json"


def test_deprecated_console_alias_notes_and_runs_main(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(srv, "main", lambda: called.append(True))
    srv.main_deprecated_alias()
    assert called == [True]
    assert "scgameapi-dashboard` is deprecated" in capsys.readouterr().err
