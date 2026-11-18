"""Rules refuse, and drop, a codec the CHDMAN mode can't use (#474).

#471 made each chdman create mode declare its codecs, so ``/api/jobs`` refuses
Create DVD with cdzl. Rules went around that check: the save took the pair, the
run queued it, and every Image failed in chdman with "Codec error". A Rule save
now refuses the pair with the job route's own message, on every save path (a
folder Library's Rules and RomM's), and a Rule stored before the check runs on
the codecs its mode takes (chdman's own when none is left), and says so.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from services.job_manager import job_manager
from services.preferences_store import preferences_store

from .test_folder_library_303 import _db_fixture, _ready, _register, _tree

__all__ = ["_db_fixture", "_ready", "_tree"]

_REFUSED = (
    "Create DVD can't use cdzl compression; it takes "
    "flac, huff, lzma, none, zlib, zstd"
)


@pytest.fixture(name="queue")
def _queue(monkeypatch):
    """The queue stubbed: records each queued path with the compression it got."""
    queued: list[tuple[str, str | None]] = []

    async def fake_batch(paths, mode, **kwargs):
        jobs = []
        for path in paths:
            queued.append((Path(path).name, kwargs.get("compression")))
            jobs.append(SimpleNamespace(file_path=path, id=f"job-{len(queued)}"))
        return jobs

    monkeypatch.setattr(job_manager, "create_batch_jobs", fake_batch)
    monkeypatch.setattr(
        job_manager, "get_active_job_candidates",
        lambda: [(f"job-{i}", [p]) for i, (p, _c) in enumerate(queued)],
    )
    return queued


@pytest.mark.asyncio
async def test_a_library_rule_save_refuses_a_codec_its_mode_cannot_use(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    with pytest.raises(HTTPException) as refused:
        await routes.put_library_rules(lib["id"], {"rules": {
            "ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
        }})

    assert refused.value.status_code == 400
    assert refused.value.detail == _REFUSED
    assert (await routes.get_library_rules(lib["id"]))["rules"] == {}


@pytest.mark.asyncio
async def test_a_romm_rule_save_refuses_a_codec_its_mode_cannot_use(db: Path) -> None:
    from routes import romm as romm_routes

    with pytest.raises(HTTPException) as refused:
        await romm_routes.put_romm_rules({"rules": {
            "9": {"enabled": True, "mode": "createdvd", "compression": "zlib,cdzl"},
        }})

    assert refused.value.status_code == 400
    assert refused.value.detail == _REFUSED
    assert (await romm_routes.get_romm_rules())["rules"] == {}


@pytest.mark.asyncio
async def test_a_rule_with_codecs_its_mode_takes_still_saves(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    saved = await routes.put_library_rules(lib["id"], {"rules": {
        "ps2": {"enabled": True, "mode": "createdvd", "compression": "zlib,huff"},
    }})

    assert saved["rules"]["ps2"]["compression"] == "zlib,huff"
    assert saved["rules"]["ps2"]["dropped_codecs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "kept"), [
    ("cdzl", None),
    ("zlib,cdzl,cdlz", "zlib"),
])
async def test_a_stored_rule_reads_back_without_the_codecs_its_mode_cannot_use(
    tree: Path, stored: str, kept: str | None,
) -> None:
    """A blob saved before the check (or written straight into the database)
    reads back with only what the mode takes, and names what it dropped, so
    the editor shows it and its next save sends a pair the save accepts."""
    from routes import libraries as routes

    lib = await _register(tree)
    await preferences_store.put("rules", {
        f"{lib['id']}:ps2": {"enabled": True, "mode": "createdvd", "compression": stored},
    })

    rule = (await routes.get_library_rules(lib["id"]))["rules"]["ps2"]
    assert rule["compression"] == kept
    assert rule["dropped_codecs"] == [c for c in stored.split(",") if c.startswith("cd")]

    saved = await routes.put_library_rules(lib["id"], {"rules": {"ps2": rule}})
    assert saved["rules"]["ps2"]["compression"] == kept


@pytest.mark.asyncio
async def test_a_stored_bad_rule_runs_on_chdmans_own_codecs_and_says_so(
    tree: Path, ready, queue, caplog,
) -> None:
    """The run queues the Image on chdman's defaults instead of a job that is
    sure to fail with "Codec error", and the log names the dropped codec."""
    from routes import libraries as routes

    lib = await _register(tree)
    await preferences_store.put("rules", {
        f"{lib['id']}:ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
    })

    with caplog.at_level(logging.WARNING):
        await routes.run_library(lib["id"], routes.SweepRequest(platform_ids=["ps2"]))

    assert ("Ico.iso", None) in queue
    assert any(
        "cdzl" in r.getMessage() and "Create DVD" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_an_auto_target_keeps_the_saved_codecs_its_mode_takes() -> None:
    """The one drop rule serves Auto and the CLI too (#471's fallback): the
    codecs the mode takes are kept, and chdman's own run only when none is."""
    from app.routes.convert import checked_compression

    assert checked_compression("createdvd", "zlib,cdzl") == "zlib"
    assert checked_compression("createdvd", "cdlz,cdzl,cdfl") is None
    assert checked_compression("createcd", "cdlz,cdzl,cdfl") == "cdlz,cdzl,cdfl"
