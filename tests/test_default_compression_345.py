"""RVZ and WIA take the tool's default codec when none is chosen (#345).

dolphin-tool refuses an RVZ or WIA conversion without ``-c``. Only the Rules
sweep used to fill one in, so on a fresh install (nothing saved) an Auto
convert, a single job and the CLI all failed at the tool. The default belongs
to the tool plugin: ``ToolPlugin.compression_for`` resolves it for every job,
whoever queued it. These tests queue through each web entry point, run the job,
and read the dolphin-tool command it built. The CLI's are in ``test_cli_306``.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path

import pytest
from services.tools import registry

from .test_platform_detect import _gamecube

pytestmark = pytest.mark.usefixtures("fresh_detect_cache")


@pytest.fixture(name="env")
def _env(tmp_path, monkeypatch):
    """A Volume at ``tmp_path``, an empty queue, nothing saved, and the
    dolphin-tool command of each conversion captured at its runner."""
    from app.routes import convert as convert_routes
    from services.job_manager import job_manager

    from config import settings

    for name, value in (
        ("chd_volumes", str(tmp_path)), ("data_mount_root", str(tmp_path)),
        ("temp_dir", str(tmp_path / "temp")), ("max_queue_depth", 0),
    ):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(job_manager, "jobs", OrderedDict())
    monkeypatch.setattr(job_manager, "_queue", asyncio.PriorityQueue())

    async def nothing_saved(_key):
        return None

    monkeypatch.setattr(convert_routes.preferences_store, "get", nothing_saved)

    async def ready(_tool, **_kwargs):
        return True

    monkeypatch.setattr(registry, "tool_is_ready", ready)
    commands: list[list[str]] = []
    service = registry.get("dolphin")._service

    async def capture(cmd, *, input_path, output_path, **kwargs):
        commands.append(list(cmd))
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    monkeypatch.setattr(service._runner, "run", capture)
    return {"tmp": tmp_path, "commands": commands}


async def _run_all() -> None:
    from services.job_manager import job_manager

    for job_id in list(job_manager.jobs):
        await job_manager._process_job(job_id)


def _codec(cmd: list[str]) -> str | None:
    return cmd[cmd.index("-c") + 1] if "-c" in cmd else None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dolphin_rvz", "dolphin_wia"])
async def test_a_single_job_takes_the_tools_default_codec(env, mode) -> None:
    from app.models import ConversionMode, JobCreateRequest
    from app.routes import convert as convert_routes

    iso = _gamecube(env["tmp"] / "gc" / "Melee.iso")
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=iso, mode=ConversionMode(mode),
    ))
    # The job records the codec it runs with (the Ledger, the re-run fast path).
    assert job.compression == registry.get("dolphin").default_compression

    await _run_all()

    (cmd,) = env["commands"]
    assert _codec(cmd) == registry.get("dolphin").default_compression


@pytest.mark.asyncio
async def test_an_auto_convert_takes_the_tools_default_codec(env) -> None:
    from app.models import AutoJobCreateRequest, AutoJobItem, AutoPlanRequest
    from app.routes import convert as convert_routes

    iso = _gamecube(env["tmp"] / "gc" / "Melee.iso")
    (row,) = await convert_routes.auto_plan(AutoPlanRequest(file_paths=[iso]))
    assert row.mode in ("dolphin_rvz", "dolphin_wia")
    await convert_routes.create_auto_jobs(AutoJobCreateRequest(
        items=[AutoJobItem(file_path=row.file_path, mode=row.mode)],
    ))

    await _run_all()

    (cmd,) = env["commands"]
    assert _codec(cmd) == registry.get("dolphin").default_compression


@pytest.mark.asyncio
async def test_a_chosen_codec_still_wins(env) -> None:
    from app.models import ConversionMode, JobCreateRequest
    from app.routes import convert as convert_routes

    iso = _gamecube(env["tmp"] / "gc" / "Melee.iso")
    await convert_routes.create_job(JobCreateRequest(
        file_path=iso, mode=ConversionMode.DOLPHIN_RVZ, compression="lzma2:9",
    ))

    await _run_all()

    (cmd,) = env["commands"]
    assert (_codec(cmd), cmd[cmd.index("-l") + 1]) == ("lzma2", "9")


@pytest.mark.parametrize(("mode", "given", "sent"), [
    # Nothing chosen: RVZ and WIA need a codec, so the tool's own is named.
    ("dolphin_rvz", None, "zstd"),
    ("dolphin_rvz", "", "zstd"),
    ("dolphin_wia", None, "zstd"),
    # A level with "tool default" for the codec keeps the level.
    ("dolphin_rvz", ":19", "zstd:19"),
    # A chosen codec is never second-guessed.
    ("dolphin_rvz", "lzma2:9", "lzma2:9"),
    # GCZ takes no codec at all.
    ("dolphin_gcz", None, None),
    # chdman works without one, and its own default beats naming one.
    ("createcd", None, None),
    # nsz has no codec picker: its empty part is a layout default, kept.
    ("nsz_compress", ":12", ":12"),
])
def test_the_plugin_resolves_what_a_job_runs_with(mode, given, sent) -> None:
    """``compression_for`` is the one place the default is applied."""
    assert registry.for_mode(mode).compression_for(mode, given) == sent


def test_a_codec_mode_with_no_declared_default_sends_no_level(monkeypatch) -> None:
    """A level cannot be sent without a codec; inventing one would convert with
    a setting nobody chose, so the level is dropped rather than fail at the tool."""
    tool = registry.for_mode("dolphin_rvz")
    monkeypatch.setattr(tool, "default_compression", None)

    assert tool.compression_for("dolphin_rvz", ":19") is None
