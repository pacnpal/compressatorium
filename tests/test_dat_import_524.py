"""DATs made easy (#524): many DATs at once, zips of them, and Redump sync.

Small local fixture DATs and zips only. The network is never touched: the one
function that talks to redump.org (``dat_import._fetch``) is replaced.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
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


def _upload(data: bytes, filename: str) -> tuple[str, tuple[str, bytes]]:
    return ("files", (filename, data))


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(dat_routes.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _batch(files: list) -> dict:
    """POST *files* to the batch route as a real multipart body."""
    async with _client() as client:
        response = await client.post("/api/dat/import-batch", files=files)
    assert response.status_code == 200, response.text
    return response.json()


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
        "No-Intro/Nintendo - Game Boy (20261120).dat": _dat_xml(
            "Nintendo - Game Boy", "G", "a" * 40,
        ),
        "No-Intro/Sega - Game Gear (20261120).dat": _dat_xml("Sega - Game Gear", "S", "b" * 40),
        "readme.txt": "not a DAT",
        "__MACOSX/No-Intro/._Sega - Game Gear (20261120).dat": b"\x00\x05junk",
    })
    result = await _batch([
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
    result = await _batch([
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
    await _batch([_upload(old.encode(), "gb.dat")])
    await _batch([_upload(new.encode(), "gb.dat")])
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
    result = await _batch([_upload(pack, "pack.zip")])
    assert store.list_dats() == []
    assert result["errors"] and result["errors"][0]["file"] == "pack.zip"


@pytest.mark.asyncio
async def test_a_member_over_the_dat_size_cap_is_refused(store, monkeypatch):
    monkeypatch.setattr(dat_import, "MAX_DAT_SIZE", 64)
    pack = _zip_bytes({"big.dat": _dat_xml("Big", "B", "a" * 40)})
    result = await _batch([_upload(pack, "pack.zip")])
    assert store.list_dats() == []
    assert result["errors"]
    # A loose DAT over the cap too, refused before it is parsed.
    loose = _dat_xml("Big", "B", "a" * 40).encode()
    result = await _batch([_upload(loose, "big.dat")])
    assert store.list_dats() == []
    assert "too large" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_a_zip_bomb_past_the_expansion_ratio_is_refused(store, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "archive_inbox_max_ratio", 5)
    padding = "<!-- " + "x" * 50_000 + " -->"
    pack = _zip_bytes({"pad.dat": _dat_xml("Pad", "P", "a" * 40) + padding})
    result = await _batch([_upload(pack, "pack.zip")])
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
    return _zip_bytes(
        {f"{name} (20261120).dat": _dat_xml(name, "Disc", sha1, homepage="redump.org")},
    )


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
        return path, {"etag": f'"{system}-1"', "last_modified": None, "transport": "https"}

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
@pytest.mark.parametrize(("meta", "transport"), [
    ({"transport": "https"}, "https"),
    ({"transport": "http"}, "http"),
    # A fetch that doesn't say how it arrived is treated as plain HTTP.
    ({}, "http"),
])
async def test_redump_sync_records_how_each_dat_arrived(
    store, prefs, monkeypatch, tmp_path, meta, transport,
):
    """the Recognized delete proof must be able to tell a
    DAT fetched over plain HTTP from one fetched over HTTPS."""
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx"})
    monkeypatch.setattr(dat_import, "REQUEST_GAP_SECONDS", 0)
    body = _redump_zip("Sony - PlayStation", "a" * 40)

    def fake_fetch(system, cached, dest):
        path = f"{dest}/{system}.zip"
        with open(path, "wb") as fh:
            fh.write(body)
        return path, {"etag": None, "last_modified": None, **meta}

    monkeypatch.setattr(dat_import, "_fetch", fake_fetch)
    await dat_import.sync_redump()
    assert [d["transport"] for d in store.list_dats()] == [transport]
    assert store.lookup_sha1("a" * 40)["transport"] == transport


def _transport_fetch(monkeypatch, body: bytes, scheme: list[str], asked: list[dict]):
    """``_fetch`` answering *body* over ``scheme[0]``, logging what it was sent."""

    def fake_fetch(system, cached, dest):
        asked.append(dict(cached))
        path = f"{dest}/{system}.zip"
        with open(path, "wb") as fh:
            fh.write(body)
        return path, {"etag": '"e"', "last_modified": None, "transport": scheme[0]}

    monkeypatch.setattr(dat_import, "_fetch", fake_fetch)
    monkeypatch.setattr(dat_import, "REQUEST_GAP_SECONDS", 0)
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx"})


@pytest.mark.asyncio
async def test_identical_bytes_over_https_upgrade_a_dat_fetched_over_http(
    store, prefs, monkeypatch,
):
    """the same bytes again over HTTPS prove the plain-HTTP
    download was genuine, so it may vouch from now on. A DAT fetched over
    HTTP is asked for again unconditionally, or a 304 would keep it untrusted."""
    scheme, asked = ["http"], []
    _transport_fetch(monkeypatch, _redump_zip("Sony - PlayStation", "a" * 40), scheme, asked)
    await dat_import.sync_redump()
    assert [d["transport"] for d in store.list_dats()] == ["http"]

    scheme[0] = "https"
    result = await dat_import.sync_redump(force=True)

    assert asked[1] == {}  # unconditional: no If-None-Match for an HTTP DAT
    assert result["systems"] == [{"system": "psx", "status": "up_to_date"}]
    assert [d["transport"] for d in store.list_dats()] == ["https"]
    # Now trusted, the next ask is conditional again.
    await dat_import.sync_redump(force=True)
    assert asked[2].get("etag") == '"e"'


@pytest.mark.asyncio
async def test_identical_bytes_over_http_never_downgrade_a_trusted_dat(
    store, prefs, monkeypatch,
):
    scheme, asked = ["https"], []
    _transport_fetch(monkeypatch, _redump_zip("Sony - PlayStation", "a" * 40), scheme, asked)
    await dat_import.sync_redump()
    scheme[0] = "http"
    await dat_import.sync_redump(force=True)
    assert [d["transport"] for d in store.list_dats()] == ["https"]


@pytest.mark.asyncio
async def test_upgrading_never_touches_a_dat_the_sync_did_not_fetch(store, prefs, monkeypatch):
    """Only the http rows the cache names for that system are upgraded."""
    loose = _dat_xml("Nintendo - Game Boy", "G", "b" * 40)
    await store.import_dat(loose, transport="http")
    scheme, asked = ["http"], []
    _transport_fetch(monkeypatch, _redump_zip("Sony - PlayStation", "a" * 40), scheme, asked)
    await dat_import.sync_redump()
    scheme[0] = "https"
    await dat_import.sync_redump(force=True)
    by_name = {d["name"]: d["transport"] for d in store.list_dats()}
    assert by_name == {"Nintendo - Game Boy": "http", "Sony - PlayStation": "https"}


@pytest.mark.asyncio
async def test_a_dat_imported_by_hand_is_recorded_as_manual(store, prefs, tmp_path):
    loose = tmp_path / "gb.dat"
    loose.write_text(_dat_xml("Nintendo - Game Boy", "G", "a" * 40))
    await dat_import.import_files([("gb.dat", str(loose))])
    assert [d["transport"] for d in store.list_dats()] == ["manual"]


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
        assert url == f"https://redump.org/datfile/{system}/"
        assert "bios" not in url and "keys" not in url
        # The plain-HTTP fallback (#556) asks the same path on the same host.
        assert dat_import.redump_url(system, "http") == f"http://redump.org/datfile/{system}/"


class _Conn:
    """A stand-in for http.client.HTTPSConnection: records what was asked."""

    seen: ClassVar[dict] = {}
    status = 200

    def __init__(self, host, timeout, context):
        _Conn.seen = {"host": host}

    def connect(self):
        pass

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
    monkeypatch.setattr(dat_import.http.client, "HTTPSConnection", _Conn)
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
    monkeypatch.setattr(dat_import.http.client, "HTTPSConnection", _Conn)
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


@pytest.mark.asyncio
async def test_a_zip_whose_data_is_corrupt_is_refused_alone(store, tmp_path):
    """Codex PR #527: corrupt deflate data is one zip's refusal, not the
    batch's failure."""
    bad = tmp_path / "bad.zip"
    data = bytearray(_zip_bytes({"x.dat": _dat_xml("Broken", "B", "d" * 40) * 50}))
    start = data.index(b"x.dat") + len(b"x.dat")
    for i in range(start + 8, start + 40):
        data[i] ^= 0xFF
    bad.write_bytes(bytes(data))
    good = tmp_path / "gb.dat"
    good.write_text(_dat_xml("Nintendo - Game Boy", "G", "a" * 40))

    result = await dat_import.import_files([("bad.zip", str(bad)), ("gb.dat", str(good))])

    assert [d["name"] for d in result["imported"]] == ["Nintendo - Game Boy"]
    assert [e["file"] for e in result["errors"]] == ["bad.zip"]


