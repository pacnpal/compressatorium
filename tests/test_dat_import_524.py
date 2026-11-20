"""DATs made easy (#524): many DATs at once, zips of them, and Redump sync.

Small local fixture DATs and zips only. The network is never touched: the one
function that talks to redump.org (``dat_import._fetch``) is replaced.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from services import dat_import

from app.routes import dat as dat_routes


def _dat_xml(name: str, game: str, sha1: str, *, homepage: str = "") -> str:
    home = f"<homepage>{homepage}</homepage>" if homepage else ""
    return f"""<?xml version="1.0"?>
<datafile>
  <header><name>{name}</name><description>{name}</description>{home}</header>
  <game name="{game}"><rom name="{game}.bin" size="4" sha1="{sha1}"/></game>
</datafile>
"""


def _zip_bytes(members: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _upload(data: bytes, filename: str):
    upload = MagicMock()
    upload.filename = filename
    upload.read = AsyncMock(side_effect=[data, b""])
    return upload


@pytest.fixture
def store(tmp_path, monkeypatch):
    from services.dat_store import DATStore

    s = DATStore(store_path=str(tmp_path / "dat.db"))
    monkeypatch.setattr("services.dat_store.dat_store", s)
    monkeypatch.setattr(dat_routes, "dat_store", s)
    rematch = AsyncMock()
    monkeypatch.setattr(dat_import, "_rematch", rematch)
    s.rematch = rematch
    return s


@pytest.fixture
def prefs(monkeypatch):
    saved: dict = {}

    async def get(key):
        return saved.get(key)

    async def put(key, value):
        saved[key] = value
        return value

    from services.preferences_store import preferences_store

    monkeypatch.setattr(preferences_store, "get", get)
    monkeypatch.setattr(preferences_store, "put", put)
    return saved


# ---------------------------------------------------------------------------
# Upload: several files, and zips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_upload_imports_several_dats_and_a_pack_zip(store):
    pack = _zip_bytes({
        "No-Intro/Nintendo - Game Boy (20261120).dat": _dat_xml("Nintendo - Game Boy", "G", "a" * 40),
        "No-Intro/Sega - Game Gear (20261120).dat": _dat_xml("Sega - Game Gear", "S", "b" * 40),
        "readme.txt": "not a DAT",
        "__MACOSX/No-Intro/._Sega - Game Gear (20261120).dat": b"\x00\x05junk",
    })
    result = await dat_routes.import_dat_batch(files=[
        _upload(_dat_xml("Sony - PlayStation", "P", "c" * 40).encode(), "psx.dat"),
        _upload(pack, "No-Intro Love Pack.zip"),
    ])
    names = sorted(d["name"] for d in store.list_dats())
    assert names == ["Nintendo - Game Boy", "Sega - Game Gear", "Sony - PlayStation"]
    assert len(result["imported"]) == 3
    assert result["errors"] == []
    # One rematch for the whole batch, not one per DAT.
    assert store.rematch.await_count == 1


@pytest.mark.asyncio
async def test_one_bad_dat_does_not_stop_the_rest(store):
    result = await dat_routes.import_dat_batch(files=[
        _upload(b"not xml", "bad.dat"),
        _upload(_dat_xml("Good", "G", "a" * 40).encode(), "good.dat"),
        _upload(b"whatever", "notes.txt"),
    ])
    assert [d["name"] for d in store.list_dats()] == ["Good"]
    assert {e["file"] for e in result["errors"]} == {"bad.dat", "notes.txt"}


@pytest.mark.asyncio
async def test_reimporting_a_dat_replaces_the_older_copy(store):
    old = _dat_xml("Nintendo - Game Boy", "Old", "a" * 40, homepage="No-Intro")
    new = _dat_xml("Nintendo - Game Boy", "New", "b" * 40, homepage="No-Intro")
    await dat_routes.import_dat_batch(files=[_upload(old.encode(), "gb.dat")])
    await dat_routes.import_dat_batch(files=[_upload(new.encode(), "gb.dat")])
    dats = store.list_dats()
    assert len(dats) == 1
    assert store.lookup_sha1("a" * 40) is None
    assert store.lookup_sha1("b" * 40)["game_name"] == "New"


@pytest.mark.parametrize("member", ["../evil.dat", "/abs/evil.dat", "a\\b.dat"])
@pytest.mark.asyncio
async def test_a_zip_with_an_unsafe_member_name_is_refused_whole(store, member):
    pack = _zip_bytes({
        "fine.dat": _dat_xml("Fine", "F", "a" * 40),
        member: _dat_xml("Evil", "E", "b" * 40),
    })
    result = await dat_routes.import_dat_batch(files=[_upload(pack, "pack.zip")])
    assert store.list_dats() == []
    assert result["errors"] and result["errors"][0]["file"] == "pack.zip"


@pytest.mark.asyncio
async def test_a_member_over_the_dat_size_cap_is_refused(store, monkeypatch):
    monkeypatch.setattr(dat_import, "MAX_DAT_SIZE", 64)
    pack = _zip_bytes({"big.dat": _dat_xml("Big", "B", "a" * 40)})
    result = await dat_routes.import_dat_batch(files=[_upload(pack, "pack.zip")])
    assert store.list_dats() == []
    assert result["errors"]
    # A loose DAT over the cap too, refused before it is parsed.
    loose = _dat_xml("Big", "B", "a" * 40).encode()
    result = await dat_routes.import_dat_batch(files=[_upload(loose, "big.dat")])
    assert store.list_dats() == []
    assert "too large" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_a_zip_bomb_past_the_expansion_ratio_is_refused(store, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "archive_inbox_max_ratio", 5)
    padding = "<!-- " + "x" * 50_000 + " -->"
    pack = _zip_bytes({"pad.dat": _dat_xml("Pad", "P", "a" * 40) + padding})
    result = await dat_routes.import_dat_batch(files=[_upload(pack, "pack.zip")])
    assert store.list_dats() == []
    assert "expand" in result["errors"][0]["error"]


def test_extracted_dats_never_take_the_member_name_on_disk(tmp_path):
    zpath = tmp_path / "p.zip"
    zpath.write_bytes(_zip_bytes({"dir/Some Name.dat": _dat_xml("A", "A", "a" * 40)}))
    out = tmp_path / "out"
    out.mkdir()
    found = dat_import.extract_dats(str(zpath), str(out))
    assert [name for name, _ in found] == ["dir/Some Name.dat"]
    assert [p.name for p in out.iterdir()] == ["0.dat"]


# ---------------------------------------------------------------------------
# Redump sync
# ---------------------------------------------------------------------------


def _redump_zip(name: str, sha1: str) -> bytes:
    return _zip_bytes({f"{name} (20261120).dat": _dat_xml(name, "Disc", sha1, homepage="redump.org")})


@pytest.fixture
def fetches(monkeypatch):
    """The redump.org requests made, in order; each answers a small fixture zip."""
    calls: list[tuple[str, dict]] = []
    bodies = {
        "psx": _redump_zip("Sony - PlayStation", "a" * 40),
        "gc": _redump_zip("Nintendo - GameCube - Datfile", "b" * 40),
    }

    def fake_fetch(system, cached, dest):
        calls.append((system, dict(cached)))
        path = f"{dest}/{system}.zip"
        with open(path, "wb") as fh:
            fh.write(bodies[system])
        return path, {"etag": f'"{system}-1"', "last_modified": None}

    monkeypatch.setattr(dat_import, "_fetch", fake_fetch)
    monkeypatch.setattr(dat_import, "REQUEST_GAP_SECONDS", 0)
    return calls


@pytest.mark.asyncio
async def test_redump_sync_fetches_only_the_platforms_present(store, prefs, fetches, monkeypatch):
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx", "ngc", "gba", None})
    result = await dat_import.sync_redump()
    # gba is a cartridge Platform: Redump has no DAT for it, so nothing is asked.
    assert [system for system, _ in fetches] == ["gc", "psx"]
    assert sorted(d["name"] for d in store.list_dats()) == [
        "Nintendo - GameCube - Datfile", "Sony - PlayStation",
    ]
    assert {s["status"] for s in result["systems"]} == {"imported"}


@pytest.mark.asyncio
async def test_redump_sync_is_cached(store, prefs, fetches, monkeypatch):
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx"})
    await dat_import.sync_redump()
    again = await dat_import.sync_redump()
    # A second sync the same day asks redump.org nothing.
    assert len(fetches) == 1
    assert again["systems"][0]["status"] == "up_to_date"
    # Forced, it asks again, conditionally.
    await dat_import.sync_redump(force=True)
    assert len(fetches) == 2
    assert fetches[1][1].get("etag") == '"psx-1"'
    assert len(store.list_dats()) == 1


@pytest.mark.asyncio
async def test_redump_sync_refetches_when_its_dat_was_deleted(store, prefs, fetches, monkeypatch):
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx"})
    await dat_import.sync_redump()
    await store.delete_dat(store.list_dats()[0]["id"])
    await dat_import.sync_redump()
    assert len(fetches) == 2
    # Not conditional: a 304 would leave the user with no DAT.
    assert fetches[1][1] == {}
    assert len(store.list_dats()) == 1


@pytest.mark.asyncio
async def test_redump_sync_is_one_at_a_time(store, prefs, fetches, monkeypatch):
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx"})
    assert dat_import._sync_running.acquire(blocking=False)
    try:
        with pytest.raises(dat_import.SyncRunning):
            await dat_import.sync_redump()
    finally:
        dat_import._sync_running.release()
    assert fetches == []


def test_redump_urls_are_metadata_only():
    """Only /datfile/: never /keys/, /dkeys/, a -bios set, or a ROM."""
    for system in dat_import.REDUMP_SYSTEMS.values():
        url = dat_import.redump_url(system)
        assert url == f"http://redump.org/datfile/{system}/"
        assert "bios" not in url and "keys" not in url


class _Conn:
    """A stand-in for http.client.HTTPConnection: records what was asked."""

    seen: ClassVar[dict] = {}
    status = 200

    def __init__(self, host, timeout):
        _Conn.seen = {"host": host}

    def request(self, method, path, headers):
        _Conn.seen.update(method=method, path=path, headers=headers)

    def getresponse(self):
        resp = io.BytesIO(b"PK")
        resp.status, resp.reason = _Conn.status, "x"
        resp.headers = {"ETag": '"x"'}
        return resp

    def close(self):
        pass


def test_fetch_asks_redump_with_a_generic_user_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(dat_import.http.client, "HTTPConnection", _Conn)
    _Conn.status = 200
    path, meta = dat_import._fetch("psx", {"etag": '"old"'}, str(tmp_path))
    seen = _Conn.seen
    assert (seen["host"], seen["method"], seen["path"]) == ("redump.org", "GET", "/datfile/psx/")
    assert seen["headers"]["User-Agent"] == dat_import.USER_AGENT
    assert "@" not in dat_import.USER_AGENT
    assert seen["headers"]["If-None-Match"] == '"old"'
    assert meta["etag"] == '"x"'
    assert Path(path).read_bytes() == b"PK"


def test_fetch_refuses_anything_but_200_and_304(monkeypatch, tmp_path):
    monkeypatch.setattr(dat_import.http.client, "HTTPConnection", _Conn)
    _Conn.status = 304
    assert dat_import._fetch("psx", {}, str(tmp_path)) == (None, {})
    # A redirect is not followed: it is refused like any other answer.
    _Conn.status = 302
    with pytest.raises(ValueError):
        dat_import._fetch("psx", {}, str(tmp_path))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_dat_import_and_sync(store, prefs, fetches, monkeypatch, tmp_path, capsys):
    import cli

    monkeypatch.setattr(cli, "_open_db", lambda dry_run: True)
    monkeypatch.setattr(dat_import, "present_platforms", lambda: set())
    loose = tmp_path / "gb.dat"
    loose.write_text(_dat_xml("Nintendo - Game Boy", "G", "a" * 40))
    pack = tmp_path / "pack.zip"
    pack.write_bytes(_zip_bytes({"gg.dat": _dat_xml("Sega - Game Gear", "S", "b" * 40)}))
    assert cli.main(["dat", "import", str(loose), str(pack)]) == cli.EXIT_OK
    assert len(store.list_dats()) == 2

    assert cli.main(["dat", "sync", "--platform", "psx"]) == cli.EXIT_OK
    assert [system for system, _ in fetches] == ["psx"]
    assert len(store.list_dats()) == 3

    bad = tmp_path / "bad.dat"
    bad.write_text("nope")
    assert cli.main(["dat", "import", str(bad)]) == cli.EXIT_FAILED
    assert cli.main(["dat", "import", str(tmp_path / "missing.dat")]) == cli.EXIT_USAGE


def test_present_platforms_reads_every_library_and_skips_one_that_is_down(monkeypatch):
    from services import libraries
    from services.rules.library import LibraryUnreadable, Platform

    class Lib:
        def __init__(self, lib_id, slugs, *, configured=True, down=False):
            self.id, self._slugs, self._configured, self._down = lib_id, slugs, configured, down

        def configured(self):
            return self._configured

        def platforms(self):
            if self._down:
                raise LibraryUnreadable("RomM is down")
            return [Platform(id=s, slug=s, name=s) for s in self._slugs]

    monkeypatch.setattr(libraries, "all_libraries", lambda: [
        Lib("romm", ["ps2"], down=True),
        Lib("off", ["xbox"], configured=False),
        Lib("1", ["gc", "gba"]),
    ])
    assert dat_import.present_platforms() == {"ngc", "gba"}
