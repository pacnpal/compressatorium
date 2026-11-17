"""rom-converto (#422): the tool plugin and its first mode, Xbox 360 ISO -> ZAR.

A stub ``rom-converto`` script stands in for the real binary. It behaves the
way the spike (#421) recorded v0.24.0 behaving: everything on stderr, nothing
on stdout, the product written under a hidden temp name beside the requested
output and renamed into place at the end, ``Error: ...`` and exit 1 on a bad
input, ``Hash: OK`` from ``xenon verify``. Every call it gets is logged, so the
flags the service passes are pinned too. The Xbox images are the real XDVDFS
fixtures from the xiso suite. ``test_rom_converto_real.py`` runs the real
binary when it is installed.
"""
from __future__ import annotations

import inspect
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routes import convert as convert_routes
from app.services.romm import converter
from app.services.tools import registry
from app.services.tools.spec import ModeKind

from .test_xiso_service import build_image

TOOL = registry.get("rom_converto")
SERVICE = TOOL._service

_STUB = r'''
import json, os, sys

log = os.environ.get("STUB_LOG")
args = sys.argv[1:]
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"args": args, "no_color": os.environ.get("NO_COLOR")}) + "\n")
if args == ["--version"]:
    print("rom-converto 0.24.0")
    sys.exit(0)
flags = [a for a in args if a.startswith("--")]
rest = [a for a in args if not a.startswith("--")]
if rest[:2] == ["xenon", "compress"]:
    src, out = rest[2], rest[3]
    if "bad" in os.path.basename(src):
        sys.stderr.write("Error: no XDVDFS volume descriptor found at any probed base\n")
        sys.exit(1)
    if "nobootexe" in os.path.basename(src):
        sys.stderr.write(
            "[WARN] archive has no root-level default.xex; Xenia will refuse to mount it\n"
        )
    temp = os.path.join(os.path.dirname(out), "." + os.path.basename(out) + ".k0NJ5b.tmp")
    with open(src, "rb") as fh_in, open(temp, "wb") as fh_out:
        fh_out.write(b"ZARSTUB" + fh_in.read(64))
    os.replace(temp, out)
    sys.stderr.write("1 files: 1.0 MiB -> 71 B, saved 1.0 MiB (100%) in 0.0s\n")
    sys.exit(0)
if rest[:2] == ["xenon", "verify"]:
    with open(rest[2], "rb") as fh:
        head = fh.read(7)
    if head != b"ZARSTUB":
        sys.stderr.write("Error: invalid ZArchive magic: got 0x00000000\n")
        sys.exit(1)
    sys.stderr.write("Blocks: 1\nLogical bytes: 8100\nHash: OK\n")
    sys.exit(0)
if rest[:2] == ["xenon", "info"]:
    sys.stdout.write(
        "Container:\n"
        "  Format:            Xbox 360 ZArchive\n"
        "  Files:             3\n"
        "  Directories:       2\n"
        "  Logical bytes:     8100\n"
        "  Compressed bytes:  348\n"
        "  Blocks:            1\n"
        "  default.xex:       present\n"
        "\nInner files:\n    default.xex  3000 bytes\n"
    )
    sys.exit(0)
sys.stderr.write("error: unrecognized subcommand\n")
sys.exit(2)
'''


def _write_stub(path: Path, body: str = _STUB) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture(name="stub")
def _stub(tmp_path, monkeypatch):
    """The stub binary, wired in as the service's rom-converto, with a call log."""
    binary = _write_stub(tmp_path / "rom-converto")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setattr(SERVICE, "binary_path", str(binary))
    SERVICE.forget_readiness()
    yield {"binary": binary, "log": log}
    SERVICE.forget_readiness()


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


async def _drain(gen) -> list[dict]:
    return [update async for update in gen]


def _library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    return lib


# --------------------------------------------------------------------------- #
# The mode, as the registry sees it
# --------------------------------------------------------------------------- #


def test_mode_spec():
    spec = registry.spec("zar_compress")
    assert registry.for_mode("zar_compress") is TOOL
    assert spec.tool_id == "rom_converto"
    assert spec.kind == ModeKind.COMPRESS
    assert spec.output_ext == ".zar"
    assert spec.input_extensions == frozenset({".iso"})
    assert spec.platform_slugs == frozenset({"xbox360"})
    # Checked after conversion (xenon verify decodes every block), but its
    # Source is kept: a ZAR can't be decoded back to the ISO (spike #421).
    assert spec.supports_verify is True
    assert spec.supports_delete_on_verify is False
    assert spec.supports_round_trip is False
    assert "Source" in spec.source_kept_reason
    assert "ISO" in spec.source_kept_reason
    # Not an Auto default: Xbox 360 stays on the XISO trim unless chosen.
    assert spec.auto_target is False
    assert spec.allows_archive_input is False
    assert registry.mode_is_automatable("zar_compress")
    assert TOOL.verify_extensions == frozenset({".zar"})
    assert ".zar" in registry.output_extensions()


