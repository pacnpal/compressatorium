"""Auto convert: each selected Image goes to its Platform's default target.

The default target is the first create-direction mode the registry lists for
the Platform that takes the file. Conflicted and Unsorted Images are skipped
with a reason. Tools are stubbed at the queue; nothing runs a binary.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.models import AutoJobCreateRequest, AutoPlanRequest
from app.routes import convert as convert_routes
from services import platform_detect
from services.platform_detect import plan_auto

from .test_disc_id import _make_iso
from .test_platform_detect import _blank, _gamecube, _ps1, _ps2, _psp, _wii, _write
from .test_xiso_service import build_image


@pytest.fixture(autouse=True)
def _fresh_cache():
    platform_detect.clear_cache()
    yield
    platform_detect.clear_cache()


def _by_path(plan):
    return {Path(entry.file_path).name: entry for entry in plan}


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


def test_ps2_cd_cue_bin_never_gets_the_dvd_command(tmp_path):
    cue, track = _raw_ps2_cd(tmp_path / "downloads")

    plan = _by_path(plan_auto([cue, track]))

    assert plan["game.cue"].detection.platform == "ps2"
    # createdvd reads a flat 2048-byte image; a track sheet or raw track is
    # not one, so there is no PS2 target for these files.
    assert plan["game.cue"].mode is None
    assert plan["game.bin"].mode is None
    assert "ps2" in plan["game.cue"].skip_reason


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
    assert entry.skip_reason


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

@pytest.fixture(name="route_env")
def _route_env(tmp_path: Path, monkeypatch):
    """Routes confined to ``tmp_path``; the queue and preferences stubbed."""
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 0)

    queued: list[dict] = []

    async def fake_atomic(job_specs, mode, **kwargs):
        queued.append({
            "mode": mode.value,
            "files": [Path(s["file_path"]).name for s in job_specs],
            "compression": kwargs.get("compression"),
        })
        return []

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)

    async def fake_prefs(key):
        assert key == "conversion"
        return {"chdman": "zlib,lzma", "dolphin": "zstd:19"}

    monkeypatch.setattr(convert_routes.preferences_store, "get", fake_prefs)
    return {"tmp_path": tmp_path, "queued": queued}


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

    result = await convert_routes.create_auto_jobs(AutoJobCreateRequest(file_paths=paths))

    queued = sorted(route_env["queued"], key=lambda q: q["mode"])
    assert queued == [
        {"mode": "createcd", "files": ["ps1.iso"], "compression": "zlib,lzma"},
        {"mode": "createdvd", "files": ["ps2.iso"], "compression": "zlib,lzma"},
        {"mode": "dolphin_rvz", "files": ["gc.iso"], "compression": "zstd:19"},
    ]
    skipped = {Path(p.file_path).name: p.skip_reason for p in result.plan if p.skip_reason}
    assert set(skipped) == {"clash.iso", "mystery.iso"}


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


@pytest.mark.asyncio
async def test_auto_rejects_paths_outside_the_volumes(route_env, tmp_path_factory):
    outside = _ps2(tmp_path_factory.mktemp("elsewhere") / "ps2.iso")

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(AutoJobCreateRequest(file_paths=[outside]))

    assert exc.value.status_code == 403
