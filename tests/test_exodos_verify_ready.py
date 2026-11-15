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
