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
import dataclasses
from collections import OrderedDict
from pathlib import Path

import pytest
from services.tools import registry

from .dolphin_helpers import dolphin_option
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
    """dolphin-tool's codec, read after its binary (a priority prefix such
    as `ionice -c 2` may come first)."""
    return dolphin_option(cmd, "-c")


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
    assert job.compression.partition(":")[0] == registry.spec(mode).default_compression

    await _run_all()

    (cmd,) = env["commands"]
    assert _codec(cmd) == registry.spec(mode).default_compression


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
    assert _codec(cmd) == registry.spec(row.mode).default_compression


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
    assert (_codec(cmd), dolphin_option(cmd, "-l")) == ("lzma2", "9")


@pytest.mark.parametrize(("mode", "given", "sent"), [
    # Nothing chosen: RVZ and WIA need a codec, so the mode's own is named,
    # with that codec's default level (dolphin-tool needs one).
    ("dolphin_rvz", None, "zstd:19"),
    ("dolphin_rvz", "", "zstd:19"),
    # A chosen codec without a level gets that codec's default level.
    ("dolphin_wia", "lzma2", "lzma2:5"),
    # WIA's own default: Dolphin refuses zstd in a WIA container.
    ("dolphin_wia", None, "none"),
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
    # And the mode's own default, which comes first.
    rvz = tool.spec("dolphin_rvz")
    others = [m for m in tool.modes if m is not rvz]
    no_default = dataclasses.replace(rvz, default_compression=None)
    monkeypatch.setattr(tool, "modes", [no_default, *others])

    assert tool.compression_for("dolphin_rvz", ":19") is None


# --------------------------------------------------------------------------- #
# Each container's own codecs (codex 4581559119)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["dolphin_rvz", "dolphin_wia"])
def test_each_modes_default_is_a_codec_its_container_takes(mode) -> None:
    """dolphin-tool refuses zstd in a WIA container, so one tool-wide default
    cannot serve both. The default is the mode's, checked against the plugin's
    table of what each container accepts, not against a string written here."""
    from services.tools.dolphin import CONTAINER_CODECS, container_of

    allowed = CONTAINER_CODECS[container_of(mode)]
    resolved = registry.for_mode(mode).compression_for(mode, None)
    assert resolved.split(":", 1)[0] in allowed
    assert registry.spec(mode).codecs == allowed


@pytest.mark.asyncio
async def test_a_wia_job_with_zstd_is_refused_before_the_tool_runs(env) -> None:
    from app.models import ConversionMode, JobCreateRequest
    from app.routes import convert as convert_routes
    from fastapi import HTTPException

    iso = _gamecube(env["tmp"] / "gc" / "Melee.iso")
    with pytest.raises(HTTPException) as refused:
        await convert_routes.create_job(JobCreateRequest(
            file_path=iso, mode=ConversionMode.DOLPHIN_WIA, compression="zstd:19",
        ))

    assert refused.value.status_code == 400
    assert "zstd" in refused.value.detail
    # A job that got past the routes (a Rule saved before this) fails at the
    # plugin, with the same reason, and dolphin-tool never runs.
    tool = registry.for_mode("dolphin_wia")
    with pytest.raises(ValueError, match="zstd"):
        tool.convert(iso, str(env["tmp"] / "Melee.wia"), "dolphin_wia", compression="zstd")
    assert env["commands"] == []


# --------------------------------------------------------------------------- #
# Each codec's own level range (Dolphin rejects lzma2 at 19)
# --------------------------------------------------------------------------- #


def test_every_codec_a_container_takes_has_a_valid_level_range() -> None:
    """From DiscIO/WIABlob.cpp GetAllowedCompressionLevels: bzip2, lzma and
    lzma2 take 1-9, zstd 1-22 (the GUI range), and none takes no level."""
    from services.tools.dolphin import CODEC_LEVELS, CONTAINER_CODECS

    for container, codecs in CONTAINER_CODECS.items():
        for codec in codecs:
            assert codec in CODEC_LEVELS, (container, codec)
            levels = CODEC_LEVELS[codec]
            if codec == "none":
                assert levels is None
                continue
            low, high, default = levels
            assert 1 <= low <= default <= high, (codec, levels)
    for mode in ("dolphin_rvz", "dolphin_wia"):
        spec = registry.spec(mode)
        for codec in spec.codecs:
            assert spec.level_range(codec) == CODEC_LEVELS[codec], (mode, codec)


@pytest.mark.parametrize("mode", ["dolphin_rvz", "dolphin_wia"])
def test_a_codec_without_a_level_gets_one_in_its_range(mode) -> None:
    from services.tools.dolphin import CODEC_LEVELS

    resolved = registry.for_mode(mode).compression_for(mode, "lzma2")
    codec, _, level = resolved.partition(":")
    low, high, _default = CODEC_LEVELS[codec]
    assert low <= int(level) <= high


@pytest.mark.asyncio
async def test_an_out_of_range_level_is_refused_before_the_tool_runs(env) -> None:
    from app.models import ConversionMode, JobCreateRequest
    from app.routes import convert as convert_routes
    from fastapi import HTTPException

    iso = _gamecube(env["tmp"] / "gc" / "Melee.iso")
    with pytest.raises(HTTPException) as refused:
        await convert_routes.create_job(JobCreateRequest(
            file_path=iso, mode=ConversionMode.DOLPHIN_WIA, compression="lzma2:19",
        ))

    assert refused.value.status_code == 400
    assert "lzma2" in refused.value.detail and "1-9" in refused.value.detail
    tool = registry.for_mode("dolphin_wia")
    with pytest.raises(ValueError, match="1-9"):
        tool.convert(iso, str(env["tmp"] / "Melee.wia"), "dolphin_wia", compression="lzma2:19")
    assert env["commands"] == []


def test_the_codec_is_read_after_a_priority_prefix(monkeypatch) -> None:
    """On Linux with `ionice`, `ionice -c 2 -n …` comes before dolphin-tool;
    its `-c` is the I/O class, not the codec (codex 4581651579)."""
    from services import dolphin_tool

    monkeypatch.setattr(
        dolphin_tool, "ioprio_prefix", lambda _owner: ["ionice", "-c", "2", "-n", "7"],
    )
    service = registry.get("dolphin")._service

    cmd = service._build_convert_command("dolphin_rvz", "in.iso", "out.rvz", compression="zstd:19")

    assert cmd[:3] == ["ionice", "-c", "2"]
    assert (dolphin_option(cmd, "-c"), dolphin_option(cmd, "-l")) == ("zstd", "19")


# --------------------------------------------------------------------------- #
# A chain step resolves its codec at the convert boundary (CodeRabbit 4581672840)
# --------------------------------------------------------------------------- #


@pytest.fixture(name="nkit_chain")
def _nkit_chain(env, monkeypatch):
    """nkit_to_rvz with the restore step faked (it writes a GameCube ISO) and
    the RVZ step's dolphin-tool command captured at its runner."""
    from services.tools import chain as chain_mod

    work = env["tmp"] / "chainwork"

    def scratch(prefix=""):
        work.mkdir(parents=True, exist_ok=True)
        return str(work)

    async def no_tagging(*_args, **_kwargs):
        return None

    def restore(_input_path, output_path, _mode, **_kwargs):
        async def run():
            _gamecube(Path(output_path))
            yield {"progress": 100, "message": "restored"}

        return run()

    monkeypatch.setattr(chain_mod, "create_scratch_dir", scratch)
    monkeypatch.setattr(registry.get("dolphin"), "post_convert", no_tagging)
    monkeypatch.setattr(registry.get("nkit"), "convert", restore)
    source = env["tmp"] / "Melee.nkit.iso"
    source.write_bytes(b"x" * 1000)
    return {"source": str(source), "out": str(env["tmp"] / "Melee.rvz")}


async def _run_chain(chain, nkit_chain, compression=None) -> None:
    async for _ in chain.convert(
        nkit_chain["source"], nkit_chain["out"], "nkit_to_rvz", compression=compression,
    ):
        pass


@pytest.mark.asyncio
async def test_the_nkit_chain_gives_its_rvz_step_the_default_codec(env, nkit_chain) -> None:
    """nkit_to_rvz advertises no compression, so its RVZ step used to reach
    dolphin-tool with no ``-c``, which dolphin-tool refuses."""
    await _run_chain(registry.for_mode("nkit_to_rvz"), nkit_chain)

    (cmd,) = env["commands"]
    assert (dolphin_option(cmd, "-c"), dolphin_option(cmd, "-l")) == ("zstd", "19")


@pytest.mark.asyncio
async def test_a_chain_step_keeps_an_explicit_compression(
    env, nkit_chain, monkeypatch,
) -> None:
    chain = registry.for_mode("nkit_to_rvz")
    spec = dataclasses.replace(chain.spec("nkit_to_rvz"), supports_compression=True)
    monkeypatch.setattr(chain, "modes", tuple(
        spec if m.mode == "nkit_to_rvz" else m for m in chain.modes
    ))

    await _run_chain(chain, nkit_chain, compression="lzma:7")

    (cmd,) = env["commands"]
    assert (dolphin_option(cmd, "-c"), dolphin_option(cmd, "-l")) == ("lzma", "7")
