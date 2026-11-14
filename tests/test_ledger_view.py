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


@pytest.mark.parametrize("path", ["/api/ledger", "/api/ledger/export.csv", "/api/metrics"])
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
