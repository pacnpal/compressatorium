"""Ledger view (#297): paged list, filters, CSV export, metrics, auth."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from models import ConversionJob, ConversionMode, JobStatus
from services import db as _db
from services import ledger


@pytest.fixture(autouse=True)
def _engine(db_path, reset_db_engine):
    _db.init_engine(db_path)
    yield
    reset_db_engine()


async def _seed():
    base = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    rows = [
        ("/data/ps2/Alpha.iso", ConversionMode.CREATEDVD, JobStatus.COMPLETED),
        ("/data/gc/Beta_100%.iso", ConversionMode.DOLPHIN_RVZ, JobStatus.FAILED),
        ("/data/ps2/Gamma.iso", ConversionMode.CREATEDVD, JobStatus.CANCELLED),
    ]
    for i, (path, mode, status) in enumerate(rows):
        done = base + timedelta(days=i)
        await ledger.record(ConversionJob(
            id=f"s{i}", file_path=path, filename=path.rsplit("/", 1)[-1], mode=mode,
            status=status, created_at=done, started_at=done - timedelta(seconds=30),
            completed_at=done, output_path=path[:-4] + ".out", output_size=400,
            input_bytes=1000, check_passed=None, deleted_paths=[],
            error_message="=boom" if status is JobStatus.FAILED else None,
        ))


def _ids(page):
    return [r["job_id"] for r in page["items"]]


@pytest.mark.asyncio
async def test_list_pages_newest_first_with_total():
    await _seed()
    page = await ledger.list_page(ledger.LedgerFilter(), limit=2, offset=0)
    assert page["total"] == 3
    assert _ids(page) == ["s2", "s1"]
    rest = await ledger.list_page(ledger.LedgerFilter(), limit=2, offset=2)
    assert _ids(rest) == ["s0"]


@pytest.mark.asyncio
async def test_list_filters_by_path_status_tool_platform():
    await _seed()
    f = ledger.LedgerFilter
    assert _ids(await ledger.list_page(f(q="alpha"))) == ["s0"]
    # A literal % in the search must not act as a wildcard.
    assert _ids(await ledger.list_page(f(q="100%"))) == ["s1"]
    assert _ids(await ledger.list_page(f(q="%"))) == ["s1"]
    assert _ids(await ledger.list_page(f(status="failed"))) == ["s1"]
    assert _ids(await ledger.list_page(f(tool="dolphin"))) == ["s1"]
    # Platform is only known when the mode serves exactly one; stamp it here.
    with _db.SessionLocal() as session:
        session.query(_db.LedgerEntry).filter_by(job_id="s2").update({"platform": "ps2"})
        session.commit()
    assert _ids(await ledger.list_page(f(platform="ps2"))) == ["s2"]
    assert _ids(await ledger.list_page(f(tool="chdman", platform="gc"))) == []
    assert "ps2" in (await ledger.facets())["platform"]


@pytest.mark.asyncio
async def test_facets_list_distinct_filter_values():
    await _seed()
    facets = await ledger.facets()
    assert set(facets["status"]) == {"completed", "failed", "cancelled"}
    assert set(facets["tool"]) == {"chdman", "dolphin"}


@pytest.mark.asyncio
async def test_csv_export_matches_filter_and_is_safe():
    await _seed()
    chunks = [c async for c in ledger.export_csv(ledger.LedgerFilter(status="failed"))]
    lines = "".join(chunks).strip().splitlines()
    assert lines[0].startswith("job_id,finished_at,tool,mode,source_path")
    assert len(lines) == 2 and "Beta_100%.iso" in lines[1]
    # A cell that starts with = is neutralised so a spreadsheet won't run it.
    assert "'=boom" in lines[1]
    everything = "".join([c async for c in ledger.export_csv(ledger.LedgerFilter())])
    assert len(everything.strip().splitlines()) == 4


@pytest.mark.asyncio
async def test_metrics_text_has_status_bytes_and_queue_depth():
    await _seed()
    text = await ledger.metrics_text(queue_depth=7)
    assert 'compressatorium_jobs_total{status="completed"} 1' in text
    assert 'compressatorium_jobs_total{status="failed"} 1' in text
    assert 'compressatorium_jobs_total{status="cancelled"} 1' in text
    # Bytes count completed conversions only.
    assert "compressatorium_input_bytes_total 1000" in text
    assert "compressatorium_output_bytes_total 400" in text
    assert "compressatorium_queue_depth 7" in text
    assert "# TYPE compressatorium_queue_depth gauge" in text


@pytest.mark.asyncio
async def test_metrics_lists_every_status_even_when_empty():
    text = await ledger.metrics_text(queue_depth=0)
    for s in ("completed", "failed", "cancelled"):
        assert f'compressatorium_jobs_total{{status="{s}"}} 0' in text


@pytest.mark.asyncio
async def test_routes_call_the_same_query_layer():
    from routes import ledger as routes

    await _seed()
    page = await routes.ledger_list(
        q="", status="failed", tool="", platform="", limit=50, offset=0,
    )
    assert page["total"] == 1
    resp = await routes.ledger_export(q="", status="failed", tool="", platform="")
    assert resp.headers["content-disposition"].startswith('attachment; filename="ledger')
    assert resp.media_type == "text/csv"
    assert (await routes.metrics()).media_type.startswith("text/plain")


LEDGER_PATHS = [
    "/api/ledger", "/api/ledger/export.csv", "/api/ledger/savings", "/api/metrics",
]


def test_ledger_paths_are_mounted_on_the_app():
    import main

    assert set(LEDGER_PATHS) <= set(main.app.openapi()["paths"])


@pytest.mark.asyncio
async def test_export_route_streams_the_csv_body():
    from routes import ledger as routes

    await _seed()
    resp = await routes.ledger_export(q="", status="", tool="", platform="")
    body = "".join([c async for c in resp.body_iterator])
    assert body.startswith("job_id,finished_at,tool")
    assert len(body.strip().splitlines()) == 4


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r", "\n"])
@pytest.mark.asyncio
async def test_csv_cells_with_formula_prefixes_are_neutralised(prefix):
    import csv
    import io

    job = ConversionJob(
        id="f", file_path="/data/x.iso", filename="x.iso", mode=ConversionMode.CREATEDVD,
        status=JobStatus.FAILED, created_at=datetime.now(timezone.utc),
        error_message=f"{prefix}cmd|' /C calc'!A0",
    )
    await ledger.record(job)
    text = "".join([c async for c in ledger.export_csv(ledger.LedgerFilter())])
    rows = list(csv.reader(io.StringIO(text, newline="")))
    err = rows[1][rows[0].index("error")]
    assert err.startswith("'" + prefix)


@pytest.mark.asyncio
async def test_a_repeated_notification_counts_once():
    job = _done("dup", datetime.now(timezone.utc))
    await ledger.record(job)
    await ledger.record(job)
    assert 'compressatorium_jobs_total{status="completed"} 1' in await ledger.metrics_text(0)


def _done(job_id, when):
    return ConversionJob(
        id=job_id, file_path=f"/data/{job_id}.iso", filename=f"{job_id}.iso",
        mode=ConversionMode.CREATEDVD, status=JobStatus.COMPLETED, created_at=when,
        started_at=when - timedelta(seconds=5), completed_at=when,
        output_path=f"/data/{job_id}.chd", output_size=400, input_bytes=1000,
        deleted_paths=[],
    )


@pytest.mark.asyncio
async def test_export_does_not_duplicate_when_a_row_lands_mid_export(monkeypatch):
    monkeypatch.setattr(ledger, "_CSV_PAGE", 2)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for i in range(5):
        await ledger.record(_done(f"j{i}", base + timedelta(days=i)))
    chunks = []
    async for chunk in ledger.export_csv(ledger.LedgerFilter()):
        chunks.append(chunk)
        if len(chunks) == 1:
            await ledger.record(_done("late", base + timedelta(days=30)))
    ids = [line.split(",")[0] for line in "".join(chunks).strip().splitlines()[1:]]
    assert len(ids) == len(set(ids))
    assert set(ids) == {f"j{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_export_does_not_skip_when_old_rows_are_pruned_mid_export(monkeypatch):
    monkeypatch.setattr(ledger, "_CSV_PAGE", 2)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for i in range(5):
        await ledger.record(_done(f"j{i}", base + timedelta(days=i)))
    seen = []
    async for chunk in ledger.export_csv(ledger.LedgerFilter()):
        seen.append(chunk)
        if len(seen) == 1:
            with _db.SessionLocal() as s:
                s.query(_db.LedgerEntry).filter_by(job_id="j4").delete()
                s.commit()
    ids = [line.split(",")[0] for line in "".join(seen).strip().splitlines()[1:]]
    # j4 was in the first page; deleting it must not shift later pages and drop j2.
    assert ids == ["j4", "j3", "j2", "j1", "j0"]


@pytest.mark.asyncio
async def test_export_reads_one_snapshot_while_a_prune_commits(monkeypatch):
    monkeypatch.setattr(ledger, "_CSV_PAGE", 2)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    for i in range(5):
        await ledger.record(_done(f"old{i}", old + timedelta(minutes=i)))
    chunks = []
    async for chunk in ledger.export_csv(ledger.LedgerFilter()):
        chunks.append(chunk)
        if len(chunks) == 1:
            # Rows are deleted from another connection mid-export.
            with _db.SessionLocal() as other:
                other.execute(_db.LedgerEntry.__table__.delete())
                other.commit()
    ids = [line.split(",")[0] for line in "".join(chunks).strip().splitlines()[1:]]
    assert sorted(ids) == [f"old{i}" for i in range(5)]
    assert await ledger.recent(10) == []


def _track_snapshot_close(monkeypatch):
    closed = []
    real_open = ledger._open_snapshot

    def tracking_open():
        session = real_open()
        real_close = session.close
        session.close = lambda: (closed.append(True), real_close())[1]
        return session

    monkeypatch.setattr(ledger, "_open_snapshot", tracking_open)
    return closed


@pytest.mark.asyncio
async def test_closing_the_stream_after_one_chunk_releases_the_snapshot(monkeypatch):
    monkeypatch.setattr(ledger, "_CSV_PAGE", 2)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for i in range(5):
        await ledger.record(_done(f"j{i}", base + timedelta(days=i)))
    closed = _track_snapshot_close(monkeypatch)
    gen = ledger.export_csv(ledger.LedgerFilter())
    await gen.__anext__()
    assert closed == []
    await gen.aclose()
    assert closed == [True]


@pytest.mark.asyncio
async def test_a_failed_send_closes_the_export_stream(monkeypatch):
    from routes import ledger as routes

    monkeypatch.setattr(ledger, "_CSV_PAGE", 2)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for i in range(5):
        await ledger.record(_done(f"j{i}", base + timedelta(days=i)))
    closed = _track_snapshot_close(monkeypatch)
    resp = await routes.ledger_export(q="", status="", tool="", platform="")

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    with pytest.raises(OSError):
        await resp(scope, receive, send)
    assert closed == [True]


@pytest.mark.asyncio
async def test_a_cancelled_export_still_releases_its_snapshot(monkeypatch):
    import anyio

    monkeypatch.setattr(ledger, "_CSV_PAGE", 2)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for i in range(5):
        await ledger.record(_done(f"j{i}", base + timedelta(days=i)))
    closed = []
    real_open = ledger._open_snapshot

    def tracking_open():
        session = real_open()
        real_close = session.close
        session.close = lambda: (closed.append(True), real_close())[1]
        return session

    monkeypatch.setattr(ledger, "_open_snapshot", tracking_open)
    gen = ledger.export_csv(ledger.LedgerFilter())
    with anyio.CancelScope() as scope:
        async for _ in gen:
            scope.cancel()  # the next page fetch runs inside a cancelled scope
    assert closed == [True]


def test_migration_0011_seeds_counters_from_the_ledger(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    _db.apply_migrations(engine)
    with engine.begin() as c:
        c.execute(text("delete from ledger_totals"))
        for i, (st, inb, outb) in enumerate(
            [("completed", 10, 4), ("completed", 20, 5), ("failed", 7, None)]
        ):
            c.execute(text(
                "insert into ledger (job_id, finished_at, mode, source_path, status, "
                "check_passed, source_deleted, deleted_paths, referrers_updated, "
                "input_bytes, output_bytes) values (:j, :f, 'm', '/p', :s, 'none', 0, "
                "'[]', '[]', :i, :o)"
            ), {"j": f"j{i}", "f": f"2026-01-0{i + 1}", "s": st, "i": inb, "o": outb})
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "migrations/versions"
    spec = importlib.util.spec_from_file_location("m10", next(path.glob("0011_*.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with engine.begin() as c:
        mod.seed_totals(c)
        rows = {r[0]: r[1:] for r in c.execute(text(
            "select status, jobs, input_bytes, output_bytes from ledger_totals"))}
    assert rows["completed"] == (2, 30, 9)
    assert rows["failed"] == (1, 0, 0)


def test_migrations_have_a_single_head():
    from alembic.script import ScriptDirectory

    cfg = _db._alembic_config(_db.make_engine(":memory:"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    # #303's 0012 (folder Libraries) stacks on this branch's 0011, #304's
    # 0013 (mappings and Findings) on that, #305's 0014 (check times) on
    # that, #337's 0015 (re-pin plan generation) on that, #386's 0016
    # (the EmulationStation frontend switch) on that, #388's 0017 (content
    # keys) on that, #387's 0018 (the ES-DE gamelists folder) on that, and
    # #389's 0019 (a content key's device and inode) on that, and #391's
    # 0020 (the RomM swap journal) on that, and #392's 0021 (what swap
    # recovery needs) on that, and #424's 0022 (every DAT's match) on that,
    # and #430's 0023 (a folder Library's layout) on that, and #423's 0024
    # (the DAT that Recognized an output) on that, and #426's 0025 (each
    # DAT's file list) on that, and #428's 0026 (the Ledger's extra inputs)
    # on that, and #431's 0027 (Inboxes) on that.
    assert heads == ["0027"]


def test_upgrade_from_0010_seeds_totals_from_populated_ledger(tmp_path):
    from alembic import command
    from sqlalchemy import text

    engine = _db.make_engine(str(tmp_path / "up.db"))
    cfg = _db._alembic_config(engine)
    command.upgrade(cfg, "0010")
    with engine.begin() as c:
        for i, (st, inb, outb) in enumerate(
            [("completed", 10, 4), ("completed", 20, 5), ("cancelled", 3, None)]
        ):
            c.execute(text(
                "insert into ledger (job_id, finished_at, mode, source_path, status, "
                "check_passed, source_deleted, deleted_paths, referrers_updated, "
                "input_bytes, output_bytes) values (:j, :f, 'm', '/p', :s, 'none', 0, "
                "'[]', '[]', :i, :o)"
            ), {"j": f"j{i}", "f": f"2026-01-0{i + 1}", "s": st, "i": inb, "o": outb})
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        rows = {r[0]: tuple(r[1:]) for r in c.execute(text(
            "select status, jobs, input_bytes, output_bytes from ledger_totals"))}
    assert rows == {"completed": (2, 30, 9), "cancelled": (1, 0, 0)}


def _run_0011_upgrade(engine):
    import importlib.util
    import pathlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = pathlib.Path(__file__).resolve().parent.parent / "migrations/versions"
    spec = importlib.util.spec_from_file_location("m10u", next(path.glob("0011_*.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        mod.upgrade()


def _ledger_db(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path}/pre.db")
    _db.ensure_schema(engine)  # create_all path: ledger_totals exists, empty
    with engine.begin() as c:
        for i, (st, inb, outb) in enumerate([("completed", 10, 4), ("failed", 7, None)]):
            c.execute(text(
                "insert into ledger (job_id, finished_at, mode, source_path, status, "
                "check_passed, source_deleted, deleted_paths, referrers_updated, "
                "input_bytes, output_bytes) values (:j, :f, 'm', '/p', :s, 'none', 0, "
                "'[]', '[]', :i, :o)"
            ), {"j": f"j{i}", "f": f"2026-01-0{i + 1}", "s": st, "i": inb, "o": outb})
    return engine


def test_0011_seeds_a_precreated_empty_totals_table(tmp_path):
    from sqlalchemy import text

    engine = _ledger_db(tmp_path)
    _run_0011_upgrade(engine)
    with engine.connect() as c:
        rows = {r[0]: r[1:] for r in c.execute(text(
            "select status, jobs, input_bytes, output_bytes from ledger_totals"))}
    assert rows == {"completed": (1, 10, 4), "failed": (1, 0, 0)}


def test_0011_never_overwrites_existing_counters(tmp_path):
    from sqlalchemy import text

    engine = _ledger_db(tmp_path)
    with engine.begin() as c:
        c.execute(text("insert into ledger_totals values ('completed', 99, 5, 6)"))
    _run_0011_upgrade(engine)
    with engine.connect() as c:
        rows = c.execute(text("select * from ledger_totals")).all()
    assert [tuple(r) for r in rows] == [("completed", 99, 5, 6)]


@pytest.mark.parametrize("path", LEDGER_PATHS)
def test_new_endpoints_are_behind_auth(monkeypatch, path):
    from auth import require_auth_middleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from config import settings

    monkeypatch.setattr(settings, "enable_auth", True)
    monkeypatch.setattr(settings, "auth_token", "tok")

    def req(headers):
        return Request({"type": "http", "method": "GET", "path": path, "query_string": b"",
                        "headers": headers, "server": ("t", 80), "scheme": "http",
                        "client": ("c", 1)})

    async def ok(_r):
        return JSONResponse({})

    assert asyncio.run(require_auth_middleware(req([]), ok)).status_code == 401
    good = [(b"authorization", b"Bearer tok")]
    assert asyncio.run(require_auth_middleware(req(good), ok)).status_code == 200