def test_cli_dat_sync_reads_romms_saved_settings_first(store, prefs, fetches, monkeypatch):
    """Codex PR #527: RomM set up in the Web UI is in the database; the sync
    must read it before it asks which consoles are present."""
    import cli
    from services.romm import settings as romm_settings

    order: list[str] = []

    async def load(*_args, **_kwargs):
        order.append("load")
        return {}

    def present():
        order.append("present")
        return set()

    monkeypatch.setattr(cli, "_open_db", lambda dry_run: True)
    monkeypatch.setattr(romm_settings, "load", load)
    monkeypatch.setattr(dat_import, "present_platforms", present)

    assert cli.main(["dat", "sync"]) == cli.EXIT_OK
    assert order[:2] == ["load", "present"]


def test_cli_dat_import_rematches_and_waits_for_it(store, prefs, monkeypatch, tmp_path):
    """Codex PR #527: a DAT imported from the CLI refreshes the files it now
    knows (a remote hit upgrades to the DAT's name), and the CLI waits for
    that local-only rematch before it exits."""
    import cli

    waited = AsyncMock()
    monkeypatch.setattr(cli, "_open_db", lambda dry_run: True)
    monkeypatch.setattr("routes.dat.wait_for_match_jobs", waited, raising=False)
    loose = tmp_path / "gb.dat"
    loose.write_text(_dat_xml("Nintendo - Game Boy", "G", "a" * 40))

    assert cli.main(["dat", "import", str(loose)]) == cli.EXIT_OK
    store.rematch.assert_awaited_once()
    waited.assert_awaited_once()


