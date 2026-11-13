"""Tests for the extract-xiso tool (Xbox / Xbox 360 disc image -> trimmed XISO).

The fixtures here are real XDVDFS images, built in Python: a volume descriptor
at 0x10000 into the game partition and directory tables in the binary-tree
layout extract-xiso reads. Each is placed at the partition offset of the layout
under test (sparse files, so an XGD1 image at 0x18300000 costs a few KB on
disk). The subprocess is mocked, as in the other per-tool suites; what is
pinned is everything the service owns — content detection, the directory walk,
the source-protecting workspace, progress, cleanup, naming, verify and info.
"""
from __future__ import annotations

import asyncio
import os
import stat
import struct
from pathlib import Path

import pytest

from app.services.tools import registry
from app.services.tools.spec import ModeKind

# The registry's plugin holds the service instance the app actually uses;
# reach it through the registry so monkeypatching hits that object.
TOOL = registry.get("xiso")
SERVICE = TOOL._service
MODULE = __import__(type(SERVICE).__module__, fromlist=["walk_image"])

MAGIC = b"MICROSOFT*XBOX*MEDIA"
SECTOR = 2048
OFFSETS = {"XISO": 0, "XGD2": 0x0FD90000, "XGD3": 0x02080000, "XGD1": 0x18300000}


def _dir_table(entries: list[tuple[str, int, int, int]]) -> bytes:
    """Entries ``(name, sector, size, attr)`` chained through right pointers."""
    blobs: list[bytes] = []
    offsets: list[int] = []
    pos = 0
    for name, *_rest in entries:
        offsets.append(pos)
        length = 14 + len(name)
        pos += (length + 3) & ~3
    table = bytearray()
    for i, (name, sector, size, attr) in enumerate(entries):
        right = offsets[i + 1] // 4 if i + 1 < len(entries) else 0
        raw = struct.pack("<HHIIBB", 0, right, sector, size, attr, len(name)) + name.encode()
        raw += b"\xff" * (((len(raw) + 3) & ~3) - len(raw))
        blobs.append(raw)
        table += raw
    table += b"\xff" * (-len(table) % SECTOR)
    return bytes(table)


def build_image(
    path: Path,
    *,
    layout: str = "XGD2",
    optimized: bool = False,
    exe: str = "default.xex",
    truncate_by: int = 0,
) -> dict:
    """Write an Xbox image: ``/<exe>``, ``/maps/a.map``, ``/$SystemUpdate/su``."""
    base = OFFSETS[layout]
    root_sector, maps_sector, su_sector = 33, 34, 35
    files = {
        exe: (40, 3000),
        "a.map": (42, 5000),
        "su.bin": (46, 100),
    }
    root = _dir_table([
        (exe, files[exe][0], files[exe][1], 0x20),
        ("maps", maps_sector, SECTOR, 0x10),
        ("$SystemUpdate", su_sector, SECTOR, 0x10),
    ])
    maps = _dir_table([("a.map", *files["a.map"], 0x20)])
    su = _dir_table([("su.bin", *files["su.bin"], 0x20)])
    end = base + (46 * SECTOR) + 100
    with open(path, "wb") as fh:
        fh.seek(base + 0x10000)
        fh.write(MAGIC + struct.pack("<II", root_sector, len(root)) + b"\0" * 0x7D0 + MAGIC)
        for sector, blob in ((root_sector, root), (maps_sector, maps), (su_sector, su)):
            fh.seek(base + sector * SECTOR)
            fh.write(blob)
        for sector, size in files.values():
            fh.seek(base + sector * SECTOR)
            fh.write(b"\xab" * size)
        if optimized:
            fh.seek(31337)
            fh.write(b"in!xiso!2.7.1")
        fh.truncate(end - truncate_by)
    return {"data_bytes": sum(s for _, s in files.values()), "file_count": 3}


# --------------------------------------------------------------------------- #
# Detection and the directory walk
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("layout", "console"),
    [("XGD1", "Xbox"), ("XGD2", "Xbox 360"), ("XGD3", "Xbox 360"), ("XISO", None)],
)
def test_detect_layout_finds_every_generation(tmp_path, layout, console):
    img = tmp_path / "g.iso"
    build_image(img, layout=layout)
    offset, found, found_console = MODULE.detect_layout(str(img))
    assert (offset, found, found_console) == (OFFSETS[layout], layout, console)


def test_detect_layout_rejects_a_non_xbox_iso(tmp_path):
    img = tmp_path / "psp.iso"
    img.write_bytes(b"\0" * 0x8000 + b"\x01CD001" + b"\0" * 0x20000)
    with pytest.raises(MODULE.XisoImageError):
        MODULE.detect_layout(str(img))


def test_walk_totals_and_markers(tmp_path):
    img = tmp_path / "g.iso"
    expected = build_image(img, layout="XGD2")
    stats = MODULE.walk_image(str(img))
    assert stats["file_count"] == expected["file_count"]
    assert stats["data_bytes"] == expected["data_bytes"]
    assert stats["root_executable"] == "default.xex"
    assert stats["has_system_update"] is True
    assert stats["dir_count"] == 2


