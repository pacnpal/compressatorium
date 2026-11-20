"""`--to <format>` resolves a mode through the registry for every tool (#306).

``format_route`` is Auto's ranking (``default_mode``) restricted to the modes
that write the requested format, so the CLI and Auto pick a target the same
way: the Image's Platform narrows the candidates, the most specific extension
wins, then the smallest read set, then registration order.
"""
from __future__ import annotations

import pytest
from services.platform_detect import format_candidates, format_route
from services.tools import registry


def _touch(tmp_path, name, data=b"x"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


@pytest.mark.parametrize(("name", "target", "platform", "mode"), [
    ("Game.iso", "chd", "ps2", "createdvd"),
    ("Game.cue", "chd", "ps", "createcd"),
    ("Game.cue", ".chd", "ps2", "createcd"),
    ("Game.iso", "rvz", "ngc", "dolphin_rvz"),
    ("Game.nkit.iso", "rvz", "ngc", "nkit_to_rvz"),
    ("Game.nkit.iso", "iso", "wii", "nkit_restore"),
    ("Game.cso", "chd", "ps2", "cso_to_chd"),
    ("Game.rvz", "iso", "wii", "dolphin_iso"),
    ("Game.chd", "cue", "ps", "extractcd"),
    ("Game.iso", "cso", "psp", "cso_compress"),
    ("Game.nsp", "nsz", "switch", "nsz_compress"),
    ("Game.3ds", "z3ds", "3ds", "z3ds_compress"),
    ("Game.wud", "wux", "wiiu", "jwud_compress"),
    ("Game.gba", "7z", "gba", "romz_7z"),
])
def test_a_format_resolves_to_the_mode_auto_would_pick(
    tmp_path, name, target, platform, mode,
):
    path = _touch(tmp_path, name)
    assert format_route(path, target, platform, registry) == (mode, None)


def test_an_unsorted_image_is_not_narrowed_by_platform(tmp_path):
    path = _touch(tmp_path, "Game.cue")
    assert format_route(path, "chd", None, registry) == ("createcd", None)


def test_a_platform_no_writer_serves_is_skipped_with_a_reason(tmp_path):
    path = _touch(tmp_path, "Game.iso")
    mode, reason = format_route(path, "chd", "ngc", registry)
    assert mode is None
    assert reason == "No converter here makes CHD for GameCube games"


@pytest.mark.parametrize(("name", "target", "reason"), [
    ("Game.chd", "chd", "Already CHD"),
    ("Game.rvz", ".rvz", "Already RVZ"),
])
def test_an_image_already_in_the_format_is_left_alone(tmp_path, name, target, reason):
    path = _touch(tmp_path, name)
    assert format_route(path, target, None, registry) == (None, reason)


def test_a_mode_id_is_taken_as_is_when_it_reads_the_image(tmp_path):
    path = _touch(tmp_path, "Game.iso")
    assert format_route(path, "createcd", "ps2", registry) == ("createcd", None)


def test_a_mode_id_is_refused_for_a_platform_it_does_not_serve(tmp_path):
    path = _touch(tmp_path, "Game.iso")
    mode, reason = format_route(path, "createcd", "ngc", registry)
    assert mode is None
    assert reason == "createcd does not convert GameCube games"


def test_candidates_say_whether_an_image_is_in_scope(tmp_path):
    # A .bin is read by no default CHD writer (its .cue is the Image), and a
    # .chd is in scope for "chd" only so it can be reported as already done.
    assert format_candidates(_touch(tmp_path, "Game.bin"), "chd", registry) == []
    assert format_candidates(_touch(tmp_path, "Game.txt"), "chd", registry) == []
    assert format_candidates(_touch(tmp_path, "Game.chd"), "chd", registry)
    assert format_candidates(_touch(tmp_path, "Game.iso"), "createcd", registry)
    assert format_candidates(_touch(tmp_path, "Game.rvz"), "createcd", registry) == []


def test_an_archive_member_needs_a_mode_that_reads_from_archives(tmp_path):
    archive = _touch(tmp_path, "pack.zip")
    assert format_route(f"{archive}::Game.iso", "rvz", "ngc", registry) == (
        "dolphin_rvz", None,
    )
    # romz modes take no archive members.
    assert format_candidates(f"{archive}::Game.gba", "7z", registry) == []


def test_every_target_format_some_mode_writes_resolves_for_its_own_input(tmp_path):
    """Every create/compress/extract mode is reachable through a format: for
    one of its own inputs, the format it writes picks some mode (maybe a more
    specific sibling, never nothing)."""
    for spec in registry.mode_specs():
        if not spec.auto_target or not spec.input_extensions:
            continue
        ext = min(registry.mode_reads_extensions(spec.mode))
        path = _touch(tmp_path / spec.mode, f"Game{ext}")
        if not registry.for_mode(spec.mode).converts_path(path):
            continue  # content-gated (xiso): needs a real image
        out = registry.for_mode(spec.mode).output_path(spec.mode, path)
        fmt = out[len(str(tmp_path / spec.mode / "Game")):]
        if fmt in ("", ext):
            # romz_extract names its output from inside the archive; copy-style
            # modes are already in the format.
            continue
        mode, reason = format_route(path, fmt, None, registry)
        assert mode is not None, (spec.mode, fmt, reason)


def test_generic_archive_formats_are_targets():
    from services.platform_detect import target_formats

    formats = target_formats(registry)
    assert ".7z" in formats and ".zip" in formats
    assert "_copy.chd" not in formats


@pytest.mark.parametrize(("name", "target", "platform", "mode"), [
    ("Game.gba", "7z", "gba", "romz_7z"),
    ("Game.sfc", "zip", "snes", "romz_zip"),
    ("Game.nes", "7z", None, "romz_7z"),
])
def test_generic_archive_formats_resolve_per_image(
    tmp_path, name, target, platform, mode,
):
    path = _touch(tmp_path, name)
    assert format_route(path, target, platform, registry) == (mode, None)


def test_a_mode_id_needs_a_command_that_reads_the_image(tmp_path):
    # createdvd accepts a .cue by hand, but its command reads only a flat .iso.
    path = _touch(tmp_path, "Game.cue")
    assert format_candidates(path, "createdvd", registry) == []
    mode, reason = format_route(path, "createdvd", None, registry)
    assert mode is None and reason == "createdvd does not read .cue files"


def test_auto_sends_a_ps3_disc_folder_to_folder_to_iso(tmp_path):
    """The shared reader ranking reads folders too, so Auto (and --to iso)
    now route a PS3 disc folder to makeps3iso instead of leaving it alone."""
    from services.platform_detect import default_mode

    from tests.ps3_helpers import make_ps3_folder

    folder = make_ps3_folder(tmp_path / "ps3" / "Game")
    assert default_mode(str(folder), "ps3", registry) == "folder_to_iso"
    assert format_route(str(folder), "iso", "ps3", registry) == ("folder_to_iso", None)
