"""Dumps-hub fetch (sctelemetry/gamedata_hub.py): synthetic manifests and a fake HTTP getter, no network."""
import hashlib
import json

from sctelemetry import gamedata_hub as gh

TAG = "9.9.9.1"


def install(tmp_path, channel="PTU", data=None):
    sc = tmp_path / "StarCitizen"
    (sc / channel).mkdir(parents=True)
    (sc / channel / "build_manifest.id").write_text(
        json.dumps({"Data": data if data is not None else {"Version": TAG, "RequestedP4kVersion": None}}),
        encoding="utf-8-sig")
    return sc


def ds(build=TAG, **extra):
    return json.dumps({"_build": build, "vehicles": [], **extra}).encode()


class FakeGet:
    def __init__(self, files):
        self.files, self.urls = files, []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        key = url.removeprefix(f"{gh.RAW_BASE}/")
        if isinstance(self.files.get(key), Exception):
            raise self.files[key]
        return (200, self.files[key]) if key in self.files else (404, b"")


def test_installed_build_prefers_requested_p4k_version(tmp_path):
    sc = install(tmp_path, data={"RequestedP4kVersion": "5.0.1.2", "Version": "5.0.1.1"})
    assert gh.installed_build(sc, "ptu") == "5.0.1.2"
    sc2 = install(tmp_path / "b", data={"RequestedP4kVersion": "", "Version": TAG})
    assert gh.installed_build(sc2, "PTU") == TAG
    assert gh.installed_build(sc2, "LIVE") is None and gh.installed_build(None, "PTU") is None
    sc3 = install(tmp_path / "c", data={"Version": "../../evil"})  # never interpolate junk into the URL
    assert gh.installed_build(sc3, "PTU") is None


def test_sync_fetches_verifies_and_writes_atomically(tmp_path):
    sc = install(tmp_path)
    ships = ds(_schema="game-ships@1", future_key=1)
    get = FakeGet({f"{TAG}/build.json": json.dumps({"build": TAG, "channel": "PTU", "files": [
        {"name": "game-ships.json", "sha256": hashlib.sha256(ships).hexdigest(), "bytes": len(ships)}]}).encode(),
        f"{TAG}/data/game-ships.json": ships, f"{TAG}/data/game-salvage.json": ds()})
    rep = gh.sync(sc, "PTU", root=tmp_path, get=get)
    assert rep["error"] is None and rep["build"] == TAG
    assert rep["fetched"] == ["game-ships.json", "game-salvage.json"] and rep["missing"] == ["game-keybinds.json"]
    out = tmp_path / ".tmp/gamedata/ptu"
    assert (out / "game-ships.json").read_bytes() == ships and not list(out.glob("*.partial"))
    # second run: local copies already carry _build == tag -> only the missing one is requested again
    get2 = FakeGet({})
    rep2 = gh.sync(sc, "PTU", root=tmp_path, get=get2)
    assert rep2["current"] == ["game-ships.json", "game-salvage.json"]
    assert [u.rsplit("/", 1)[1] for u in get2.urls] == ["build.json", "game-keybinds.json"]


def test_sync_rejects_wrong_build_bad_hash_and_schema(tmp_path):
    sc = install(tmp_path)
    get = FakeGet({f"{TAG}/build.json": json.dumps({"files": [{"name": "game-salvage.json", "sha256": "00"}]}).encode(),
                   f"{TAG}/data/game-ships.json": ds(build="9.9.9.0"),
                   f"{TAG}/data/game-salvage.json": ds(),
                   f"{TAG}/data/game-keybinds.json": ds(_schema="game-ships@1")})
    rep = gh.sync(sc, "PTU", root=tmp_path, get=get)
    assert rep["fetched"] == []
    assert set(rep["rejected"]) == set(gh.DATASETS)
    assert "_build" in rep["rejected"]["game-ships.json"] and "sha256" in rep["rejected"]["game-salvage.json"]
    assert "_schema" in rep["rejected"]["game-keybinds.json"]
    assert not (tmp_path / ".tmp/gamedata/ptu").exists()


def test_sync_build_json_mismatch_and_network_error(tmp_path):
    sc = install(tmp_path)
    rep = gh.sync(sc, "PTU", root=tmp_path, get=FakeGet({f"{TAG}/build.json": b'{"build": "1.2.3.4"}'}))
    assert "build.json" in rep["error"] and rep["fetched"] == []
    rep = gh.sync(sc, "PTU", root=tmp_path, get=FakeGet({f"{TAG}/build.json": OSError("offline")}))
    assert rep["error"] == "OSError: offline"
    assert "fetch skipped" in gh.summary(rep)
    rep = gh.sync(tmp_path / "none", "PTU", root=tmp_path, get=FakeGet({}))
    assert rep["build"] is None and "no installed build" in rep["error"]


def test_no_tag_yet_only_reports_missing(tmp_path):
    sc = install(tmp_path)
    rep = gh.sync(sc, "PTU", root=tmp_path, get=FakeGet({}))
    assert rep["error"] is None and rep["missing"] == list(gh.DATASETS) and rep["fetched"] == []
    assert "missing" in gh.summary(rep)