@pytest.mark.parametrize(("exe", "console"), [("default.xbe", "Xbox"), ("default.xex", "Xbox 360")])
def test_trimmed_image_console_comes_from_the_boot_executable(tmp_path, exe, console):
    img = tmp_path / "g.iso"
    build_image(img, layout="XISO", exe=exe)
    assert MODULE.walk_image(str(img))["console"] == console


def test_walk_rejects_a_directory_table_past_eof(tmp_path):
    img = tmp_path / "crafted.iso"
    img.write_bytes(
        b"\0" * 0x10000 + MAGIC + struct.pack("<II", 0x7FFFFFFF, 4096) + b"\0" * 2048,
    )
    with pytest.raises(MODULE.XisoImageError, match="outside the image"):
        MODULE.walk_image(str(img))


def test_walk_rejects_a_directory_cycle(tmp_path):
    img = tmp_path / "loop.iso"
    # Root (sector 33) contains a directory that points back at sector 33.
    root = _dir_table([("loop", 33, SECTOR, 0x10)])
    with open(img, "wb") as fh:
        fh.seek(0x10000)
        fh.write(MAGIC + struct.pack("<II", 33, len(root)))
        fh.seek(33 * SECTOR)
        fh.write(root)
    with pytest.raises(MODULE.XisoImageError, match="cycle"):
        MODULE.walk_image(str(img))


# --------------------------------------------------------------------------- #
# Convertibility (what the listing and the plan-time gate ask)
# --------------------------------------------------------------------------- #


def test_untrimmed_xbox_image_is_convertible(tmp_path):
    img = tmp_path / "Halo 3.iso"
    build_image(img, layout="XGD2")
    assert SERVICE.is_convertible(str(img)) is True
    assert TOOL.converts_path(str(img)) is True


def test_already_optimized_xiso_is_not_offered(tmp_path):
    img = tmp_path / "g.iso"
    build_image(img, layout="XISO", optimized=True)
    assert SERVICE.is_convertible(str(img)) is False


def test_non_xbox_iso_and_products_are_not_offered(tmp_path):
    psp = tmp_path / "psp.iso"
    psp.write_bytes(b"\0" * 0x30000)
    product = tmp_path / "g.xiso.iso"
    build_image(product, layout="XGD2")  # even with Xbox content, by name
    assert SERVICE.is_convertible(str(psp)) is False
    assert SERVICE.is_convertible(str(product)) is False
    assert SERVICE.is_convertible(str(tmp_path / "missing.iso")) is False
    assert SERVICE.is_convertible(str(tmp_path / "g.wud")) is False


def test_convertibility_cache_follows_file_changes(tmp_path):
    img = tmp_path / "g.iso"
    img.write_bytes(b"\0" * 0x30000)
    assert SERVICE.is_convertible(str(img)) is False
    build_image(img, layout="XISO")
    os.utime(img, ns=(1, 2_000_000_000))  # force a different mtime
    assert SERVICE.is_convertible(str(img)) is True


# --------------------------------------------------------------------------- #
# Mode spec, naming, argv
# --------------------------------------------------------------------------- #


def test_mode_spec():
    spec = registry.spec("xiso_rewrite")
    assert spec.tool_id == "xiso"
    assert spec.kind is ModeKind.COMPRESS
    assert spec.output_ext == ".xiso.iso"
    assert spec.input_extensions == frozenset({".iso"})
    # Verifiable, never deletable: the trim drops partitions nothing rebuilds.
    assert spec.supports_verify is True
    assert spec.supports_delete_on_verify is False
    assert spec.allows_archive_input is False
    assert registry.mode_supports_verify("xiso_rewrite") is True


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("/data/Halo 3 (USA).iso", "/data/Halo 3 (USA).xiso.iso"),
        ("/data/GAME.ISO", "/data/GAME.xiso.iso"),
        ("/data/g.xiso.iso", "/data/g.xiso.iso"),
    ],
)
def test_output_path(inp, expected):
    assert SERVICE.get_output_path(inp) == expected


def test_output_path_honours_output_dir():
    assert SERVICE.get_output_path("/data/g.iso", "/out") == "/out/g.xiso.iso"


def test_build_command_puts_options_before_the_image(monkeypatch):
    monkeypatch.setattr(SERVICE, "extract_xiso_path", "/usr/local/bin/extract-xiso")
    cmd = SERVICE._build_command("/scratch/source.iso", "/out/.work")
    tail = cmd[-5:]
    # extract-xiso stops parsing options at the first operand, so a trailing
    # -d would be ignored and the rewrite would land in the cwd.
    assert tail == ["/usr/local/bin/extract-xiso", "-r", "-d", "/out/.work", "/scratch/source.iso"]
    assert "-D" not in cmd