def test_every_mode_that_keeps_its_source_can_say_why():
    """The contract: a mode with a reason never offers delete-on-verify."""
    for spec in registry.mode_specs():
        if spec.source_kept_reason:
            assert not spec.supports_delete_on_verify, spec.mode


@pytest.mark.parametrize(
    ("layout", "exe", "expected"),
    [
        ("XGD2", "default.xex", True),
        ("XGD3", "default.xex", True),
        # A trimmed image: the boot executable says which console.
        ("XISO", "default.xex", True),
        # Original Xbox: ZAR is a Xenia (Xbox 360) format.
        ("XGD1", "default.xbe", False),
        ("XISO", "default.xbe", False),
    ],
)
def test_only_xbox_360_images_are_offered(tmp_path, layout, exe, expected):
    img = tmp_path / "game.iso"
    build_image(img, layout=layout, exe=exe)
    assert TOOL.converts_path(str(img)) is expected


def test_a_non_xbox_iso_and_a_zar_are_not_offered(tmp_path):
    psp = tmp_path / "psp.iso"
    psp.write_bytes(b"\0" * 0x8000 + b"\x01CD001" + b"\0" * 0x20000)
    zar = tmp_path / "game.zar"
    zar.write_bytes(b"ZARSTUB")
    assert not TOOL.converts_path(str(psp))
    assert not TOOL.converts_path(str(zar))


def test_plan_job_names_why_a_non_xbox_360_iso_is_skipped():
    reason = convert_routes._NOT_CONVERTIBLE_REASON["rom_converto"]
    _status, message = convert_routes._SKIP_HTTP[reason]
    assert "Xbox 360" in message


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("/v/Halo 3.iso", "/v/Halo 3.zar"),
        ("/v/Halo 3.ISO", "/v/Halo 3.zar"),
        # A trimmed XISO from the xiso tool is a fine input too.
        ("/v/Halo 3.xiso.iso", "/v/Halo 3.zar"),
    ],
)
def test_output_path(inp, expected):
    assert TOOL.output_path("zar_compress", inp) == expected


def test_output_path_honours_output_dir():
    assert TOOL.output_path("zar_compress", "/v/Halo 3.iso", "/out") == "/out/Halo 3.zar"


def test_romm_coexistence_now_matches_xbox_360_zar():
    """RomM's ``zar`` target for Xbox 360 has a mode here now (#385 flags)."""
    assert converter.modes_for_target("xbox360", "zar") == ["zar_compress"]


# --------------------------------------------------------------------------- #
# Converting, with the stub binary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_convert_writes_the_zar_and_cleans_up(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "Halo 3.iso"
    build_image(src, layout="XISO")
    before = src.read_bytes()
    out = lib / "Halo 3.zar"

    updates = await _drain(TOOL.convert(str(src), str(out), "zar_compress"))

    assert out.read_bytes().startswith(b"ZARSTUB")
    assert updates[-1]["progress"] == 100
    assert "ZAR" in updates[-1]["message"]
    assert src.read_bytes() == before
    # Nothing else is left beside the output: no hidden work dir, no temp file.
    assert sorted(p.name for p in lib.iterdir()) == ["Halo 3.iso", "Halo 3.zar"]
    (call,) = [c for c in _calls(stub["log"]) if "compress" in c["args"]]
    args = call["args"]
    # Off the network, no persistent cache, no colour codes, and an explicit
    # OUTPUT inside a private work dir beside the real one.
    assert args[:2] == ["--no-update-check", "--no-cache"]
    assert args[2:5] == ["xenon", "compress", str(src)]
    work = Path(args[5]).parent
    assert work.parent == lib and work.name.startswith(".")
    assert Path(args[5]).name == "Halo 3.zar"
    assert call["no_color"] == "1"


