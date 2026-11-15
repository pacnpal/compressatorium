"""Auto convert: each selected Image goes to its Platform's default target.

The default target is the first create-direction mode the registry lists for
the Platform that takes the file. Conflicted and Unsorted Images are skipped
with a reason. Tools are stubbed at the queue; nothing runs a binary.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from services import platform_detect
from services.platform_detect import plan_auto

from app.models import AutoJobCreateRequest, AutoJobItem, AutoPlanRequest
from app.routes import convert as convert_routes

from .test_disc_id import _make_iso
from .test_platform_detect import _blank, _gamecube, _ps1, _ps2, _psp, _wii, _write
from .test_xiso_service import build_image

# The detection cache is cleared and tmp_path is the configured Volume
# (tests/conftest.py), so the folder walk reaches every folder a test
# builds under it and nothing above it.
pytestmark = pytest.mark.usefixtures("fresh_detect_cache", "tmp_volume")


def _by_path(plan):
    return {Path(entry.file_path).name: entry for entry in plan}


async def _confirmed(paths: list[str], **kwargs) -> AutoJobCreateRequest:
    """What the client sends after previewing ``paths``: the actionable rows."""
    plan = await convert_routes.auto_plan(AutoPlanRequest(file_paths=paths, **kwargs))
    return AutoJobCreateRequest(
        items=[AutoJobItem(file_path=row.file_path, mode=row.mode) for row in plan if row.mode],
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Default target per Platform
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("builder", "name", "mode"),
    [
        (_ps2, "game.iso", "createdvd"),
        (_psp, "game.iso", "createdvd"),
        (_ps1, "game.iso", "createcd"),
        (_gamecube, "game.iso", "dolphin_rvz"),
        (_wii, "game.iso", "dolphin_rvz"),
        # The most specific input extension wins, as in tool_for_verify.
        (_gamecube, "game.nkit.iso", "nkit_to_rvz"),
    ],
)
def test_default_target_is_the_first_create_mode_for_the_platform(
    tmp_path, builder, name, mode,
):
    path = builder(tmp_path / "downloads" / name)

    [entry] = plan_auto([path])

    assert (entry.mode, entry.skip_reason) == (mode, None)


def _raw_ps2_cd(folder: Path) -> tuple[str, str]:
    """A PS2 CD-ROM title as a redump cue/bin: a raw 2352-byte Mode 2 track."""
    cooked = _make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_200.71;1\n"})
    raw = bytearray()
    for start in range(0, len(cooked), 2048):
        sector = bytearray(2352)
        sector[24:24 + 2048] = cooked[start:start + 2048]
        raw += sector
    track = _write(folder / "game.bin", bytes(raw))
    cue = _write(folder / "game.cue", b'FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    return cue, track


def test_ps2_cd_cue_goes_to_the_cd_command(tmp_path):
    cue, track = _raw_ps2_cd(tmp_path / "downloads")

    plan = _by_path(plan_auto([cue, track]))

    assert plan["game.cue"].detection.platform == "ps2"
    # A PS2 CD-ROM title is a plain Mode 2 CD: createcd reads its track sheet.
    # createdvd reads only a flat 2048-byte image, so it never gets the .cue.
    assert plan["game.cue"].mode == "createcd"
    # The raw track has no table of contents; its .cue is the Image.
    assert plan["game.bin"].mode is None
    assert ".bin" in plan["game.bin"].skip_reason


@pytest.mark.parametrize("folder", ["arcade", "mame"])
@pytest.mark.parametrize("name", ["game.gdi", "game.cue", "game.iso"])
def test_arcade_cd_images_never_get_a_hard_disk_command(tmp_path, folder, name):
    path = _blank(tmp_path / folder / name)

    [entry] = plan_auto([path])

    assert entry.mode not in {"createraw", "createhd"}
    assert entry.mode == "createcd"


@pytest.mark.parametrize(
    ("folder", "name", "mode"),
    [
        ("pcenginecd", "game.cue", "createcd"),
        ("cdi", "game.cue", "createcd"),
        ("megadrive", "game.md", "romz_7z"),
        ("n3ds", "game.3ds", "z3ds_compress"),
    ],
)
def test_frontend_folder_names_reach_a_target(tmp_path, folder, name, mode):
    [entry] = plan_auto([_blank(tmp_path / folder / name)])

    assert entry.mode == mode


def test_a_modes_read_set_never_widens_what_it_accepts():
    # reads_extensions narrows input_extensions for Auto only; a read set that
    # names something plan_job would refuse would plan jobs that never queue.
    from services.tools import registry

    for spec in registry.mode_specs():
        assert spec.reads_extensions <= spec.input_extensions, spec.mode


def test_manual_chdman_create_modes_keep_their_inputs():
    # Manual selection reads input_extensions, which the Auto read set leaves
    # alone: every chdman create mode still takes all four source extensions.
    from services.tools import registry

    for mode in ("createraw", "createhd", "createcd", "createdvd", "createld"):
        assert registry.spec(mode).input_extensions == {".iso", ".cue", ".bin", ".gdi"}


def test_xbox_goes_to_the_xbox_tool(tmp_path):
    path = tmp_path / "downloads" / "halo.iso"
    path.parent.mkdir()
    build_image(path, layout="XGD2", exe="default.xex")

    [entry] = plan_auto([str(path)])

    assert entry.mode == "xiso_rewrite"


def test_folder_named_platform_uses_that_platform(tmp_path):
    path = _write(tmp_path / "ps2" / "game.cso", b"CISO" + b"\0" * 60)

    [entry] = plan_auto([path])

    assert (entry.detection.how, entry.mode) == ("folder", "cso_to_chd")


def test_conflicted_and_unsorted_images_are_skipped_with_a_reason(tmp_path):
    conflicted = _gamecube(tmp_path / "ps2" / "clash.iso")
    unsorted = _blank(tmp_path / "downloads" / "mystery.iso")

    plan = _by_path(plan_auto([conflicted, unsorted]))

    assert plan["clash.iso"].mode is None
    assert "conflict" in plan["clash.iso"].skip_reason.lower()
    assert "ngc" in plan["clash.iso"].skip_reason
    assert plan["mystery.iso"].mode is None
    assert "unsorted" in plan["mystery.iso"].skip_reason.lower()


def test_platform_with_no_target_for_the_file_type_is_skipped(tmp_path):
    # A PS2 folder, but .rvz is a GameCube/Wii container: no PS2 mode takes it.
    path = _write(tmp_path / "ps2" / "odd.rvz", b"RVZ\x01" + b"\0" * 60)

    [entry] = plan_auto([path])

    assert entry.mode is None
    assert entry.skip_reason == "No ps2 target reads .rvz files"


def test_an_image_already_in_its_target_format_is_left_alone(tmp_path):
    path = _write(tmp_path / "gc" / "game.rvz", b"RVZ\x01" + b"\0" * 60)

    [entry] = plan_auto([path])

    assert (entry.detection.platform, entry.mode) == ("ngc", None)
    assert entry.skip_reason == "Already RVZ"


def test_other_dolphin_formats_still_convert_to_rvz(tmp_path):
    [entry] = plan_auto([_write(tmp_path / "gc" / "game.wia", b"WIA\x01" + b"\0" * 60)])

    assert entry.mode == "dolphin_rvz"


@pytest.mark.parametrize(
    ("folder", "name", "platform"),
    [
        ("ngpc", "game.ngc", "neo-geo-pocket-color"),
        ("wonderswancolor", "game.wsc", "wonderswan-color"),
    ],
)
def test_a_platform_no_tool_converts_says_so(tmp_path, folder, name, platform):
    [entry] = plan_auto([_blank(tmp_path / folder / name)])

    assert entry.mode is None
    assert entry.skip_reason == f"No tool converts {platform} Images"


def test_saved_compression_follows_the_tool(tmp_path):
    ps2 = _ps2(tmp_path / "a" / "ps2.iso")
    gc = _gamecube(tmp_path / "a" / "gc.iso")

    plan = _by_path(plan_auto(
        [ps2, gc], compression_prefs={"chdman": "zlib,lzma", "dolphin": "zstd:19"},
    ))

    assert plan["ps2.iso"].compression == "zlib,lzma"
    assert plan["gc.iso"].compression == "zstd:19"


def test_a_stored_iso_handling_preference_is_ignored(tmp_path):
    # The old ISO handling toggle is gone; a value left in saved preferences
    # must neither break Auto nor steer it.
    gc = _gamecube(tmp_path / "a" / "gc.iso")

    [entry] = plan_auto([gc], compression_prefs={"iso_handling": "chdman"})

    assert (entry.mode, entry.compression) == ("dolphin_rvz", None)


def test_no_saved_compression_means_the_tool_default(tmp_path):
    [entry] = plan_auto([_ps2(tmp_path / "a" / "ps2.iso")], compression_prefs={})

    assert entry.compression is None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def _mixed_selection(tmp_path: Path) -> list[str]:
    return [
        _ps2(tmp_path / "pile" / "ps2.iso"),
        _gamecube(tmp_path / "pile" / "gc.iso"),
        _ps1(tmp_path / "pile" / "ps1.iso"),
        _gamecube(tmp_path / "ps2" / "clash.iso"),
        _blank(tmp_path / "pile" / "mystery.iso"),
    ]


@pytest.mark.asyncio
async def test_auto_batch_sends_each_image_to_its_platform_target(route_env):
    paths = _mixed_selection(route_env["tmp_path"])
    preview = await convert_routes.auto_plan(AutoPlanRequest(file_paths=paths))
    skipped = {Path(p.file_path).name: p.skip_reason for p in preview if p.skip_reason}
    assert set(skipped) == {"clash.iso", "mystery.iso"}

    request = await _confirmed(paths)
    result = await convert_routes.create_auto_jobs(request)

    # The skipped Images were never sent, so never queued.
    assert {Path(i.file_path).name for i in request.items} == {"ps1.iso", "ps2.iso", "gc.iso"}
    queued = sorted(route_env["queued"], key=lambda q: q["mode"])
    assert queued == [
        {"mode": "createcd", "files": ["ps1.iso"], "compression": "zlib,lzma"},
        {"mode": "createdvd", "files": ["ps2.iso"], "compression": "zlib,lzma"},
        {"mode": "dolphin_rvz", "files": ["gc.iso"], "compression": "zstd:19"},
    ]
    assert not any(p.skip_reason for p in result.plan)


@pytest.mark.asyncio
async def test_auto_plan_previews_without_queueing(route_env):
    paths = _mixed_selection(route_env["tmp_path"])

    plan = await convert_routes.auto_plan(AutoPlanRequest(file_paths=paths))

    assert route_env["queued"] == []
    rows = {Path(p.file_path).name: p for p in plan}
    assert rows["ps2.iso"].platform == "ps2"
    assert rows["ps2.iso"].how == "header"
    assert rows["ps2.iso"].mode == "createdvd"
    assert rows["clash.iso"].how == "conflict"
    assert rows["clash.iso"].mode is None


def test_two_images_with_one_output_keep_the_first_and_say_why(tmp_path):
    first = _gamecube(tmp_path / "pile" / "game.iso")
    second = _gamecube(tmp_path / "pile" / "game.nkit.iso")

    plan = _by_path(plan_auto([first, second]))

    assert plan["game.iso"].mode == "dolphin_rvz"
    assert plan["game.nkit.iso"].mode is None
    assert "game.rvz" in plan["game.nkit.iso"].skip_reason
    assert "game.iso" in plan["game.nkit.iso"].skip_reason


def test_output_collisions_keep_the_input_the_batch_path_would_keep(tmp_path):
    # The batch collapse keeps the .cue over the .iso (input_priority); the
    # plan must agree whatever order the selection arrives in.
    iso = _ps1(tmp_path / "psx" / "game.iso")
    cue = _write(tmp_path / "psx" / "game.cue", b'FILE "game.bin" BINARY\n')

    plan = _by_path(plan_auto([iso, cue]))

    assert plan["game.cue"].mode == "createcd"
    assert plan["game.iso"].mode is None
    assert "game.cue" in plan["game.iso"].skip_reason


def test_output_collisions_follow_the_output_directory(tmp_path):
    # Different folders, one output directory: both would write out/game.rvz.
    first = _gamecube(tmp_path / "a" / "game.iso")
    second = _wii(tmp_path / "b" / "game.iso")

    plan = plan_auto([first, second], output_dir=str(tmp_path / "out"))

    assert [entry.mode for entry in plan] == ["dolphin_rvz", None]


@pytest.mark.asyncio
async def test_auto_route_queues_one_job_for_two_images_with_one_output(route_env):
    tmp = route_env["tmp_path"]
    paths = [_gamecube(tmp / "pile" / "game.iso"), _gamecube(tmp / "pile" / "game.nkit.iso")]
    preview = {Path(p.file_path).name: p for p in await convert_routes.auto_plan(
        AutoPlanRequest(file_paths=paths),
    )}
    assert preview["game.nkit.iso"].skip_reason

    await convert_routes.create_auto_jobs(await _confirmed(paths))

    assert route_env["queued"] == [
        {"mode": "dolphin_rvz", "files": ["game.iso"], "compression": "zstd:19"},
    ]


@pytest.mark.asyncio
async def test_auto_route_queues_nothing_when_a_later_group_fails_planning(
    route_env, monkeypatch,
):
    paths = _mixed_selection(route_env["tmp_path"])
    real_plan_job = convert_routes.plan_job

    async def plan_job(file_path, **kwargs):
        if file_path.endswith("gc.iso"):
            raise convert_routes.DeleteSnapshotError("source vanished")
        return await real_plan_job(file_path, **kwargs)

    monkeypatch.setattr(convert_routes, "plan_job", plan_job)

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(await _confirmed(paths))

    assert exc.value.status_code == 400
    assert route_env["queued"] == []


@pytest.mark.asyncio
async def test_auto_route_cancels_what_it_queued_when_a_later_group_fails(
    route_env, monkeypatch,
):
    paths = _mixed_selection(route_env["tmp_path"])
    queued_ids: list[str] = []
    cancelled: list[str] = []

    async def fake_atomic(job_specs, mode, **_kwargs):
        if mode.value == "dolphin_rvz":
            raise convert_routes.OutputClaimedError("taken by another job")
        job_id = f"job-{mode.value}"
        queued_ids.append(job_id)
        return [type("Job", (), {"id": job_id})()]

    async def fake_cancel(job_id):
        cancelled.append(job_id)
        return True

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    monkeypatch.setattr(convert_routes.job_manager, "cancel_job", fake_cancel)

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(await _confirmed(paths))

    assert exc.value.status_code == 409
    assert queued_ids, "the failing group must not be the first one queued"
    assert sorted(cancelled) == sorted(queued_ids)


@pytest.mark.asyncio
async def test_auto_route_names_a_job_that_finished_before_the_withdrawal(
    route_env, monkeypatch, caplog,
):
    paths = _mixed_selection(route_env["tmp_path"])

    async def fake_atomic(job_specs, mode, **_kwargs):
        if mode.value == "dolphin_rvz":
            raise convert_routes.OutputClaimedError("taken by another job")
        spec = job_specs[0]
        return [convert_routes.ConversionJob(
            id=f"job-{mode.value}",
            file_path=spec["file_path"],
            filename=Path(spec["file_path"]).name,
            output_path=spec["output_path"],
            mode=mode,
            status="completed",
            created_at="2026-11-14T00:00:00Z",
        )]

    async def fake_cancel(_job_id):
        return False  # already finished: nothing left to cancel

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    monkeypatch.setattr(convert_routes.job_manager, "cancel_job", fake_cancel)

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(await _confirmed(paths))

    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert "taken by another job" in detail["message"]
    assert "ps2.iso" in detail["message"]
    assert "ps2.iso" in caplog.text
    # The kept jobs come back in the shape a success returns them, so the
    # client can adopt them and keep their re-pin records.
    kept = {job["id"]: job for job in detail["jobs"]}
    assert "job-createdvd" in kept
    assert kept["job-createdvd"]["file_path"].endswith("ps2.iso")
    assert kept["job-createdvd"]["output_path"].endswith("ps2.chd")


@pytest.mark.asyncio
async def test_auto_route_withdraws_on_any_failure_not_only_http_errors(
    route_env, monkeypatch,
):
    paths = _mixed_selection(route_env["tmp_path"])
    cancelled: list[str] = []

    async def fake_atomic(job_specs, mode, **_kwargs):
        if mode.value == "dolphin_rvz":
            raise RuntimeError("queue fell over")
        return [type("Job", (), {"id": f"job-{mode.value}", "filename": "x"})()]

    async def fake_cancel(job_id):
        cancelled.append(job_id)
        return True

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    monkeypatch.setattr(convert_routes.job_manager, "cancel_job", fake_cancel)

    with pytest.raises(RuntimeError):
        await convert_routes.create_auto_jobs(await _confirmed(paths))

    assert cancelled


@pytest.mark.asyncio
async def test_auto_route_withdraws_earlier_groups_when_cancelled(route_env, monkeypatch):
    # A client disconnect cancels the request mid-loop: the groups already
    # queued are withdrawn, then the cancellation propagates.
    import asyncio

    paths = _mixed_selection(route_env["tmp_path"])
    queued_ids: list[str] = []
    cancelled: list[str] = []

    async def fake_atomic(job_specs, mode, **_kwargs):
        if mode.value == "dolphin_rvz":
            raise asyncio.CancelledError
        job_id = f"job-{mode.value}"
        queued_ids.append(job_id)
        return [type("Job", (), {"id": job_id, "filename": "x"})()]

    async def fake_cancel(job_id):
        cancelled.append(job_id)
        return True

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    monkeypatch.setattr(convert_routes.job_manager, "cancel_job", fake_cancel)

    with pytest.raises(asyncio.CancelledError):
        await convert_routes.create_auto_jobs(await _confirmed(paths))

    assert queued_ids, "the cancelled group must not be the first one queued"
    assert sorted(cancelled) == sorted(queued_ids)


def test_a_target_whose_tool_is_not_ready_is_skipped(tmp_path):
    from services.tools import registry

    nsp = _blank(tmp_path / "switch" / "game.nsp")
    gc = _gamecube(tmp_path / "pile" / "gc.iso")
    ready = {t.id for t in registry.all()} - {"nsz"}

    plan = _by_path(plan_auto([nsp, gc], ready_tool_ids=ready))

    assert plan["game.nsp"].mode is None
    assert plan["game.nsp"].skip_reason == (
        f"Switch isn't ready: {registry.get('nsz').not_ready_reason}"
    )
    assert plan["gc.iso"].mode == "dolphin_rvz"


@pytest.mark.asyncio
async def test_auto_route_skips_targets_whose_tool_is_not_ready(route_env, monkeypatch):
    async def ready_tool_ids():
        return [t.id for t in convert_routes.registry.all() if t.id != "dolphin"]

    monkeypatch.setattr(convert_routes.registry, "ready_tool_ids", ready_tool_ids)
    paths = _mixed_selection(route_env["tmp_path"])
    preview = {Path(p.file_path).name: p for p in await convert_routes.auto_plan(
        AutoPlanRequest(file_paths=paths),
    )}
    assert preview["gc.iso"].skip_reason.startswith("Dolphin isn't ready: ")

    await convert_routes.create_auto_jobs(await _confirmed(paths))

    assert all(q["mode"] != "dolphin_rvz" for q in route_env["queued"]), route_env["queued"]


@pytest.mark.asyncio
async def test_auto_rejects_paths_outside_the_volumes(route_env, tmp_path_factory):
    outside = _ps2(tmp_path_factory.mktemp("elsewhere") / "ps2.iso")

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(AutoJobCreateRequest(
            items=[AutoJobItem(file_path=outside, mode="createdvd")],
        ))

    assert exc.value.status_code == 403


def test_a_ps3_disc_folder_goes_to_folder_to_iso(tmp_path):
    from .ps3_helpers import make_ps3_folder

    # Anywhere, not only under a `ps3` folder: the folder's own layout is the
    # header (the tool that accepts the directory names its Platform).
    folder = make_ps3_folder(tmp_path / "downloads" / "Some Game")

    [entry] = plan_auto([str(folder)])

    assert (entry.detection.platform, entry.detection.how) == ("ps3", "header")
    assert entry.mode == "folder_to_iso"


def test_a_ps3_folder_named_like_an_iso_is_not_already_an_iso(tmp_path):
    # The "already the target format" check reads a file's name; a folder's
    # name says nothing about what it holds (CodeRabbit 4576895545).
    from .ps3_helpers import make_ps3_folder

    folder = make_ps3_folder(tmp_path / "downloads" / "Game.iso")

    [entry] = plan_auto([str(folder)])

    assert entry.mode == "folder_to_iso"
    assert entry.skip_reason is None


def test_an_iso_file_under_folder_to_iso_is_still_already_an_iso(tmp_path, monkeypatch):
    from services.tools import registry

    iso = _write(tmp_path / "Game.iso", b"\0" * 2048)
    monkeypatch.setattr(platform_detect, "default_mode", lambda *_a: "folder_to_iso")

    assert platform_detect.auto_route(iso, "ps3", registry) == (None, "Already ISO")


def test_a_custom_probe_set_does_not_reuse_the_shared_cache(tmp_path):
    # Same file, two probe sets: the second must not get the first's answer
    # (CodeRabbit 4576895544).
    from services.tools import registry

    iso = _gamecube(tmp_path / "neutral" / "game.iso")
    chdman_only = [registry.get("chdman")]
    dolphin_only = [registry.get("dolphin")]

    assert platform_detect.detect_platform(iso, tools=chdman_only).platform is None
    assert platform_detect.detect_platform(iso, tools=dolphin_only).platform == "ngc"
    assert platform_detect.detect_platform(iso).platform == "ngc"


def test_a_plain_folder_is_not_a_ps3_image(tmp_path):
    folder = tmp_path / "ps3" / "not a game"
    folder.mkdir(parents=True)

    [entry] = plan_auto([str(folder)])

    assert entry.mode is None


def test_a_folder_is_re_probed_when_its_layout_changes(tmp_path):
    folder = tmp_path / "downloads" / "Some Game"
    (folder / "PS3_GAME").mkdir(parents=True)

    assert platform_detect.detect_platform(str(folder)).platform is None

    # The folder's own entries don't change, only a nested file appears.
    from .ps3_helpers import make_sfo

    (folder / "PS3_GAME" / "PARAM.SFO").write_bytes(make_sfo([("TITLE_ID", "BLES01807")]))

    assert platform_detect.detect_platform(str(folder)).platform == "ps3"