# --------------------------------------------------------------------------- #
# Convert (mocked subprocess)
# --------------------------------------------------------------------------- #


def _workdirs(path: Path) -> list[str]:
    return [n for n in os.listdir(path) if n.startswith(".xiso-")]


@pytest.fixture(name="scratch")
def _scratch(tmp_path, monkeypatch):
    temp = tmp_path / "apptemp"
    monkeypatch.setattr(MODULE.settings, "temp_dir", str(temp))
    return temp


@pytest.mark.asyncio
async def test_convert_never_hands_the_source_to_the_binary(tmp_path, scratch, monkeypatch):
    src = tmp_path / "Halo 3.iso"
    build_image(src, layout="XGD2")
    before = (src.stat().st_size, src.stat().st_mtime_ns)
    out = tmp_path / "Halo 3.xiso.iso"
    seen: dict = {}

    async def fake_run(cmd, *, output_path, parse_progress, complete_message, **_kw):
        link = cmd[-1]
        seen["link_is_symlink"] = os.path.islink(link)
        seen["link_target"] = os.readlink(link)
        # Real extract-xiso renames its input to .old before reading it.
        os.rename(link, link + ".old")
        Path(output_path).write_bytes(b"XISO")
        for line in (
            "adding / (0 bytes) [OK]",
            "adding /default.xex (3000 bytes) [OK]",
            "adding /maps/a.map (5000 bytes) [OK]",
            "adding /$SystemUpdate/su.bin (100 bytes) [OK]",
        ):
            pct = parse_progress(line)
            if pct is not None:
                yield {"progress": pct, "message": line}
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(SERVICE._runner, "run", fake_run)

    updates = [u async for u in SERVICE.convert(str(src), str(out), "xiso_rewrite")]

    assert seen["link_is_symlink"] is True
    assert seen["link_target"] == str(src.resolve())
    # The source was only ever read: same bytes, same name, no .old beside it.
    assert (src.stat().st_size, src.stat().st_mtime_ns) == before
    assert not (tmp_path / "Halo 3.iso.old").exists()
    assert out.read_bytes() == b"XISO"
    assert not _workdirs(tmp_path)
    assert not os.listdir(scratch)
    progress = [u["progress"] for u in updates]
    assert progress == sorted(progress)
    assert progress[-1] == 100
    # Real progress from the per-file lines: 3000 of 8100 bytes ~= 37%.
    assert 30 <= progress[3] <= 45


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [MODULE.ConversionCancelled("cancelled"), asyncio.CancelledError(), RuntimeError("x")],
)
async def test_convert_failure_sweeps_everything_but_the_source(
    tmp_path, scratch, monkeypatch, boom,
):
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD3")
    out = tmp_path / "g.xiso.iso"

    async def fake_run(cmd, *, output_path, **_kw):
        os.rename(cmd[-1], cmd[-1] + ".old")
        Path(output_path).write_bytes(b"partial")
        yield {"progress": 10, "message": "adding /x (1 bytes) [OK]"}
        raise boom

    monkeypatch.setattr(SERVICE._runner, "run", fake_run)

    with pytest.raises(type(boom)):
        async for _ in SERVICE.convert(str(src), str(out), "xiso_rewrite"):
            pass

    assert src.exists() and not out.exists()
    assert not _workdirs(tmp_path)
    assert not os.listdir(scratch)


@pytest.mark.asyncio
async def test_convert_failure_leaves_an_existing_output_alone(tmp_path, scratch, monkeypatch):
    """The binary writes only inside its work dir, so a failed run must not
    sweep a file already at output_path: it was never this job's write."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "g.xiso.iso"
    out.write_bytes(b"earlier good XISO")

    async def fake_run(cmd, **_kw):
        yield {"progress": 10, "message": "adding /x (1 bytes) [OK]"}
        raise RuntimeError("extract-xiso failed with return code 1")

    monkeypatch.setattr(SERVICE._runner, "run", fake_run)

    with pytest.raises(RuntimeError):
        async for _ in SERVICE.convert(str(src), str(out), "xiso_rewrite"):
            pass

    assert out.read_bytes() == b"earlier good XISO"
    assert not _workdirs(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["symlink", "makedirs"])
async def test_workspace_setup_failure_leaves_no_scratch_dirs(
    tmp_path, scratch, monkeypatch, failing,
):
    """A setup step failing after the first dir exists still sweeps it."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "out" / "g.xiso.iso"
    real = getattr(MODULE.os, failing)

    def boom(path, *args, **kwargs):
        # Fail only the output-side makedirs, not the temp-dir one.
        if failing == "makedirs" and str(scratch) in str(path):
            return real(path, *args, **kwargs)
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(MODULE.os, failing, boom)

    with pytest.raises(PermissionError):
        async for _ in SERVICE.convert(str(src), str(out), "xiso_rewrite"):
            pass

    assert not os.path.exists(scratch) or not os.listdir(scratch)
    assert not _workdirs(tmp_path)
    assert src.exists()