@pytest.mark.asyncio
async def test_convert_follows_growth_in_its_work_dir(tmp_path, stub, monkeypatch):
    """The stall bound watches the work dir, not the output path: rom-converto
    writes a temp name of its own and is silent on a pipe (ruling 2, #422)."""
    lib = _library(tmp_path)
    src = lib / "Halo 3.iso"
    build_image(src, layout="XISO")
    seen: dict = {}

    async def fake_run(cmd, *, output_path, output_growth_paths=None, **_kw):
        work = Path(output_path).parent
        (work / ".Halo 3.zar.abc.tmp").write_bytes(b"x" * 10)
        seen["paths"] = output_growth_paths()
        Path(output_path).write_bytes(b"ZARSTUB")
        yield {"progress": 100, "message": "done", "activity": True}

    monkeypatch.setattr(SERVICE._runner, "run", fake_run)
    await _drain(TOOL.convert(str(src), str(lib / "Halo 3.zar"), "zar_compress"))

    assert [Path(p).name for p in seen["paths"]] == [".Halo 3.zar.abc.tmp"]


@pytest.mark.asyncio
async def test_convert_failure_says_what_rom_converto_said(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "bad.iso"
    build_image(src, layout="XISO")

    with pytest.raises(RuntimeError, match="no XDVDFS volume descriptor"):
        await _drain(TOOL.convert(str(src), str(lib / "bad.zar"), "zar_compress"))

    assert sorted(p.name for p in lib.iterdir()) == ["bad.iso"]


@pytest.mark.asyncio
async def test_convert_marks_rom_convertos_warning(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "nobootexe.iso"
    build_image(src, layout="XISO")

    updates = await _drain(TOOL.convert(str(src), str(lib / "nobootexe.zar"), "zar_compress"))

    warnings = [u for u in updates if u.get("warning")]
    assert warnings and "Xenia will refuse to mount it" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_convert_never_overwrites_an_output_that_appeared(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "Halo 3.iso"
    build_image(src, layout="XISO")
    out = lib / "Halo 3.zar"
    out.write_bytes(b"someone else's")

    with pytest.raises(RuntimeError, match="not overwriting"):
        await _drain(TOOL.convert(str(src), str(out), "zar_compress"))

    assert out.read_bytes() == b"someone else's"
    assert sorted(p.name for p in lib.iterdir()) == ["Halo 3.iso", "Halo 3.zar"]


@pytest.mark.asyncio
async def test_convert_refuses_a_source_that_is_not_xbox_360(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "og.iso"
    build_image(src, layout="XGD1", exe="default.xbe")

    with pytest.raises(ValueError, match="Xbox 360"):
        await _drain(TOOL.convert(str(src), str(lib / "og.zar"), "zar_compress"))

    assert not [c for c in _calls(stub["log"]) if "compress" in c["args"]]


# --------------------------------------------------------------------------- #
# Readiness: a missing or broken binary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ready_with_a_working_binary(stub):
    assert await TOOL.is_ready() is True


@pytest.mark.asyncio
async def test_a_missing_binary_is_not_ready_and_says_where_it_looked(tmp_path, monkeypatch):
    missing = tmp_path / "nowhere" / "rom-converto"
    monkeypatch.setattr(SERVICE, "binary_path", str(missing))
    SERVICE.forget_readiness()

    assert await TOOL.is_ready() is False
    assert str(missing) in TOOL.not_ready_reason
    # The rest of the registry still answers.
    assert await registry.get("xiso").is_ready() in (True, False)
    SERVICE.forget_readiness()


@pytest.mark.asyncio
async def test_a_broken_binary_is_not_ready_and_says_why(tmp_path, monkeypatch):
    broken = _write_stub(
        tmp_path / "rom-converto",
        "import sys\nsys.stderr.write('cannot execute binary file: Exec format error\\n')\n"
        "sys.exit(126)\n",
    )
    monkeypatch.setattr(SERVICE, "binary_path", str(broken))
    SERVICE.forget_readiness()

    assert await TOOL.is_ready() is False
    assert "did not run" in TOOL.not_ready_reason
    assert "Exec format error" in TOOL.not_ready_reason
    SERVICE.forget_readiness()


@pytest.mark.asyncio
async def test_the_version_probe_never_forks_a_python_preexec(stub, monkeypatch):
    """Readiness runs the binary in this threaded process: nice is a command
    wrapper, never a preexec_fn, which can deadlock the child before exec."""
    seen: dict = {}

    async def capture(cmd, **kwargs):
        seen.update(kwargs)
        return 0, b"rom-converto 0.24.0\n", b""

    monkeypatch.setattr(SERVICE._runner, "run_capture", capture)
    assert await TOOL.is_ready() is True
    assert seen.get("nice_via_wrapper") is True


# --------------------------------------------------------------------------- #
# Verify and info
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_verify_passes_a_good_zar_and_rehashes_it(tmp_path, stub):
    zar = tmp_path / "Halo 3.zar"
    zar.write_bytes(b"ZARSTUB" + b"\0" * 64)

    result = await TOOL.verify(str(zar))

    assert result["valid"] is True
    (call,) = [c for c in _calls(stub["log"]) if "verify" in c["args"]]
    # --no-cache: a verify must decode the file, never answer from a cache.
    assert call["args"] == ["--no-update-check", "--no-cache", "xenon", "verify", str(zar)]


@pytest.mark.asyncio
async def test_verify_fails_a_bad_zar(tmp_path, stub):
    zar = tmp_path / "Halo 3.zar"
    zar.write_bytes(b"\0" * 64)

    result = await TOOL.verify(str(zar))

    assert result["valid"] is False
    assert "invalid ZArchive magic" in result["message"]


@pytest.mark.asyncio
async def test_verify_only_claims_zar_files(tmp_path, stub):
    iso = tmp_path / "Halo 3.iso"
    iso.write_bytes(b"x" * 64)
    result = await TOOL.verify(str(iso))
    assert result["valid"] is False
    assert not _calls(stub["log"])


@pytest.mark.asyncio
async def test_info_reads_the_zar_container(tmp_path, stub):
    zar = tmp_path / "Halo 3.zar"
    zar.write_bytes(b"ZARSTUB" + b"\0" * 64)

    raw = await TOOL.info(str(zar))
    model = TOOL.info_model(raw, str(zar))

    assert model.format == "Xbox 360 ZArchive"
    assert model.extension == ".zar"
    assert model.compressed is True
    assert model.file_count == 3
    assert model.logical_size == 8100
    assert model.boot_executable is True


# --------------------------------------------------------------------------- #
# Delete-on-verify: refused, with the mode's own reason
# --------------------------------------------------------------------------- #


def test_delete_on_verify_is_refused_with_the_reason():
    spec = registry.spec("zar_compress")
    with pytest.raises(HTTPException) as excinfo:
        convert_routes._validate_delete_on_verify(spec, True)
    assert excinfo.value.status_code == 400
    assert spec.source_kept_reason in excinfo.value.detail


def test_detect_output_badges_an_existing_zar(tmp_path):
    src = tmp_path / "Halo 3.iso"
    build_image(src, layout="XISO")
    assert TOOL.detect_output(str(src)) is None
    (tmp_path / "Halo 3.zar").write_bytes(b"ZARSTUB")
    status = TOOL.detect_output(str(src))
    assert status is not None and status.exists and status.tool_id == "rom_converto"


def test_the_service_runs_through_the_shared_runner():
    # The shared runner: nice/ionice policy, stall bound, PID tracking, cancel.
    assert SERVICE._runner.owner == "rom_converto"
    assert inspect.iscoroutinefunction(TOOL.is_ready)
    assert os.path.basename(SERVICE.binary_path) == "rom-converto"


# --------------------------------------------------------------------------- #
# End to end: a job, and a Rule
# --------------------------------------------------------------------------- #


@pytest.fixture(name="app_db")
def _app_db(tmp_path, reset_db_engine):
    """The app database the job pipeline records its checks in."""
    from services import db as _db  # the module the stores read

    reset_db_engine()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    yield
    reset_db_engine()


@pytest.mark.asyncio
async def test_a_job_converts_end_to_end_and_keeps_the_source(
    tmp_path, stub, app_db, monkeypatch,
):
    """The job pipeline with the stub binary: convert, verify, Source kept."""
    from app.models import ConversionMode, JobStatus
    from app.services import job_manager as job_manager_module
    from app.services.job_manager import JobManager

    lib = _library(tmp_path)
    src = lib / "Halo 3.iso"
    build_image(src, layout="XISO")
    out = lib / "Halo 3.zar"
    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    manager = JobManager(max_concurrent=1, max_job_history=5)
    job = await manager.create_job(
        str(src), ConversionMode.ZAR_COMPRESS, output_path=str(out), verify_after=True,
    )
    await manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert out.read_bytes().startswith(b"ZARSTUB")
    assert src.exists()
    called = [c["args"][2:4] for c in _calls(stub["log"]) if len(c["args"]) > 3]
    assert ["xenon", "compress"] in called
    assert ["xenon", "verify"] in called


def test_a_rule_keeps_zar_and_its_verify_but_never_deletes():
    from app.services.rules.rules import normalize_rule

    rule = normalize_rule({
        "enabled": True, "mode": "zar_compress",
        "verify_after": True, "delete_on_verify": True,
    })

    assert rule is not None and rule["mode"] == "zar_compress"
    assert rule["verify_after"] is True
    assert rule["delete_on_verify"] is False
