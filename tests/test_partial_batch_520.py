"""A batch never fails over one file, and Rules turn on when set (#520).

* A destination another live job holds skips that one file, reported with
  code ``output_claimed``; the rest of the batch is queued. The sweep and the
  Inbox still ask for all-or-nothing (they pass no skip list).
* An out-of-range compression level is clamped to the codec's range.
* A sweep counts the Rules it passed over because they are off, so Preview and
  Run now can say why nothing ran; the editor turns a Rule on when its target
  is picked.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.models import BatchJobCreateRequest, ConversionMode, DuplicateAction
from app.routes import convert as convert_routes
from app.services.job_manager import JobManager, OutputClaimedError
from services.tools import registry

from .test_frontend_rules_view import _find_node
from .test_platform_detect import _gamecube

_SRC = Path(__file__).resolve().parents[1] / "src"
_SUMMARY = _SRC / "lib" / "rules" / "sweepSummary.js"


# --------------------------------------------------------------------------- #
# One claimed destination skips one file
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_claimed_destination_skips_that_file_and_queues_the_rest(tmp_path) -> None:
    sources = [tmp_path / f"{name}.iso" for name in ("a", "b", "c")]
    for source in sources:
        source.write_bytes(b"x")
    manager = JobManager(max_concurrent=1, max_job_history=10)
    # Holds taken.chd, so the batch's first file is skipped.
    await manager.create_job(
        str(sources[0]), ConversionMode.CREATECD, output_path=str(tmp_path / "taken.chd"),
    )
    skipped: list = []
    jobs = await manager.create_jobs_atomic(
        [
            {"file_path": str(sources[1]), "output_path": str(tmp_path / "taken.chd")},
            {"file_path": str(sources[2]), "output_path": str(tmp_path / "c.chd")},
        ],
        ConversionMode.CREATECD,
        skipped=skipped,
    )
    assert [job.file_path for job in jobs] == [str(sources[2])]
    assert [path for path, _reason in skipped] == [str(sources[1])]
    assert "already writing taken.chd" in skipped[0][1]


@pytest.mark.asyncio
async def test_without_a_skip_list_the_batch_stays_all_or_nothing(tmp_path) -> None:
    """The sweep and the Inbox rely on it: every path is queued or none is."""
    sources = [tmp_path / f"{name}.iso" for name in ("a", "b", "c")]
    for source in sources:
        source.write_bytes(b"x")
    manager = JobManager(max_concurrent=1, max_job_history=10)
    held = await manager.create_job(
        str(sources[0]), ConversionMode.CREATECD, output_path=str(tmp_path / "taken.chd"),
    )
    with pytest.raises(OutputClaimedError):
        await manager.create_jobs_atomic(
            [
                {"file_path": str(sources[2]), "output_path": str(tmp_path / "c.chd")},
                {"file_path": str(sources[1]), "output_path": str(tmp_path / "taken.chd")},
            ],
            ConversionMode.CREATECD,
        )
    assert [job.id for job in manager.get_all_jobs()] == [held.id]


@pytest.mark.asyncio
async def test_the_batch_route_reports_a_claimed_file_and_queues_the_rest(route_env, monkeypatch):
    tmp = route_env["tmp_path"]
    first, second = _gamecube(tmp / "a.iso"), _gamecube(tmp / "b.iso")

    async def fake_atomic(job_specs, mode, *, skipped=None, **_kwargs):
        assert skipped is not None, "the batch route asks for skips, not a refusal"
        skipped.append((job_specs[0]["file_path"], "Another queued job is writing a.rvz"))
        return [convert_routes.ConversionJob(
            id="job-b", file_path=job_specs[1]["file_path"], filename="b.iso",
            output_path=job_specs[1]["output_path"], mode=mode, status="queued",
            created_at="2026-11-20T00:00:00Z",
        )]

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    result = await convert_routes.create_batch_jobs(
        BatchJobCreateRequest(
            file_paths=[first, second], mode=ConversionMode.DOLPHIN_RVZ,
            duplicate_action=DuplicateAction.SKIP,
        ),
        report_skipped=True,
    )
    assert [job.id for job in result.jobs] == ["job-b"]
    assert [(s.path, s.code) for s in result.skipped] == [(first, "output_claimed")]


# --------------------------------------------------------------------------- #
# Levels are clamped
# --------------------------------------------------------------------------- #

def test_a_level_outside_the_codec_range_is_clamped() -> None:
    from services.tools.base import clamp_levels

    spec = registry.spec("dolphin_rvz")
    assert clamp_levels(spec, "zstd:99") == "zstd:22"
    assert clamp_levels(spec, "zstd:0") == "zstd:1"
    assert clamp_levels(spec, "zstd:7") == "zstd:7"
    assert clamp_levels(spec, "none") == "none"
    assert clamp_levels(spec, None) is None


@pytest.mark.asyncio
async def test_the_batch_route_queues_a_clamped_level(route_env):
    disc = _gamecube(route_env["tmp_path"] / "melee.iso")
    await convert_routes.create_batch_jobs(
        BatchJobCreateRequest(
            file_paths=[disc], mode=ConversionMode.DOLPHIN_RVZ, compression="zstd:40",
        ),
    )
    assert route_env["queued"][0]["compression"] == "zstd:22"


# --------------------------------------------------------------------------- #
# Why nothing ran
# --------------------------------------------------------------------------- #

def _summary(script: str):
    node = _find_node()
    if node is None:
        pytest.skip("node is not installed")
    src = _SUMMARY.read_text(encoding="utf-8").replace("export function ", "function ")
    out = subprocess.run(
        [node, "-e", f"{src}\nconsole.log(JSON.stringify(({script})()));"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(("result", "says"), [
    ({"stopped_reason": "no_rules"}, "no console is set"),
    ({"skipped_paused": 2, "considered": 0}, "2 consoles are off"),
    ({"skipped_paused": 1, "considered": 0}, "1 console is off"),
    ({"considered": 0}, "no games match"),
    ({"considered": 2, "skipped_existing": 2}, "all 2 are already converted"),
    ({"considered": 3, "skipped_active": 3}, "all 3 are already in the queue"),
    ({"considered": 0, "errors": [{"platform_id": "ps2", "error": "tool_not_ready"}]},
     "could not run"),
    ({"stopped_reason": "not_configured"}, "not set up"),
])
def test_an_empty_run_says_why(result, says) -> None:
    text = _summary(f"() => sweepToast({json.dumps(result)}, 'Queued', 'Checking')")
    assert text.startswith("Nothing to convert"), text
    assert says in text, text


def test_a_run_that_queued_says_how_many() -> None:
    assert _summary("() => sweepToast({ queued: 3, checks: 1 }, 'Queued', 'Checking')") == (
        "Queued 3 conversions, checking 1 game"
    )


def test_picking_a_target_turns_the_rule_on() -> None:
    # The console table (#541): a picked format turns the console on, and the
    # run card says why nothing ran.
    table = (_SRC / "lib" / "rules" / "ruleTable.js").read_text(encoding="utf-8")
    body = table[table.index("export function ruleUpdate("):]
    body = body[: body.index("\n}\n")]
    assert "enabled: true" in body, "a newly picked target must enable the Rule"
    automation = (_SRC / "lib" / "components" / "library" / "LibraryAutomation.svelte")
    assert "sweepSummary.js" in automation.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_sweep_counts_the_rules_that_are_off(tmp_path, monkeypatch) -> None:
    from .test_folder_library_303 import _register

    from services import db as _db
    from services import libraries, platform_detect
    from services.romm import settings as romm_settings

    from config import settings as app_settings

    from .test_platform_detect import _ps2

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    platform_detect.clear_cache()
    volume = tmp_path / "vol"
    roms = volume / "roms"
    _ps2(roms / "ps2" / "Ico.iso")
    _gamecube(roms / "gc" / "Melee.iso")
    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))

    async def always(_tool):
        return True

    monkeypatch.setattr(registry, "tool_is_ready", always)
    try:
        from routes import libraries as routes

        lib = await _register(roms)
        await routes.put_library_rules(lib["id"], {"rules": {
            "ps2": {"mode": "createdvd", "enabled": False},
            "ngc": {"mode": "dolphin_rvz", "enabled": False},
        }})
        preview = await routes.preview_library(lib["id"])
        assert preview["queued"] == 0
        assert preview["skipped_paused"] == 2
    finally:
        platform_detect.clear_cache()
        libraries.reset_for_tests()
        romm_settings.reset_for_tests()
        if _db.engine is not None:
            _db.engine.dispose()
        _db.engine = None
        _db.SessionLocal = None
