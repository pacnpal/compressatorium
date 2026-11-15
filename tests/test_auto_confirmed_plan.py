"""``/jobs/auto`` queues only the plan the user confirmed (Codex 4576967499).

The client sends the confirmed actionable rows, each as ``{file_path, mode}``;
an Image the preview skipped is never sent. The route replans those paths and
answers 409 "The plan changed", with the new plan, when any item's target
differs (another tool, or now skipped). Nothing is queued then: the client
shows the new plan and asks again.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import AutoJobCreateRequest, AutoJobItem, AutoPlanRequest
from app.routes import convert as convert_routes

from .test_auto_convert import _fresh_cache, _route_env  # noqa: F401
from .test_platform_detect import _blank, _gamecube, _ps2


async def _preview(paths: list[str]) -> dict:
    plan = await convert_routes.auto_plan(AutoPlanRequest(file_paths=paths))
    return {Path(row.file_path).name: row for row in plan}


def _items(rows) -> list[AutoJobItem]:
    return [AutoJobItem(file_path=r.file_path, mode=r.mode) for r in rows if r.mode]


@pytest.mark.asyncio
async def test_the_confirmed_items_are_queued_and_nothing_else(route_env):
    tmp = route_env["tmp_path"]
    ps2 = _ps2(tmp / "pile" / "ps2.iso")
    mystery = _blank(tmp / "pile" / "mystery.iso")
    rows = await _preview([ps2, mystery])
    assert rows["mystery.iso"].mode is None          # the preview skips it

    result = await convert_routes.create_auto_jobs(
        AutoJobCreateRequest(items=_items(rows.values())),
    )

    assert route_env["queued"] == [
        {"mode": "createdvd", "files": ["ps2.iso"], "compression": "zlib,lzma"},
    ]
    assert [Path(p.file_path).name for p in result.plan] == ["ps2.iso"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["other-target", "now-skipped"])
async def test_a_plan_that_changed_since_the_preview_is_refused(route_env, change):
    tmp = route_env["tmp_path"]
    ps2 = _ps2(tmp / "pile" / "ps2.iso")
    gc = _gamecube(tmp / "pile" / "gc.iso")
    rows = await _preview([ps2, gc])
    items = _items(rows.values())

    # Between preview and submit, one Image becomes something else.
    if change == "other-target":
        _gamecube(Path(ps2))           # now a GameCube disc: dolphin_rvz
    else:
        Path(ps2).write_bytes(b"\0" * 4096)   # now unreadable: skipped

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(AutoJobCreateRequest(items=items))

    assert exc.value.status_code == 409
    assert "plan changed" in exc.value.detail["message"].lower()
    new = {Path(row["file_path"]).name: row for row in exc.value.detail["plan"]}
    assert set(new) == {"ps2.iso", "gc.iso"}
    assert new["ps2.iso"]["mode"] == ("dolphin_rvz" if change == "other-target" else None)
    assert route_env["queued"] == []                 # nothing queued


@pytest.mark.asyncio
async def test_an_item_naming_another_target_than_the_plan_is_refused(route_env):
    ps2 = _ps2(route_env["tmp_path"] / "pile" / "ps2.iso")

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(AutoJobCreateRequest(
            items=[AutoJobItem(file_path=ps2, mode="createcd")],
        ))

    assert exc.value.status_code == 409
    assert route_env["queued"] == []


@pytest.mark.asyncio
async def test_a_path_listed_twice_is_refused(route_env):
    ps2 = _ps2(route_env["tmp_path"] / "pile" / "ps2.iso")

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(AutoJobCreateRequest(items=[
            AutoJobItem(file_path=ps2, mode="createdvd"),
            AutoJobItem(file_path=ps2, mode="createdvd"),
        ]))

    assert exc.value.status_code == 400
    assert route_env["queued"] == []


@pytest.mark.asyncio
async def test_no_items_queues_nothing(route_env):
    result = await convert_routes.create_auto_jobs(AutoJobCreateRequest(items=[]))

    assert result.jobs == [] and result.plan == []
    assert route_env["queued"] == []