def test_workspace_dirs_are_private(tmp_path, scratch):
    """Both scratch dirs are 0o700, as ``mkdtemp`` would have made them, even
    though their names are chosen up front."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    link_dir, out_dir = SERVICE._workspace_paths(str(tmp_path / "g.xiso.iso"))
    old_umask = os.umask(0o022)
    try:
        SERVICE._prepare_workspace(
            str(src), link_dir, out_dir, MODULE.threading.Event(),
        )
        for directory in (link_dir, out_dir):
            assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    finally:
        os.umask(old_umask)
        SERVICE._remove_workspace(link_dir, out_dir)


def test_abandoned_workspace_setup_undoes_its_own_work(tmp_path, scratch):
    """A setup a cancel gave up on removes what it creates afterwards."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    link_dir, out_dir = SERVICE._workspace_paths(str(tmp_path / "g.xiso.iso"))
    abandoned = MODULE.threading.Event()
    abandoned.set()

    SERVICE._prepare_workspace(str(src), link_dir, out_dir, abandoned)

    assert not os.path.exists(link_dir)
    assert not os.path.exists(out_dir)


@pytest.mark.asyncio
async def test_cancel_during_workspace_setup_leaves_no_scratch_dirs(
    tmp_path, scratch, monkeypatch,
):
    """Cancelled while setup is still running: convert sweeps, and the setup
    finishing afterwards cleans up after itself."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "g.xiso.iso"
    entered = MODULE.threading.Event()
    release = MODULE.threading.Event()
    real_prepare = type(SERVICE)._prepare_workspace.__func__
    finished = MODULE.threading.Event()

    def slow_prepare(cls, *args):
        entered.set()
        release.wait(5)
        try:
            return real_prepare(cls, *args)
        finally:
            finished.set()

    monkeypatch.setattr(type(SERVICE), "_prepare_workspace", classmethod(slow_prepare))
    cancel = asyncio.Event()

    async def run():
        async for _ in SERVICE.convert(
            str(src), str(out), "xiso_rewrite", cancel_event=cancel,
        ):
            pass

    task = asyncio.create_task(run())
    try:
        # Cancel only once setup is really running, not after a guessed delay.
        assert await asyncio.to_thread(entered.wait, 5)
        cancel.set()
        with pytest.raises(MODULE.ConversionCancelled):
            await task
    finally:
        release.set()  # never leave the worker blocked, even on a failure
    assert await asyncio.to_thread(finished.wait, 5)

    assert not _workdirs(tmp_path)
    assert not os.path.exists(scratch) or not os.listdir(scratch)


@pytest.mark.asyncio
async def test_convert_explains_an_already_optimized_skip(tmp_path, scratch, monkeypatch):
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")

    async def fake_run(cmd, **_kw):
        if False:  # pragma: no cover - make this an async generator
            yield {}
        raise RuntimeError(
            "extract-xiso produced no output file.\nLast output:\n"
            "source.iso is already optimized, skipping...",
        )

    monkeypatch.setattr(SERVICE._runner, "run", fake_run)

    with pytest.raises(RuntimeError, match="already an optimized XISO"):
        async for _ in SERVICE.convert(str(src), str(tmp_path / "g.xiso.iso"), "xiso_rewrite"):
            pass


@pytest.mark.asyncio
async def test_convert_rejects_a_non_xbox_source(tmp_path, scratch):
    src = tmp_path / "psp.iso"
    src.write_bytes(b"\0" * 0x30000)
    with pytest.raises(ValueError, match="Not an untrimmed Xbox"):
        async for _ in SERVICE.convert(str(src), str(tmp_path / "psp.xiso.iso"), "xiso_rewrite"):
            pass
    assert not os.path.exists(scratch) or not os.listdir(scratch)


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #


async def _verify_events(path: str, monkeypatch, *, rc_valid: bool = True) -> list[dict]:
    async def fake_capture(cmd, *, path, success_message, **_kw):
        assert "-l" in cmd
        if rc_valid:
            yield {"type": "complete", "valid": True, "message": success_message}
        else:
            yield {"type": "error", "valid": False, "message": "Integrity check failed"}

    monkeypatch.setattr(SERVICE._runner, "capture_verify", fake_capture)
    return [e async for e in SERVICE.verify_stream(path)]


@pytest.mark.asyncio
async def test_verify_accepts_a_good_xiso(tmp_path, monkeypatch):
    img = tmp_path / "g.xiso.iso"
    build_image(img, layout="XISO", optimized=True)
    events = await _verify_events(str(img), monkeypatch)
    assert events[-1]["valid"] is True
    assert "3 files" in events[-1]["message"]


@pytest.mark.asyncio
async def test_verify_uses_wrapper_priority_like_the_other_tools(tmp_path, monkeypatch):
    """nice/ionice as command wrappers with nice_via_wrapper=True (maxcso,
    chdman, dolphin), so capture_verify neither forks a preexec renice nor
    prepends a second ionice."""
    img = tmp_path / "g.xiso.iso"
    build_image(img, layout="XISO", optimized=True)
    monkeypatch.setattr(MODULE, "nice_prefix", lambda _o: ["nice", "-n", "10"])
    monkeypatch.setattr(MODULE, "ioprio_prefix", lambda _o: ["ionice", "-c", "3"])
    seen: dict = {}

    async def fake_capture(cmd, *, path, success_message, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        yield {"type": "complete", "valid": True, "message": success_message}

    monkeypatch.setattr(SERVICE._runner, "capture_verify", fake_capture)
    [e async for e in SERVICE.verify_stream(str(img))]
    assert seen["kw"].get("nice_via_wrapper") is True
    assert seen["cmd"][:5] == ["nice", "-n", "10", "ionice", "-c"]
    assert seen["cmd"].count("ionice") == 1


@pytest.mark.asyncio
async def test_verify_catches_a_truncated_copy(tmp_path, monkeypatch):
    img = tmp_path / "g.xiso.iso"
    build_image(img, layout="XISO", optimized=True, truncate_by=80)
    events = await _verify_events(str(img), monkeypatch)
    assert events[-1]["valid"] is False
    assert "truncated" in events[-1]["message"]


@pytest.mark.asyncio
async def test_verify_rejects_a_full_disc_layout(tmp_path, monkeypatch):
    img = tmp_path / "g.xiso.iso"
    build_image(img, layout="XGD2")
    events = await _verify_events(str(img), monkeypatch)
    assert events[-1]["valid"] is False
    assert "XGD2" in events[-1]["message"]


@pytest.mark.asyncio
async def test_verify_only_claims_its_own_products(tmp_path, monkeypatch):
    img = tmp_path / "g.iso"
    build_image(img, layout="XISO", optimized=True)
    events = await _verify_events(str(img), monkeypatch)
    assert events[-1]["valid"] is False
    assert ".xiso.iso" in events[-1]["message"]


# --------------------------------------------------------------------------- #
# Info and plugin wiring
# --------------------------------------------------------------------------- #


def test_info_describes_a_full_disc_image(tmp_path):
    img = tmp_path / "g.iso"
    build_image(img, layout="XGD2")
    info = SERVICE.info(str(img))
    assert info["console"] == "Xbox 360"
    assert info["layout"] == "XGD2"
    assert info["format"] == "Xbox 360 disc image (XGD2)"
    assert info["file_count"] == 3
    assert info["data_size"] == 8100
    assert info["trimmed"] is False and info["compressed"] is False
    assert info["has_system_update"] is True
    model = TOOL.info_model(info, str(img))
    assert model.layout == "XGD2" and model.extension == ".iso"


def test_info_describes_a_product(tmp_path):
    img = tmp_path / "g.xiso.iso"
    build_image(img, layout="XISO", optimized=True, exe="default.xbe")
    info = SERVICE.info(str(img))
    assert info["format"] == "XISO (Xbox)"
    assert info["extension"] == ".xiso.iso"
    assert info["trimmed"] is True and info["optimized"] is True


def test_detect_output_badges_an_existing_product(tmp_path):
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    assert TOOL.detect_output(str(src)) is None
    (tmp_path / "g.xiso.iso").write_bytes(b"x")
    status = TOOL.detect_output(str(src))
    assert status is not None and status.exists and status.tool_id == "xiso"


def test_verify_claim_outranks_dolphins_generic_iso():
    assert registry.tool_for_verify("/data/g.xiso.iso").id == "xiso"
    assert registry.tool_for_verify("/data/g.iso").id == "dolphin"


def test_rename_on_duplicate_keeps_the_compound_extension(tmp_path):
    """``rename`` must give ``Game_1.xiso.iso``, not ``Game.xiso_1.iso`` (which
    no longer matches the verify claim, so the product could never be verified)."""
    from app.services.output_conflicts import get_unique_output_path

    taken = tmp_path / "Game.xiso.iso"
    taken.write_bytes(b"x")
    assert get_unique_output_path(str(taken), "xiso_rewrite") == str(
        tmp_path / "Game_1.xiso.iso",
    )
    # Single-suffix products are numbered exactly as before.
    iso = tmp_path / "Game.cso"
    iso.write_bytes(b"x")
    assert get_unique_output_path(str(iso), "cso_compress") == str(tmp_path / "Game_1.cso")


@pytest.mark.asyncio
async def test_cancel_during_the_initial_probe_is_a_cancel(tmp_path, scratch, monkeypatch):
    """A cancel while the convertibility probe waits on storage must surface as
    ``ConversionCancelled`` (a cancelled job), not a failed ``ReadCancelled``."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    entered = MODULE.threading.Event()
    release = MODULE.threading.Event()

    def stuck_probe(_path):
        entered.set()
        release.wait(5)
        return True

    monkeypatch.setattr(SERVICE, "is_convertible", stuck_probe)
    cancel = asyncio.Event()

    async def run():
        async for _ in SERVICE.convert(
            str(src), str(tmp_path / "g.xiso.iso"), "xiso_rewrite", cancel_event=cancel,
        ):
            pass

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        cancel.set()
        with pytest.raises(MODULE.ConversionCancelled):
            await task
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_hung_filesystem_walk_is_bounded(tmp_path, scratch, monkeypatch):
    """The walk runs before the subprocess's stall watchdog exists, so the
    conversion's no-progress timeout bounds it instead of waiting forever."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    runner_module = __import__(
        MODULE.run_detached_within_stall_bound.__module__, fromlist=["settings"],
    )
    monkeypatch.setattr(runner_module, "_MIN_STALL_TIMEOUT", 0)
    monkeypatch.setattr(runner_module.settings, "progress_timeout", 1)
    release = MODULE.threading.Event()

    def hung_walk(_path):
        release.wait(10)
        return {}

    monkeypatch.setattr(MODULE, "walk_image", hung_walk)
    try:
        with pytest.raises(RuntimeError, match="Reading the Xbox filesystem stopped responding"):
            async for _ in SERVICE.convert(
                str(src), str(tmp_path / "g.xiso.iso"), "xiso_rewrite",
            ):
                pass
    finally:
        release.set()
    assert not _workdirs(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", ["capacity", "os_timeout"])
async def test_an_instant_timeout_keeps_its_own_cause(monkeypatch, raised):
    """Only the bound's own expiry is reworded as "stopped responding"; a
    TimeoutError that fails at once (the probe ceiling, an OS ETIMEDOUT) keeps
    its type and message."""
    runner_module = __import__(
        MODULE.run_detached_within_stall_bound.__module__, fromlist=["settings"],
    )
    monkeypatch.setattr(runner_module.settings, "progress_timeout", 600)

    if raised == "capacity":
        def blocked(_func):
            raise runner_module.ProbeCapacityExceeded("64 probes already blocked")

        monkeypatch.setattr(runner_module, "_probe_in_daemon_thread", blocked)
        expected, message = runner_module.ProbeCapacityExceeded, "64 probes already blocked"

        def func():
            return None
    else:
        expected, message = TimeoutError, "ETIMEDOUT from the mount"

        def func():
            raise TimeoutError(message)

    with pytest.raises(expected, match=message):
        await runner_module.run_detached_within_stall_bound(func, what="Reading")


class _TrackedRead(bytes):
    """A read result that counts how many sizeable reads are alive at once."""

    alive = 0
    peak = 0

    def __new__(cls, data):
        obj = super().__new__(cls, data)
        obj._counted = len(data) > 8  # skip the 8-byte root descriptor
        if obj._counted:
            cls.alive += 1
            cls.peak = max(cls.peak, cls.alive)
        return obj

    def __del__(self):
        if self._counted:
            type(self).alive -= 1


def test_walk_holds_one_directory_table_at_a_time(tmp_path, monkeypatch):
    """A parent's table is released before its children are walked, so a deep
    tree of large tables can't pile up ancestor tables in memory."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")  # root -> maps/, $SystemUpdate/
    real_open = open

    class _Handle:
        def __init__(self, *args, **kwargs):
            self._fh = real_open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

        def seek(self, *args):
            return self._fh.seek(*args)

        def read(self, *args):
            return _TrackedRead(self._fh.read(*args))

    monkeypatch.setattr(MODULE, "open", _Handle, raising=False)
    monkeypatch.setattr(_TrackedRead, "alive", 0)
    monkeypatch.setattr(_TrackedRead, "peak", 0)

    stats = MODULE.walk_image(str(src))

    assert stats["dir_count"] == 2 and stats["file_count"] == 3
    assert _TrackedRead.peak == 1