@pytest.mark.asyncio
async def test_waiting_for_match_jobs_waits_for_one_started_in_turn(monkeypatch):
    """A finishing job may start a deferred rematch; the wait covers it."""
    import asyncio

    import routes.dat as live_routes

    done: list[str] = []

    async def second():
        await asyncio.sleep(0.01)
        done.append("second")

    async def first():
        await asyncio.sleep(0.01)
        live_routes._background_match_tasks.add(asyncio.create_task(second()))
        done.append("first")

    live_routes._background_match_tasks.add(asyncio.create_task(first()))
    await live_routes.wait_for_match_jobs()

    assert done == ["first", "second"]
    live_routes._background_match_tasks.clear()


# ---------------------------------------------------------------------------
# Review findings on PR #527
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The Redump cache trusts what is installed, not a DAT's name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_a_redump_dat_replaced_by_hand_is_restored(
    store, prefs, fetches, monkeypatch, force,
):
    """Codex + CodeRabbit PR #527: a DAT of the same name and family imported
    by hand replaces the synced one; the next sync must not call it current."""
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx"})
    await dat_import.sync_redump()
    assert store.lookup_sha1("a" * 40)

    older = _dat_xml("Sony - PlayStation", "Old", "e" * 40, homepage="redump.org")
    await dat_import.import_files([("old.dat", _write(older))])
    assert store.lookup_sha1("a" * 40) is None

    result = await dat_import.sync_redump(force=force)

    assert len(fetches) == 2
    assert fetches[1][1] == {}, "a conditional request could answer 304 and restore nothing"
    assert result["systems"][0]["status"] == "imported"
    assert store.lookup_sha1("a" * 40), "the synced DAT was not restored"


def _write(text: str) -> str:
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".dat")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


# ---------------------------------------------------------------------------
# The batch upload is bounded before it is parsed
# ---------------------------------------------------------------------------

