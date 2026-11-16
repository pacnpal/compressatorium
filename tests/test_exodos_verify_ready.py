"""A repacked .dosz is checkable without the eXoDOS install (PR #324 review).

The product check reads only the .dosz, so its readiness must not depend on
EXODOS_DOS_DIR or the template tree that converting needs.
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from collections import OrderedDict

import pytest
from services.tools import registry


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch, reset_db_engine):
    from services.job_manager import job_manager

    from config import settings

    vol = tmp_path / "vol"
    vol.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(settings, "data_dir", str(config))
    monkeypatch.setattr(settings, "db_path", str(config / "compressatorium.db"))
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(settings, "max_queue_depth", 0)
    # No eXoDOS install anywhere: the template tree is unset.
    monkeypatch.setattr(settings, "exodos_dos_dir", None)
    monkeypatch.setattr(job_manager, "jobs", OrderedDict())
    monkeypatch.setattr(job_manager, "_queue", asyncio.PriorityQueue())
    yield vol
    reset_db_engine()


def _dosz(volume):
    path = volume / "dos" / "Abaron (1995).dosz"
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dosbox.conf", b"[autoexec]\r\nmount c .\\\r\nABARON.EXE\r\n")
        zf.writestr("ABARON.EXE", b"exe" * 100)
    return path


def test_checking_needs_no_template_tree_but_converting_does(volume):
    tool = registry.get("exodos")

    assert asyncio.run(registry.tool_is_ready(tool, purpose="verify")) is True
    assert asyncio.run(registry.tool_is_ready(tool)) is False


def test_the_cli_checks_a_dosz_without_the_exodos_install(volume, capsys):
    import cli

    product = _dosz(volume)

    code = cli.main(["verify", str(product), "--json"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert code == 0
    assert [(line["tool"], line["status"]) for line in lines] == [("exodos", "intact")]



# --- a check that could not read the file is no verdict (#354, codex 4582164683)


def _unreadable(monkeypatch):
    """The product's own check hits an I/O error after the preflight."""
    from services.exodos import ExodosService

    def unreadable(self, path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(ExodosService, "_check_product", unreadable)


def _stored_check(path):
    """The stored row's check, a failed one included (reads skip those)."""
    from services import db as _db
    from services.verification_store import verification_store

    with verification_store._session() as session:
        row = session.get(_db.Verification, verification_store._normalize(str(path)))
        return row.check_passed if row is not None else None


def test_the_cli_leaves_a_stored_check_when_it_cannot_read_the_file(
    volume, capsys, monkeypatch,
):
    import cli

    product = _dosz(volume)
    assert cli.main(["verify", str(product), "--json"]) == 0
    capsys.readouterr()
    assert _stored_check(product) == "intact"
    _unreadable(monkeypatch)

    cli.main(["verify", str(product), "--json"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert [line["status"] for line in lines] == ["uncheckable"]  # no verdict (#370)
    assert "Permission denied" in lines[0]["reason"]
    assert _stored_check(product) == "intact"


def test_the_cli_still_marks_a_corrupt_file_failed(volume, capsys):
    import cli

    product = _dosz(volume)
    assert cli.main(["verify", str(product), "--json"]) == 0
    capsys.readouterr()
    with zipfile.ZipFile(product, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("ABARON.EXE", b"exe" * 100)  # no dosbox.conf at the root

    cli.main(["verify", str(product), "--json"])

    assert _stored_check(product) == "failed"