@pytest.mark.asyncio
async def test_convert_uses_wrapper_priority(tmp_path, scratch, monkeypatch):
    """The rewrite is reniced by a ``nice`` wrapper, not a ``preexec_fn``: this
    process runs detached reader threads, where a preexec fork can deadlock."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    monkeypatch.setattr(MODULE, "nice_prefix", lambda _o: ["nice", "-n", "10"])
    monkeypatch.setattr(MODULE, "ioprio_prefix", lambda _o: ["ionice", "-c", "3"])
    seen: dict = {}

    async def fake_run(cmd, *, output_path, complete_message, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        Path(output_path).write_bytes(b"XISO")
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(SERVICE._runner, "run", fake_run)
    [u async for u in SERVICE.convert(str(src), str(tmp_path / "g.xiso.iso"), "xiso_rewrite")]

    assert seen["kw"].get("nice_via_wrapper") is True
    assert seen["cmd"][:5] == ["nice", "-n", "10", "ionice", "-c"]


@pytest.mark.asyncio
async def test_a_cancel_before_the_rename_leaves_the_destination_alone(
    tmp_path, scratch, monkeypatch,
):
    """A cancel that lands after extract-xiso exits but before the rename runs
    must not delete whatever is already at output_path: this job never wrote it."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "g.xiso.iso"
    out.write_bytes(b"PRECIOUS")
    cancel = asyncio.Event()

    async def fake_run(cmd, *, output_path, complete_message, **_kw):
        Path(output_path).write_bytes(b"XISO")
        yield {"progress": 100, "message": complete_message}
        cancel.set()  # the cancel arrives before the final rename

    monkeypatch.setattr(SERVICE._runner, "run", fake_run)

    with pytest.raises(MODULE.ConversionCancelled):
        async for _ in SERVICE.convert(
            str(src), str(out), "xiso_rewrite", cancel_event=cancel,
        ):
            pass

    assert out.read_bytes() == b"PRECIOUS"
    assert not _workdirs(tmp_path)



