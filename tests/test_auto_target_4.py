"""Auto is the way to convert (restructure 2/6, plan item 4).

The Convert panel always plans through ``/api/jobs/auto-plan`` and queues
through ``/api/jobs/auto``. A ``target`` (a format such as ``chd``, or a mode
id) narrows each game to the modes that write it, a per-file console override
plans a game whose console could not be worked out, and ``/api/jobs/auto``
carries the check and delete choices to each group that can honour them. The
CLI's ``--to`` plans through the same function.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from services.platform_detect import plan_auto

from app import cli
from app.models import AutoJobCreateRequest, AutoJobItem, AutoPlanRequest
from app.routes import convert as convert_routes

from .test_platform_detect import _blank, _gamecube, _ps2, _write

pytestmark = pytest.mark.usefixtures("fresh_detect_cache", "tmp_volume")


def _ps1_cue(folder: Path) -> str:
    _blank(folder / "game.bin")
    return _write(folder / "game.cue", b'FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n')


def test_target_chd_routes_a_ps1_cue_to_createcd(tmp_path):
    cue = _ps1_cue(tmp_path / "psx")

    [entry] = plan_auto([cue], target="chd")

    assert (entry.mode, entry.skip_reason) == ("createcd", None)


def test_target_chd_skips_a_gamecube_iso_and_says_why(tmp_path):
    iso = _gamecube(tmp_path / "pile" / "gc.iso")

    [entry] = plan_auto([iso], target="chd")

    assert entry.mode is None
    assert entry.skip_reason == "No converter here makes CHD for GameCube games"


def test_target_names_the_format_it_cannot_make_from_an_extension(tmp_path):
    path = _write(tmp_path / "gc" / "game.rvz", b"RVZ\x01" + b"\0" * 60)

    [entry] = plan_auto([path], target="chd")

    assert entry.skip_reason == "No converter here makes CHD from .rvz files"


def test_no_target_keeps_the_auto_choice(tmp_path):
    iso = _gamecube(tmp_path / "pile" / "gc.iso")

    [entry] = plan_auto([iso])

    assert entry.mode == "dolphin_rvz"


def test_an_override_plans_an_unsorted_game(tmp_path):
    mystery = _blank(tmp_path / "downloads" / "mystery.iso")

    [before] = plan_auto([mystery])
    [after] = plan_auto([mystery], platform_overrides={mystery: "ps2"})

    assert before.mode is None
    assert before.skip_reason.startswith("Unknown console: ")
    assert (after.detection.how, after.detection.platform) == ("chosen", "ps2")
    assert after.mode == "createdvd"


def test_an_override_never_replaces_a_decided_console(tmp_path):
    iso = _gamecube(tmp_path / "pile" / "gc.iso")

    [entry] = plan_auto([iso], platform_overrides={iso: "ps2"})

    assert (entry.detection.platform, entry.mode) == ("ngc", "dolphin_rvz")


def test_a_conflicted_game_is_skipped_in_plain_words(tmp_path):
    clash = _gamecube(tmp_path / "ps2" / "clash.iso")

    [entry] = plan_auto([clash], target="chd")

    assert entry.mode is None
    assert entry.skip_reason.startswith("Console unclear: ")


def test_a_target_plans_an_unsorted_game_its_format_reads(tmp_path):
    # A raw track with no console still goes ahead when CHD reads it.
    cue = _ps1_cue(tmp_path / "downloads")

    [entry] = plan_auto([cue], target="chd")

    assert entry.mode == "createcd"


def test_the_plan_names_each_output(tmp_path):
    iso = _gamecube(tmp_path / "pile" / "gc.iso")

    [entry] = plan_auto([iso])

    assert entry.output_name == "gc.rvz"


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

@pytest.fixture
def queue_env(route_env, monkeypatch):
    calls: list[dict] = []

    async def fake_atomic(job_specs, mode, **kwargs):
        calls.append({"mode": mode.value, "files": [Path(s["file_path"]).name for s in job_specs],
                      **kwargs})
        return []

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)

    # Every tool can run, so a group never drops out of the plan for a
    # missing binary on the test machine.
    async def every_tool():
        return [tool.id for tool in convert_routes.registry.all()]

    monkeypatch.setattr(convert_routes.registry, "ready_tool_ids", every_tool)
    return {**route_env, "calls": calls}


async def _queue(paths: list[str], **kwargs):
    target = kwargs.pop("target", None)
    plan = await convert_routes.auto_plan(AutoPlanRequest(file_paths=paths, target=target))
    return await convert_routes.create_auto_jobs(AutoJobCreateRequest(
        items=[AutoJobItem(file_path=row.file_path, mode=row.mode) for row in plan if row.mode],
        target=target, **kwargs,
    ))


@pytest.mark.asyncio
async def test_delete_on_verify_reaches_only_the_groups_that_can_delete(queue_env):
    folder = queue_env["tmp_path"] / "pile"
    ps2 = _ps2(folder / "ps2.iso")
    ps3 = folder / "ps3game"
    (ps3 / "PS3_GAME").mkdir(parents=True)
    _write(ps3 / "PS3_DISC.SFB", b"\x2ePS3" + b"\0" * 60)
    _write(ps3 / "PS3_GAME" / "PARAM.SFO", b"\0PSF" + b"\0" * 60)
    _write(ps3 / "PS3_GAME" / "USRDIR" / "EBOOT.BIN", b"\0" * 64)

    await _queue([ps2, str(ps3)], delete_on_verify=True, container_only=True)

    by_mode = {call["mode"]: call for call in queue_env["calls"]}
    assert by_mode["createdvd"]["delete_on_verify"] is True
    assert by_mode["createdvd"]["container_only"] is True
    # The PS3 folder's group keeps its originals (CodeRabbit 4654265240: the
    # group must be there, not silently missing).
    assert by_mode["folder_to_iso"]["delete_on_verify"] is False
    assert by_mode["folder_to_iso"]["container_only"] is False


@pytest.mark.asyncio
async def test_verify_after_reaches_the_jobs(queue_env):
    ps2 = _ps2(queue_env["tmp_path"] / "pile" / "ps2.iso")

    await _queue([ps2], verify_after=True)

    [call] = queue_env["calls"]
    assert (call["verify_after"], call["delete_on_verify"]) == (True, False)


@pytest.mark.asyncio
async def test_the_route_queues_the_target_it_planned(queue_env):
    cue = _ps1_cue(queue_env["tmp_path"] / "psx")

    result = await _queue([cue], target="chd")

    assert [call["mode"] for call in queue_env["calls"]] == ["createcd"]
    assert result.plan[0].output_name == "game.chd"


@pytest.mark.asyncio
async def test_an_override_reaches_the_queue(queue_env):
    mystery = _blank(queue_env["tmp_path"] / "downloads" / "mystery.iso")
    overrides = {mystery: "ps2"}
    plan = await convert_routes.auto_plan(
        AutoPlanRequest(file_paths=[mystery], platform_overrides=overrides),
    )

    await convert_routes.create_auto_jobs(AutoJobCreateRequest(
        items=[AutoJobItem(file_path=mystery, mode=plan[0].mode)],
        platform_overrides=overrides,
    ))

    assert [call["mode"] for call in queue_env["calls"]] == ["createdvd"]


# --------------------------------------------------------------------------- #
# The CLI's --to plans through the same decision
# --------------------------------------------------------------------------- #

def test_the_cli_to_routes_like_the_plan(tmp_path):
    cue = _ps1_cue(tmp_path / "psx")
    gc = _gamecube(tmp_path / "pile" / "gc.iso")
    clash = _gamecube(tmp_path / "ps2" / "clash.iso")
    paths = [cue, gc, clash]
    items = [cli.Item(path=p, explicit=True) for p in paths]
    args = SimpleNamespace(to="chd", any_platform=False, compression=None)
    ready = ["chdman", "dolphin"]

    routed = {i.path: (i.mode, i.reason) for i in cli._route_to(items, args, {}, ready)}
    planned = {t.file_path: (t.mode, t.skip_reason) for t in plan_auto(
        paths, target="chd", ready_tool_ids=ready,
    )}

    assert routed == planned


@pytest.mark.asyncio
async def test_a_game_whose_new_file_exists_is_skipped_in_the_plan(queue_env):
    """CodeRabbit 4654265187: the queue skips a game whose new file is already
    there, so the plan says so up front and its delete preview never covers it."""
    folder = queue_env["tmp_path"] / "pile"
    done = _ps2(folder / "done.iso")
    _write(folder / "done.chd", b"MComprHD" + b"\0" * 56)
    todo = _ps2(folder / "todo.iso")

    plan = await convert_routes.auto_plan(AutoPlanRequest(file_paths=[done, todo]))

    by_name = {Path(row.file_path).name: row for row in plan}
    assert by_name["done.iso"].mode is None
    assert by_name["done.iso"].skip_reason == "done.chd already exists"
    assert by_name["todo.iso"].mode == "createdvd"
