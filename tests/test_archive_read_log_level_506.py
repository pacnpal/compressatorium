"""A junk or corrupt archive is the user's file, not an app error (#506)."""

from __future__ import annotations

import logging

import pytest

from app.services import archive as archive_mod
from app.services.archive import archive_service


@pytest.mark.parametrize("name", ["junk.rar", "junk.zip", "junk.7z"])
def test_junk_archive_logs_one_warning_without_traceback(tmp_path, caplog, name):
    path = tmp_path / name
    path.write_bytes(b"this is not an archive at all" * 8)
    with caplog.at_level(logging.DEBUG, logger="archive"):
        entries = archive_service.list_archive_contents(str(path))
    assert entries == []
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.levelno for r in problems] == [logging.WARNING]
    assert name in problems[0].getMessage()
    assert all(r.exc_info is None for r in caplog.records)


def test_unexpected_exception_still_logs_error_with_traceback(tmp_path, monkeypatch, caplog):
    path = tmp_path / "ok.zip"
    path.write_bytes(b"x")

    def boom(*_a, **_k):
        raise RuntimeError("a bug in our code")

    monkeypatch.setattr(archive_mod, "read_archive_members", boom)
    with caplog.at_level(logging.DEBUG, logger="archive"):
        assert archive_service.list_archive_contents(str(path)) == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "level"),
    [(OSError("cannot read"), logging.WARNING), (RuntimeError("bug"), logging.ERROR)],
)
async def test_summary_route_logs_by_exception_type(tmp_path, monkeypatch, caplog, exc, level):
    from app.routes import files as files_routes
    from models import MetadataBatchRequest

    path = tmp_path / "bad.zip"
    path.write_bytes(b"junk")
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    def fail(*_a, **_k):
        raise exc

    monkeypatch.setattr(files_routes, "_summarize_archive", fail)
    with caplog.at_level(logging.DEBUG, logger="files"):
        out = await files_routes.archive_summary_batch(MetadataBatchRequest(paths=[str(path)]))
    assert out[str(path)] == {"error": "summary_failed"}
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.levelno for r in problems] == [level]
    assert (problems[0].exc_info is not None) == (level == logging.ERROR)