async def _wait_until_gone(path: Path) -> None:
    for _ in range(50):
        if not path.exists():
            return
        await asyncio.sleep(0.02)


def _late_link(started, release, finished):
    """An ``os.link`` stand-in that stalls, then lands late with the product's
    own inode: it pins the source under a side name first, so the job's sweep
    of its work dir can't make the late link fail."""
    real_link, real_unlink = os.link, os.unlink

    def link(src, dst):
        # Beside the work dir, not in it: the job's sweep removes the work dir.
        hold = os.path.join(os.path.dirname(os.path.dirname(src)), "late-link.hold")
        real_link(src, hold)
        try:
            started.set()
            release.wait(10)
            real_link(hold, dst)
        finally:
            try:
                real_unlink(hold)
            finally:
                finished.set()

    return link


async def _fake_run(cmd, *, output_path, complete_message, **_kw):
    Path(output_path).write_bytes(b"XISO")
    yield {"progress": 100, "message": complete_message}


@pytest.mark.asyncio
async def test_a_hung_final_placement_is_bounded(tmp_path, scratch, monkeypatch):
    """Placement runs after the subprocess's stall watchdog has stopped, so the
    no-progress bound covers it too; when it lands after the timeout, the worker
    removes its own product, so a failed job leaves nothing behind."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "g.xiso.iso"
    runner_module = __import__(
        MODULE.run_detached_within_stall_bound.__module__, fromlist=["settings"],
    )
    monkeypatch.setattr(runner_module, "_MIN_STALL_TIMEOUT", 0)
    monkeypatch.setattr(runner_module.settings, "progress_timeout", 1)
    started, release, finished = (MODULE.threading.Event() for _ in range(3))
    monkeypatch.setattr(SERVICE._runner, "run", _fake_run)
    monkeypatch.setattr(MODULE.os, "link", _late_link(started, release, finished))
    try:
        with pytest.raises(RuntimeError, match="Placing the XISO output stopped responding"):
            async for _ in SERVICE.convert(str(src), str(out), "xiso_rewrite"):
                pass
        assert not out.exists()  # nothing was ever created at the destination
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    await _wait_until_gone(out)
    assert not out.exists()


@pytest.mark.asyncio
async def test_a_late_placement_after_a_cancel_leaves_nothing_behind(
    tmp_path, scratch, monkeypatch,
):
    """Cancelled while placement is stuck: the cleanup doesn't touch the
    destination, and when placement lands later the worker removes its product."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "g.xiso.iso"
    cancel = asyncio.Event()
    started, release, finished = (MODULE.threading.Event() for _ in range(3))
    monkeypatch.setattr(SERVICE._runner, "run", _fake_run)
    monkeypatch.setattr(MODULE.os, "link", _late_link(started, release, finished))

    async def run():
        async for _ in SERVICE.convert(
            str(src), str(out), "xiso_rewrite", cancel_event=cancel,
        ):
            pass

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        cancel.set()
        with pytest.raises(MODULE.ConversionCancelled):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    await _wait_until_gone(out)
    assert not out.exists()


