"""Issue #492: a DAT import during a running rematch must not lose a file.

Two ways it did:

1. A running local-only rematch decided a miss against the old index and wrote
   it after a later import had committed and taken its rematch set. Only
   verdicts the remote source took part in were re-checked at the write, so
   the stale miss was written and nothing ever rematched it.
2. A hit written between an import's pre-change snapshot and its commit was
   deleted by that import's invalidation, while being in neither the snapshot
   nor the surviving rows, so no rematch covered it.

Each test holds the race at a seam (an awaited event) instead of sleeping.
"""

import asyncio
import hashlib

import pytest

from app.routes import dat as dat_routes
from tests.test_dat_routes import _make_upload_file

DAT_TEMPLATE = """\
<?xml version="1.0"?>
<datafile>
  <header>
    <name>{name}</name>
    <description>{name}</description>
    <version>1.0</version>
  </header>
  <game name="{game}">
    <rom name="{rom}" size="{size}" sha1="{sha1}"/>
  </game>
</datafile>
"""


def _one_entry_dat(name: str, game: str, rom: str, content: bytes) -> str:
    return DAT_TEMPLATE.format(
        name=name, game=game, rom=rom, size=len(content),
        sha1=hashlib.sha1(content).hexdigest(),
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh store wired into the routes, with an idle, local-only matcher."""
    from services.dat_store import DATStore

    fresh = DATStore(store_path=str(tmp_path / "dat_store.db"))
    monkeypatch.setattr(dat_routes, "dat_store", fresh)
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)
    monkeypatch.setattr(dat_routes, "_deferred_rematch_paths", set())
    monkeypatch.setattr(dat_routes, "_background_match_tasks", set())
    monkeypatch.setattr(dat_routes.hasheous, "enabled", lambda: False)
    return fresh


async def _import(xml: str) -> dict:
    return await dat_routes.import_dat(file=_make_upload_file(xml))


async def _drain_match_jobs() -> None:
    """Wait until no match job is left, including any deferred successor."""
    for _ in range(20):
        live = [t for t in dat_routes._background_match_tasks if not t.done()]
        if not live:
            await asyncio.sleep(0)
            if not any(not t.done() for t in dat_routes._background_match_tasks):
                return
            continue
        await asyncio.wait_for(asyncio.gather(*live), timeout=10)
    raise AssertionError("match jobs kept rescheduling themselves")


def _row(store, path: str) -> dict | None:
    """The cached payload for *path*, read from the table itself."""
    from services import db as _db

    with store._session() as session:
        row = session.get(_db.DATMatch, path)
        return None if row is None else dict(row.payload)


@pytest.mark.asyncio
async def test_a_stale_local_miss_written_mid_rematch_is_rematched(tmp_path, store):
    """Acceptance 1: hold a local-only rematch between deciding and writing.

    The rematch the first import starts decides ``b.gba`` is a miss against an
    index that does not know it yet. A second DAT, which does, commits while
    the job is held. When the job writes, the file must not be left as a
    cached miss: it ends matched to the DAT that covers it.
    """
    a, b = tmp_path / "a.gba", tmp_path / "b.gba"
    a.write_bytes(b"alpha")
    b.write_bytes(b"bravo")
    a_path, b_path = str(a), str(b)

    # Both files start with a cached miss, as after an ordinary first scan.
    for path in (a_path, b_path):
        await store.set_match(path, {"path": path, "matched": False})

    decided = asyncio.Event()
    release = asyncio.Event()
    real_set_match = store.set_match

    async def held_set_match(path, match):
        if path == b_path and not decided.is_set():
            decided.set()
            await release.wait()
        return await real_set_match(path, match)

    store.set_match = held_set_match

    # Import 1 covers a.gba and starts the rematch of both files.
    await _import(_one_entry_dat("DAT A", "Game A", "a.gba", b"alpha"))
    await asyncio.wait_for(decided.wait(), timeout=10)
    assert not store.get_match(b_path), "b.gba should not be cached yet"

    # Import 2 covers b.gba and commits while the job holds b's old verdict.
    await _import(_one_entry_dat("DAT B", "Game B", "b.gba", b"bravo"))

    release.set()
    await _drain_match_jobs()

    b_row = _row(store, b_path)
    assert b_row is not None, "b.gba was dropped out of every rematch"
    assert b_row.get("matched"), f"b.gba was left as a stale cached miss: {b_row}"
    assert b_row.get("game_name") == "Game B"
    a_row = _row(store, a_path)
    assert a_row is not None and a_row.get("game_name") == "Game A"


@pytest.mark.asyncio
async def test_a_hit_written_between_snapshot_and_commit_is_rematched(
    tmp_path, store, monkeypatch,
):
    """Acceptance 2: a hit written between two imports is in the second's rematch.

    The second import snapshots the match cache, then a running match writes a
    DAT-derived hit, then the import commits and its invalidation deletes that
    hit. The row is in neither the snapshot nor the survivors, so the file has
    to come from what the invalidation itself dropped.
    """
    a = tmp_path / "a.gba"
    a.write_bytes(b"alpha")
    a_path = str(a)
    await _import(_one_entry_dat("DAT A", "Game A", "a.gba", b"alpha"))
    await _drain_match_jobs()

    scheduled: list[list[str]] = []

    async def record_schedule(paths, **_kwargs):
        scheduled.append(list(paths))
        return "job-recorded"

    monkeypatch.setattr(dat_routes, "schedule_match_job", record_schedule)

    real_import = store.import_dat

    async def import_after_a_late_write(source, **kwargs):
        # The route has taken its snapshot by now. A match job in flight
        # decides a.gba against the current index and writes the hit.
        hit = await dat_routes._match_single_file(a_path)
        assert hit.get("matched"), hit
        await store.set_match(a_path, hit)
        assert a_path in store.list_match_paths()
        return await real_import(source, **kwargs)

    store.import_dat = import_after_a_late_write

    await _import(_one_entry_dat("DAT B", "Game B", "b.gba", b"bravo"))

    assert a_path not in store.list_match_paths(), "the import should drop the hit"
    assert scheduled, "the second import scheduled no rematch at all"
    assert a_path in scheduled[-1], (
        f"a.gba fell out of every rematch set: {scheduled}"
    )


@pytest.mark.asyncio
async def test_an_empty_rematch_set_is_logged(store, caplog):
    """An empty set used to return "none" silently, which hid the bug above."""
    with caplog.at_level("INFO", logger="compressatorium.dat"):
        status, job_id = await dat_routes.rematch_after_dat_change(
            [], source="import_dat",
        )
    assert (status, job_id) == ("none", None)
    assert any(
        "import_dat" in r.getMessage() and "nothing to rematch" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# The write boundary
# ---------------------------------------------------------------------------


def _miss(path: str, generation, **extra) -> dict:
    from services.dat_store import INDEX_GENERATION_KEY

    return {
        "path": path, "matched": False, "checked_remote": None,
        INDEX_GENERATION_KEY: generation, **extra,
    }


@pytest.mark.asyncio
async def test_a_local_verdict_from_an_older_index_is_refused_and_reported(store):
    path = "/vol/x.gba"
    before = store.index_generation()
    await store.import_dat(_one_entry_dat("Unrelated", "U", "u.gba", b"u"))
    assert store.index_generation() != before

    owed = await store.set_match(path, _miss(path, before))

    assert list(owed) == [path], "the stale verdict was not reported as owed"
    assert _row(store, path) is None, "a verdict from an older index was written"


@pytest.mark.asyncio
async def test_a_local_verdict_from_the_current_index_is_written(store):
    path = "/vol/x.gba"
    owed = await store.set_match(path, _miss(path, store.index_generation()))
    assert not owed
    row = _row(store, path)
    assert row is not None and row["matched"] is False
    from services.dat_store import INDEX_GENERATION_KEY
    assert INDEX_GENERATION_KEY not in row, "the generation is a check input, not a field"


@pytest.mark.asyncio
async def test_no_verdict_is_current_while_an_index_change_is_in_flight(store):
    """A match that starts mid-import cannot know which index it read."""
    with store._changing_local_index():
        assert store.index_generation() is None
        path = "/vol/x.gba"
        assert list(await store.set_match(path, _miss(path, None))) == [path]
    assert store.index_generation() is not None


@pytest.mark.asyncio
async def test_a_remote_verdict_survives_an_unrelated_import(store):
    """The reason a generation was never used for remote verdicts still holds.

    A stamped remote miss the new DAT knows nothing about stays writable after
    an unrelated import: refusing it would send the file back to the remote
    source for nothing. Only the hash re-check decides for remote verdicts.
    """
    path = "/vol/remote.chd"
    before = store.index_generation()
    await store.import_dat(_one_entry_dat("Unrelated", "U", "u.gba", b"u"))
    miss = _miss(
        path, before, checked_remote="https://hasheous.example",
        candidate_hashes=[("d" * 40, "file_sha1")],
    )

    assert not await store.set_match(path, miss)
    row = _row(store, path)
    assert row is not None and row["checked_remote"] == "https://hasheous.example"


@pytest.mark.asyncio
async def test_an_index_change_that_starts_before_the_write_lock_rolls_it_back(
    store, monkeypatch,
):
    """The generation is re-read once the write holds SQLite's write lock.

    Simulated by a generation that moves between the up-front read and the
    re-read: everything the call staged is rolled back and reported as owed.
    """
    path_a, path_b = "/vol/a.gba", "/vol/b.gba"
    current = store.index_generation()
    readings = iter([current, current + 1])
    monkeypatch.setattr(store, "index_generation", lambda: next(readings))

    owed = await store.set_matches_batch({
        path_a: _miss(path_a, current), path_b: _miss(path_b, current),
    })

    assert sorted(owed) == [path_a, path_b]
    assert _row(store, path_a) is None and _row(store, path_b) is None


@pytest.mark.asyncio
async def test_an_unstamped_write_is_not_generation_checked(store):
    """A caller that makes no claim about the index keeps the old behaviour."""
    path = "/vol/x.gba"
    await store.import_dat(_one_entry_dat("Unrelated", "U", "u.gba", b"u"))
    assert not await store.set_match(path, {"path": path, "matched": False})
    assert _row(store, path) is not None


@pytest.mark.asyncio
async def test_the_invalidation_reports_what_it_dropped(store):
    local_miss, remote_hit = "/vol/miss.gba", "/vol/remote.chd"
    await store.set_match(local_miss, {"path": local_miss, "matched": False})
    await store.set_match(remote_hit, {
        "path": remote_hit, "matched": True, "game_name": "Remote",
        "match_type": "file_sha1", "file_hash": "e" * 40, "source": "hasheous",
    })

    dropped: set[str] = set()
    await store.import_dat(
        _one_entry_dat("Some DAT", "S", "s.gba", b"s"), invalidated=dropped,
    )
    assert dropped == {local_miss}, dropped

    staged: set[str] = set()
    await store.set_match(local_miss, {"path": local_miss, "matched": False})
    await store.import_dat_no_persist(_one_entry_dat("Next", "N", "n.gba", b"n"))
    await store.persist(invalidated=staged)
    assert staged == {local_miss}, staged


# ---------------------------------------------------------------------------
# Every writer hands a refused verdict to a rematch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_batch_route_queues_a_refused_verdict(tmp_path, store, monkeypatch):
    target = tmp_path / "x.gba"
    target.write_bytes(b"xray")
    path = str(target)
    await store.import_dat(_one_entry_dat("Some DAT", "S", "s.gba", b"s"))
    stale = store.index_generation() - 2

    async def decided_earlier(file_path, **_kwargs):
        return _miss(file_path, stale)

    scheduled: list[tuple[list[str], dict]] = []

    async def record_schedule(paths, **kwargs):
        scheduled.append((list(paths), kwargs))
        return "job-recorded"

    monkeypatch.setattr(dat_routes, "_match_single_file", decided_earlier)
    monkeypatch.setattr(dat_routes, "schedule_match_job", record_schedule)
    monkeypatch.setattr(dat_routes, "_resolve_and_group_paths", lambda paths: (
        {p: [p] for p in paths}, set(),
    ))

    await dat_routes.match_batch(dat_routes.MatchBatchRequest(paths=[path]))

    assert _row(store, path) is None
    assert scheduled == [([path], {"defer_if_busy": True, "local_only": True})]
