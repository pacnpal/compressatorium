"""Ledger routes (#292): recent rows, savings summary, retention setting."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from services import ledger
from services.preferences_store import preferences_store

router = APIRouter()


class LedgerSettings(BaseModel):
    # None/0 keeps rows forever (the default).
    retention_days: int | None = Field(
        default=None, ge=0, le=ledger.MAX_RETENTION_DAYS,
    )


@router.get("/ledger/recent")
async def ledger_recent(limit: int = Query(5, ge=1, le=100)) -> list[dict]:
    return await ledger.recent(limit)


@router.get("/ledger/savings")
async def ledger_savings() -> dict:
    return await ledger.savings()


@router.get("/ledger/settings")
async def get_ledger_settings() -> dict:
    return {"retention_days": await ledger.retention_days()}


@router.put("/ledger/settings")
async def put_ledger_settings(body: LedgerSettings) -> dict:
    await preferences_store.put(ledger.PREF_KEY, body.model_dump())
    return {"retention_days": await ledger.retention_days()}