@pytest.mark.asyncio
async def test_a_late_placement_never_replaces_a_file_that_appeared_meanwhile(
    tmp_path, scratch, monkeypatch,
):
    """Placement is stuck, the job is cancelled, and another file appears at the
    destination. When placement finally runs it must not replace that file, and
    the abandoned worker must not remove it."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "g.xiso.iso"
    cancel = asyncio.Event()
    started, release, finished = (MODULE.threading.Event() for _ in range(3))
    monkeypatch.setattr(SERVICE._runner, "run", _fake_run)
    monkeypatch.setattr(MODULE.os, "link", _late_link(started, release, finished))

    async def run():
        async for _ in SERVICE.convert(
            str(src), str(out), "xiso_rewrite", cancel_event=cancel,
        ):
            pass

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        cancel.set()
        with pytest.raises(MODULE.ConversionCancelled):
            await task
        out.write_bytes(b"SOMEONE ELSE'S FILE")
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    await asyncio.sleep(0.1)
    assert out.read_bytes() == b"SOMEONE ELSE'S FILE"


@pytest.mark.asyncio
async def test_an_existing_destination_is_never_overwritten(tmp_path, scratch, monkeypatch):
    """A file that appeared at output_path after planning is not this job's:
    placement refuses it instead of replacing it, so its bytes survive, and the
    job fails saying why."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    out = tmp_path / "g.xiso.iso"
    out.write_bytes(b"PRECIOUS")
    monkeypatch.setattr(SERVICE._runner, "run", _fake_run)

    with pytest.raises(RuntimeError, match="not overwriting it"):
        async for _ in SERVICE.convert(str(src), str(out), "xiso_rewrite"):
            pass

    assert out.read_bytes() == b"PRECIOUS"
    assert not _workdirs(tmp_path)