_BATCH_BOUNDARY = "datbatch"
_SLICE = 64 * 1024


class _Body:
    """A multipart body of one ``files`` part of *size* bytes, a slice at a
    time; ``sent`` is how much the server pulled."""

    def __init__(self, size: int) -> None:
        self.head = (
            f"--{_BATCH_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="files"; filename="big.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        self.tail = f"\r\n--{_BATCH_BOUNDARY}--\r\n".encode()
        self.size = size
        self.sent = 0

    @property
    def length(self) -> int:
        return len(self.head) + self.size + len(self.tail)

    async def __aiter__(self):
        self.sent += len(self.head)
        yield self.head
        left = self.size
        while left > 0:
            chunk = b"\x00" * min(_SLICE, left)
            left -= len(chunk)
            self.sent += len(chunk)
            yield chunk
        self.sent += len(self.tail)
        yield self.tail


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
async def test_an_oversized_batch_is_cut_off_as_it_arrives(
    store, monkeypatch, tmp_path, declared,
):
    """CodeRabbit PR #527: the body is capped while it arrives, never spooled whole."""
    import tempfile

    monkeypatch.setattr(dat_import, "MAX_BATCH_SIZE", 1000)
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    body = _Body(8 * 1024 * 1024)
    headers = {"Content-Type": f"multipart/form-data; boundary={_BATCH_BOUNDARY}"}
    if declared:
        headers["Content-Length"] = str(body.length)

    async with _client() as client:
        response = await client.post("/api/dat/import-batch", content=body, headers=headers)

    assert response.status_code == 413, response.text
    assert body.sent <= 1000 + 128 * 1024
    if declared:
        assert body.sent == 0
    assert store.list_dats() == []
    assert list(spool.iterdir()) == []


@pytest.mark.asyncio
async def test_a_batch_reads_every_file_part(store):
    files = [
        ("files", ("gb.dat", _dat_xml("Nintendo - Game Boy", "G", "a" * 40).encode())),
        ("files", ("pack.zip", _zip_bytes({
            "gg.dat": _dat_xml("Sega - Game Gear", "S", "b" * 40),
        }))),
        ("files", ("notes.txt", b"hello")),
    ]
    async with _client() as client:
        response = await client.post("/api/dat/import-batch", files=files)
    assert response.status_code == 200, response.text
    assert sorted(d["name"] for d in store.list_dats()) == [
        "Nintendo - Game Boy", "Sega - Game Gear",
    ]
    assert [e["file"] for e in response.json()["errors"]] == ["notes.txt"]


# ---------------------------------------------------------------------------
# One loose DAT: the size cap, and a read failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_loose_dat_over_the_cap_is_refused_before_it_is_parsed(
    store, monkeypatch, tmp_path,
):
    """CodeRabbit PR #527: the CLI and the Redump bare-DAT path skip the upload cap."""
    monkeypatch.setattr(dat_import, "MAX_DAT_SIZE", 64)
    big = tmp_path / "big.dat"
    big.write_text(_dat_xml("Big", "B", "a" * 40))
    result = await dat_import.import_files([("big.dat", str(big))])
    assert store.list_dats() == []
    assert [e["file"] for e in result["errors"]] == ["big.dat"]
    assert "too large" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_an_unreadable_dat_does_not_stop_the_batch(store, monkeypatch, tmp_path):
    """CodeRabbit PR #527: a PermissionError is that file's error, not the batch's."""
    from services import dat_store as dat_store_module

    locked = tmp_path / "locked.dat"
    locked.write_text(_dat_xml("Locked", "L", "a" * 40))
    good = tmp_path / "good.dat"
    good.write_text(_dat_xml("Good", "G", "b" * 40))
    real = dat_store_module.parse_dat

    def parse(source):
        if str(source) == str(locked):
            raise PermissionError(13, "Permission denied", str(locked))
        return real(source)

    monkeypatch.setattr(dat_store_module, "parse_dat", parse)
    result = await dat_import.import_files(
        [("locked.dat", str(locked)), ("good.dat", str(good))],
    )

    assert [d["name"] for d in result["imported"]] == ["Good"]
    assert [e["file"] for e in result["errors"]] == ["locked.dat"]


