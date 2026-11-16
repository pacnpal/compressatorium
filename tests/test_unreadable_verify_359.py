"""An output the verifier can't open is no verdict (#359).

A verify of a CHD the app can't read (``chmod 000``: EACCES) used to clear its
stored Intact record: chdman exits 1 with "Permission denied", and that exit
read as a verdict. The shared verify entry (``verify_readable``, inside
``verify_preflight``, ``run_verify`` and ``capture_verify``) now opens the
output first, bounded: one it can't open is ``inconclusive``, naming the file
and the errno, so every caller (the verify routes, the CLI, a job's check, a
retried delete) leaves the stored record as it was. A verifier's own non-zero
exit after a successful open is still a verdict.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

import pytest
from services.tools import registry

GOOD = b"a good chd"


@pytest.fixture(name="chd")
def _chd(tmp_path, monkeypatch, reset_db_engine):
    """A checked CHD in a Volume, and a chdman that passes only its good bytes."""
    from services.job_manager import job_manager
    from services.verification_store import verification_store
    from services.workload_limiter import WorkloadLimiter

    from config import settings
    from routes import info as info_routes

    vol = tmp_path / "vol"
    (vol / "ps2").mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(settings, "data_dir", str(config))
    monkeypatch.setattr(settings, "db_path", str(config / "compressatorium.db"))
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(job_manager, "jobs", OrderedDict())
    monkeypatch.setattr(job_manager, "_queue", asyncio.PriorityQueue())
    monkeypatch.setattr(
        info_routes, "workload_limiter",
        WorkloadLimiter(verify_limit=1, metadata_scan_limit=1),
    )
    fake = tmp_path / "bin" / "chdman"
    fake.parent.mkdir()
    # Like chdman: it opens the file itself, and says so when it can't.
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "path = sys.argv[sys.argv.index('-i') + 1]\n"
        "try:\n"
        "    data = open(path, 'rb').read()\n"
        "except OSError as exc:\n"
        "    print(f'Error opening CHD file: {exc.strerror}'); sys.exit(1)\n"
        f"if data == {GOOD!r}:\n"
        "    print('Raw SHA1 verification successful!'); sys.exit(0)\n"
        "print('Error: Raw SHA1 in header does not match'); sys.exit(1)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(registry.get("chdman")._service, "chdman_path", str(fake))
    path = vol / "ps2" / "Game.chd"
    path.write_bytes(GOOD)
    import cli

    cli._open_db(False)
    asyncio.run(verification_store.refresh_check(str(path)))
    yield path
    if path.exists():
        path.chmod(0o644)
    reset_db_engine()


@pytest.fixture(name="unreadable")
def _unreadable(chd):
    if os.geteuid() == 0:
        pytest.skip("root reads a chmod 000 file")
    chd.chmod(0)
    return chd


def _stored(path) -> str | None:
    """The stored row's check, a failed one included (reads skip those)."""
    from services import db as _db
    from services.verification_store import verification_store

    with verification_store._session() as session:
        row = session.get(_db.Verification, verification_store._normalize(str(path)))
        return row.check_passed if row is not None else None


def _cfg():
    from routes import info as info_routes

    return info_routes._VERIFY_CONFIG["chdman"]


def test_the_sync_route_leaves_the_record_of_a_file_it_cannot_open(unreadable):
    from routes import info as info_routes

    result = asyncio.run(info_routes.verify_chd(path=str(unreadable)))

    assert result["valid"] is False
    assert result.get("inconclusive") is True
    assert "Game.chd" in result["message"] and "Permission denied" in result["message"]
    assert _stored(unreadable) == "intact"


def test_the_event_stream_leaves_the_record_of_a_file_it_cannot_open(unreadable):
    from routes import info as info_routes

    async def read_all():
        stream = info_routes._sse_from_verify_stream(
            registry.get("chdman"), _cfg(), str(unreadable),
        ).body_iterator
        return [event async for event in stream]

    events = asyncio.run(read_all())

    assert json.loads(events[-1]["data"]).get("inconclusive") is True
    assert _stored(unreadable) == "intact"


def test_the_batch_counts_a_file_it_cannot_open_as_not_checkable(unreadable):
    from routes import info as info_routes

    async def read_all():
        token = await info_routes.workload_limiter.acquire("verify")
        stream = info_routes._sse_batch_from_verify_stream(
            registry.get("chdman"), _cfg(), [str(unreadable)], token,
        ).body_iterator
        return [event async for event in stream]

    events = asyncio.run(read_all())

    done = [
        json.loads(event["data"]) for event in events
        if event["event"] == "verify_batch_file_complete"
    ]
    assert [line["status"] for line in done] == ["uncheckable"]
    assert done[0]["failed"] == 0
    assert _stored(unreadable) == "intact"


def test_the_cli_leaves_the_record_of_a_file_it_cannot_open(unreadable, capsys):
    import cli

    cli.main(["verify", str(unreadable), "--json"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert "Permission denied" in lines[0]["reason"]
    assert _stored(unreadable) == "intact"


def test_a_corrupt_file_still_marks_its_record_failed(chd, capsys):
    """Readable, and the verifier's own non-zero exit is a verdict."""
    import cli
    from routes import info as info_routes

    Path(chd).write_bytes(b"something else")

    result = asyncio.run(info_routes.verify_chd(path=str(chd)))

    assert result["valid"] is False and not result.get("inconclusive")
    assert _stored(chd) == "failed"
    asyncio.run(_refresh(chd))
    cli.main(["verify", str(chd), "--json"])
    capsys.readouterr()
    assert _stored(chd) == "failed"


def test_a_missing_output_is_judged_as_before(chd):
    """A missing file is not "could not read": it is gone. The verifier still
    runs and its own answer stands, as on latest; the open probe answers only
    for a file that exists but can't be opened."""
    Path(chd).unlink()

    result = asyncio.run(registry.get("chdman")._service.verify(str(chd)))

    assert result["valid"] is False
    assert not result.get("inconclusive")
    assert "Error opening CHD file" in result["message"]


async def _refresh(path) -> None:
    from services.verification_store import verification_store

    await verification_store.refresh_check(str(path))