def test_place_without_clobbering_links_and_refuses(tmp_path):
    from utils.path_utils import place_without_clobbering

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"NEW")
    place_without_clobbering(str(src), str(dst))
    assert dst.read_bytes() == b"NEW" and not src.exists()

    src.write_bytes(b"NEWER")
    with pytest.raises(FileExistsError):
        place_without_clobbering(str(src), str(dst))
    assert dst.read_bytes() == b"NEW" and src.exists()


def test_place_without_clobbering_without_hard_links_uses_a_no_replace_rename(
    tmp_path, monkeypatch,
):
    """No hard links (FAT/exFAT): the kernel's atomic no-replace rename
    (renameat2 / renamex_np) places the file and refuses an existing one."""
    import errno

    import utils.path_utils as pu

    if pu._noreplace_rename_call() is None:
        pytest.skip("no atomic no-replace rename on this platform")

    def no_links(_a, _b):
        raise OSError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(pu.os, "link", no_links)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"NEW")
    pu.place_without_clobbering(str(src), str(dst))
    assert dst.read_bytes() == b"NEW" and not src.exists()

    src.write_bytes(b"NEWER")
    with pytest.raises(FileExistsError):
        pu.place_without_clobbering(str(src), str(dst))
    assert dst.read_bytes() == b"NEW" and src.read_bytes() == b"NEWER"


def test_place_without_clobbering_refuses_when_no_atomic_option_exists(
    tmp_path, monkeypatch,
):
    """Neither hard links nor a no-replace rename: fail safely, touching
    nothing, rather than fall back to a check-then-rename."""
    import errno

    import utils.path_utils as pu

    def no_links(_a, _b):
        raise OSError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(pu.os, "link", no_links)
    monkeypatch.setattr(pu, "_rename_noreplace", lambda _a, _b: False)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"NEW")
    with pytest.raises(OSError) as raised:
        pu.place_without_clobbering(str(src), str(dst))
    assert raised.value.errno == errno.ENOTSUP
    assert src.read_bytes() == b"NEW" and not dst.exists()


def test_a_failed_source_cleanup_after_linking_is_not_a_failed_placement(
    tmp_path, monkeypatch,
):
    """Once the link exists the product is placed; an I/O error removing the
    source must not be reported as a failure (the caller would orphan dst)."""
    import errno

    import utils.path_utils as pu

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"NEW")
    real_unlink = os.unlink

    def flaky_unlink(path, *a, **kw):
        if str(path) == str(src):
            raise OSError(errno.EIO, "I/O error")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(pu.os, "unlink", flaky_unlink)
    pu.place_without_clobbering(str(src), str(dst))
    assert dst.read_bytes() == b"NEW"


def test_remove_if_same_file_leaves_a_replacement_alone(tmp_path):
    from utils.path_utils import is_same_file, remove_if_same_file

    path = tmp_path / "out"
    path.write_bytes(b"OURS")
    ours = os.stat(path)
    assert is_same_file(str(path), ours)

    path.unlink()
    path.write_bytes(b"THEIRS")  # someone else's file at the same name
    remove_if_same_file(str(path), ours)
    assert path.read_bytes() == b"THEIRS"

    ours = os.stat(path)
    remove_if_same_file(str(path), ours)
    assert not path.exists()


def test_a_transient_read_error_is_not_cached_as_not_xbox(tmp_path, monkeypatch):
    """An I/O error during the header probe makes this call say "no", but is
    not memoised: once storage recovers, the same unchanged file is accepted."""
    src = tmp_path / "g.iso"
    build_image(src, layout="XGD2")
    MODULE._probe_convertible.cache_clear()
    real_detect = MODULE.detect_layout
    failing = {"on": True}

    def flaky_detect(path):
        if failing["on"]:
            raise OSError(5, "Input/output error")
        return real_detect(path)

    monkeypatch.setattr(MODULE, "detect_layout", flaky_detect)
    assert SERVICE.is_convertible(str(src)) is False
    failing["on"] = False
    assert SERVICE.is_convertible(str(src)) is True
    MODULE._probe_convertible.cache_clear()
