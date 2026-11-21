"""Ledger routes (#292): recent rows and the savings summary."""

import anyio
from fastapi import APIRouter, Query
from fastapi.responses import Response, StreamingResponse
from services import ledger
from services.job_manager import job_manager

router = APIRouter()


class ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that always closes its body iterator.

    Starlette leaves the iterator suspended at its last ``yield`` when a send
    fails (client disconnect). The CSV export holds a read snapshot across
    yields, so close it here instead of waiting for garbage collection.
    """

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                with anyio.CancelScope(shield=True):
                    await aclose()


@router.get("/ledger")
async def ledger_list(
    q: str = Query("", max_length=500),
    status: str = Query("", max_length=40),
    tool: str = Query("", max_length=40),
    platform: str = Query("", max_length=40),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Newest-first, paged, filtered rows: ``{"items": [...], "total": N}``."""
    flt = ledger.LedgerFilter(q=q, status=status, tool=tool, platform=platform)
    return await ledger.list_page(flt, limit, offset)


@router.get("/ledger/export.csv")
async def ledger_export(
    q: str = Query("", max_length=500),
    status: str = Query("", max_length=40),
    tool: str = Query("", max_length=40),
    platform: str = Query("", max_length=40),
) -> StreamingResponse:
    """Every row matching the same filter the list takes, as CSV."""
    flt = ledger.LedgerFilter(q=q, status=status, tool=tool, platform=platform)
    return ClosingStreamingResponse(
        ledger.export_csv(flt),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ledger.csv"'},
    )


@router.get("/ledger/facets")
async def ledger_facets() -> dict:
    return await ledger.facets()


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus text: jobs by status, bytes in and out, queue depth."""
    text = await ledger.metrics_text(job_manager.get_queue_depth())
    return Response(text, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/ledger/recent")
async def ledger_recent(limit: int = Query(5, ge=1, le=100)) -> list[dict]:
    return await ledger.recent(limit)


@router.get("/ledger/totals")
async def ledger_totals() -> dict:
    return await ledger.totals()


@router.get("/ledger/savings")
async def ledger_savings() -> dict:
    return await ledger.savings()