# ---------------------------------------------------------------------------
# A partly imported Redump system still says the DATs changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_partly_imported_system_reports_the_change(store, prefs, fetches, monkeypatch):
    """CodeRabbit PR #527: the UI refreshes on ``changed``, not on the count of
    systems that fully imported."""
    monkeypatch.setattr(dat_import, "present_platforms", lambda: {"psx"})
    body = _zip_bytes({
        "good.dat": _dat_xml("Sony - PlayStation", "Disc", "a" * 40, homepage="redump.org"),
        "bad.dat": "not xml",
    })

    def fake_fetch(system, cached, dest):
        path = f"{dest}/{system}.zip"
        with open(path, "wb") as fh:
            fh.write(body)
        return path, {}

    monkeypatch.setattr(dat_import, "_fetch", fake_fetch)
    result = await dat_import.sync_redump()

    assert result["systems"][0]["status"] == "failed"
    assert result["imported"] == 0
    assert result["changed"] is True
    assert (await dat_import.sync_redump(force=True))["changed"] is True
    monkeypatch.setattr(dat_import, "present_platforms", set)
    assert (await dat_import.sync_redump())["changed"] is False


# ---------------------------------------------------------------------------
# The verdict stamp sees a DAT change made by another process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dat_imported_by_another_process_retires_the_stamp(tmp_path):
    """CodeRabbit PR #527: ``compressatorium dat import`` runs in its own
    process, with its own in-memory counter. Two stores on one database stand
    in for the server and the CLI."""
    from services.dat_store import INDEX_GENERATION_KEY, DATStore

    db = str(tmp_path / "shared.db")
    server, cli_process = DATStore(store_path=db), DATStore(store_path=db)
    path = "/vol/x.gba"
    stamp = server.index_generation()

    await cli_process.import_dat(_write(_dat_xml("Unrelated", "U", "a" * 40)))
    assert server.index_generation() != stamp

    miss = {"path": path, "matched": False, "checked_remote": None, INDEX_GENERATION_KEY: stamp}
    assert list(await server.set_match(path, miss)) == [path]
    assert server.get_match(path) is None
    # A verdict stamped after the change is current.
    fresh = {**miss, INDEX_GENERATION_KEY: server.index_generation()}
    assert not await server.set_match(path, fresh)


# ---------------------------------------------------------------------------
# Redump: verified HTTPS first, plain HTTP only when HTTPS can't connect (#556)
# ---------------------------------------------------------------------------


class _Wire:
    """A stand-in for http.client's connections: logs each one made.

    ``fail`` maps a scheme to the error its ``connect()`` raises; ``status``
    is the answer every request gets.
    """

    scheme = "?"
    log: ClassVar[list] = []
    fail: ClassVar[dict] = {}
    status = 200

    def __init__(self, host, timeout, context=None):
        self.entry = {"scheme": self.scheme, "host": host, "context": context}
        _Wire.log.append(self.entry)

    def connect(self):
        if self.scheme in _Wire.fail:
            raise _Wire.fail[self.scheme]
        self.entry["connected"] = True

    def request(self, method, path, headers):
        self.entry.update(method=method, path=path, headers=headers)

    def getresponse(self):
        resp = io.BytesIO(b"PK")
        resp.status, resp.reason = _Wire.status, "x"
        resp.headers = {"ETag": '"e"'}
        return resp

    def close(self):
        self.entry["closed"] = True


class _Https(_Wire):
    scheme = "https"


class _Http(_Wire):
    scheme = "http"


@pytest.fixture
def wire(monkeypatch):
    _Wire.log, _Wire.fail, _Wire.status = [], {}, 200
    monkeypatch.setattr(dat_import.http.client, "HTTPSConnection", _Https)
    monkeypatch.setattr(dat_import.http.client, "HTTPConnection", _Http)
    return _Wire


def test_redump_is_asked_over_verified_https_first(wire, tmp_path):
    """Codex security P1 on PR #527 still holds where it can: verified HTTPS
    is tried first, and when it works plain HTTP is never asked."""
    import ssl

    path, meta = dat_import._fetch("psx", {}, str(tmp_path))
    assert [e["scheme"] for e in wire.log] == ["https"]
    asked = wire.log[0]
    assert (asked["host"], asked["method"], asked["path"]) == (
        "redump.org", "GET", "/datfile/psx/")
    context = asked["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert Path(path).read_bytes() == b"PK"
    assert meta["etag"] == '"e"'
    assert meta["transport"] == "https"


@pytest.mark.parametrize("error", [
    ConnectionRefusedError(61, "Connection refused"),
    OSError(51, "Network is unreachable"),
    TimeoutError("timed out"),
], ids=["refused", "unreachable", "timeout"])
def test_https_that_cannot_connect_falls_back_to_plain_http(wire, tmp_path, error):
    """redump.org refuses port 443 (#556): the same path, the same host and
    the same headers are asked over plain HTTP instead."""
    wire.fail = {"https": error}
    cached = {"etag": '"old"', "last_modified": "Sat, 21 Nov 2026 00:00:00 GMT"}
    path, meta = dat_import._fetch("psx", cached, str(tmp_path))
    assert [e["scheme"] for e in wire.log] == ["https", "http"]
    https, http = wire.log
    assert https["closed"]
    assert (http["host"], http["method"], http["path"]) == ("redump.org", "GET", "/datfile/psx/")
    assert http["headers"] == {
        "User-Agent": dat_import.USER_AGENT,
        "If-None-Match": '"old"',
        "If-Modified-Since": cached["last_modified"],
    }
    assert http["closed"]
    assert Path(path).read_bytes() == b"PK"
    assert meta["etag"] == '"e"'
    # Recorded with the DAT, so it can never vouch for a delete (4659176015).
    assert meta["transport"] == "http"


@pytest.mark.parametrize("error", [
    "SSLCertVerificationError", "CertificateError", "SSLError",
])
def test_a_bad_certificate_does_not_fall_back(wire, tmp_path, error):
    """Something answers on 443 with a certificate that doesn't check out:
    fail closed rather than ask the same thing in plaintext."""
    import ssl

    wire.fail = {"https": getattr(ssl, error)("certificate verify failed")}
    with pytest.raises(ValueError, match="certificate") as caught:
        dat_import._fetch("psx", {}, str(tmp_path))
    assert "not retried over plain HTTP" in str(caught.value)
    assert [e["scheme"] for e in wire.log] == ["https"]


def test_an_https_answer_that_breaks_after_connecting_does_not_fall_back(
    wire, tmp_path, monkeypatch,
):
    """Only a connection that can't be made at all falls back."""

    class Dropped(_Https):
        def getresponse(self):
            raise ConnectionResetError(54, "Connection reset by peer")

    monkeypatch.setattr(dat_import.http.client, "HTTPSConnection", Dropped)
    with pytest.raises(ValueError, match="over HTTPS"):
        dat_import._fetch("psx", {}, str(tmp_path))
    assert [e["scheme"] for e in wire.log] == ["https"]


def test_no_answer_on_either_transport_says_both_were_tried(wire, tmp_path):
    wire.fail = {
        "https": ConnectionRefusedError(61, "Connection refused"),
        "http": TimeoutError("timed out"),
    }
    with pytest.raises(ValueError, match="over HTTPS .* or plain HTTP") as caught:
        dat_import._fetch("psx", {}, str(tmp_path))
    assert "Connection refused" in str(caught.value) and "timed out" in str(caught.value)


def test_plain_http_keeps_the_same_caps_and_refuses_redirects(wire, tmp_path, monkeypatch):
    """The fallback is held to the size cap (and names the URL it used), and
    follows no redirect."""
    wire.fail = {"https": ConnectionRefusedError(61, "Connection refused")}
    monkeypatch.setattr(dat_import, "MAX_ZIP_SIZE", 1)
    with pytest.raises(ValueError, match="http://redump.org/datfile/psx/"):
        dat_import._fetch("psx", {}, str(tmp_path))
    assert not (tmp_path / "psx.zip").exists()
    monkeypatch.setattr(dat_import, "MAX_ZIP_SIZE", 1024)
    wire.status = 302
    with pytest.raises(ValueError, match="302"):
        dat_import._fetch("psx", {}, str(tmp_path))
