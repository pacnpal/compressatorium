"""rom-converto (#423): PSP PBP -> ISO and 3DS CIA <-> CCI, with the Recognized proof.

A stub ``rom-converto`` stands in for the real binary, behaving the way the
spike (#421) recorded v0.24.0: everything on stderr, the product written under
a hidden temp name beside the requested output and renamed at the end,
``Error: ...`` and exit 1 on a bad input. Its products are the synthetic
containers in ``rom_converto_fixtures`` (hand-built headers, never a game).

The Recognized proof is driven the way a user drives it: a job through the
convert route, a DAT imported into the app's own store (or a Hasheous answer
at its lookup seam), then the Source, the job's message and the Ledger row.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.routes import convert as convert_routes
from app.services.romm import converter
from app.services.tools import registry
from app.services.tools.spec import DeleteProof, ModeKind

from . import rom_converto_fixtures as fx

TOOL = registry.get("rom_converto")
SERVICE = TOOL._service
NEW_MODES = ("pbp_to_iso", "cia_to_cci", "cci_to_cia")

_STUB = r'''
import json, os, sys
import rom_converto_fixtures as fx

log = os.environ.get("STUB_LOG")
args = sys.argv[1:]
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"args": args}) + "\n")
if args == ["--version"]:
    print("rom-converto 0.24.0")
    sys.exit(0)
rest = [a for a in args if not a.startswith("--")]


def write(out, data):
    temp = os.path.join(os.path.dirname(out), "." + os.path.basename(out) + ".k0NJ5b.tmp")
    with open(temp, "wb") as fh:
        fh.write(data)
    os.replace(temp, out)


if rest[:2] == ["psp", "to-iso"]:
    src, out = rest[2], rest[3]
    with open(src, "rb") as fh:
        data = fh.read()
    if b"NPUMDIMG" not in data:
        sys.stderr.write("Error: psp to-iso: DATA.PSAR magic is PSISOIMG, not NPUMDIMG\n")
        sys.exit(1)
    write(out, fx.iso_bytes(payload=data[-64:]))
    sys.stderr.write("Converted PBP to ISO\n")
    sys.exit(0)
if rest[:2] == ["ctr", "convert"]:
    src, out = rest[2], rest[3]
    name = os.path.basename(src)
    if "needkeys" in name:
        sys.stderr.write("Error: could not decrypt the content: title key not found\n")
        sys.exit(1)
    with open(src, "rb") as fh:
        data = fh.read()
    if src.lower().endswith(".cia"):
        write(out, fx.cci_bytes(payload=data[-64:]))
        sys.stderr.write(f"Converted CIA to CCI: {src} -> {out}\n")
    else:
        if data[0x100:0x104] != b"NCSD":
            sys.stderr.write(
                "Error: input is not a valid NCSD file (missing NCSD magic at 0x100)\n"
            )
            sys.exit(1)
        write(out, fx.cia_bytes(payload=data[-64:]))
        sys.stderr.write(f"Converted CCI to CIA: {src} -> {out}\n")
    sys.exit(0)
sys.stderr.write("error: unrecognized subcommand\n")
sys.exit(2)
'''


@pytest.fixture(name="stub")
def _stub(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "rom-converto"
    binary.parent.mkdir()
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    binary.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {tests_dir!r})\n{_STUB}",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setattr(SERVICE, "binary_path", str(binary))
    SERVICE.forget_readiness()
    yield {"binary": binary, "log": log}
    SERVICE.forget_readiness()


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line)["args"] for line in log.read_text().splitlines()]


async def _drain(gen) -> list[dict]:
    return [update async for update in gen]


def _library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    return lib


# --------------------------------------------------------------------------- #
# The modes, as the registry sees them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mode", "inputs", "output", "slugs"),
    [
        ("pbp_to_iso", {".pbp"}, ".iso", {"psp"}),
        ("cia_to_cci", {".cia"}, ".cci", {"3ds", "n3ds", "new-nintendo-3ds"}),
        ("cci_to_cia", {".cci", ".3ds"}, ".cia", {"3ds", "n3ds", "new-nintendo-3ds"}),
    ],
)
def test_mode_specs(mode, inputs, output, slugs):
    spec = registry.spec(mode)
    assert registry.for_mode(mode) is TOOL
    assert spec.kind == ModeKind.COMPRESS
    assert spec.input_extensions == frozenset(inputs)
    assert spec.output_ext == output
    assert spec.platform_slugs == frozenset(slugs)
    # Deletable, but only on the Recognized proof: these outputs can't be
    # Round-tripped against their Source (spike #421).
    assert spec.supports_delete_on_verify is True
    assert spec.supports_round_trip is False
    assert spec.delete_proof is DeleteProof.RECOGNIZED
    assert registry.mode_delete_proof(mode) is DeleteProof.RECOGNIZED
    assert spec.source_kept_reason == ""
    assert spec.auto_target is False
    assert spec.allows_archive_input is False
    assert registry.mode_is_automatable(mode)
    assert output in TOOL.verify_extensions


def test_every_other_mode_keeps_the_round_trip_default():
    for spec in registry.mode_specs():
        if spec.mode not in NEW_MODES:
            assert spec.delete_proof is DeleteProof.ROUND_TRIP, spec.mode
    assert registry.mode_delete_proof("no_such_mode") is DeleteProof.ROUND_TRIP


def test_a_recognized_mode_never_round_trips():
    for spec in registry.mode_specs():
        if spec.delete_proof is DeleteProof.RECOGNIZED:
            assert spec.supports_delete_on_verify and not spec.supports_round_trip


def test_the_tool_serves_every_modes_platform():
    assert TOOL.platform_slugs == frozenset(
        {"xbox360", "psp", "3ds", "n3ds", "new-nintendo-3ds"},
    )


@pytest.mark.parametrize(
    ("mode", "inp", "expected"),
    [
        ("pbp_to_iso", "/v/Game.pbp", "/v/Game.iso"),
        ("pbp_to_iso", "/v/EBOOT.PBP", "/v/EBOOT.iso"),
        ("cia_to_cci", "/v/Game.cia", "/v/Game.cci"),
        ("cci_to_cia", "/v/Game.cci", "/v/Game.cia"),
        ("cci_to_cia", "/v/Game.3DS", "/v/Game.cia"),
        ("zar_compress", "/v/Halo 3.iso", "/v/Halo 3.zar"),
    ],
)
def test_output_path(mode, inp, expected):
    assert TOOL.output_path(mode, inp) == expected


def test_output_path_honours_output_dir():
    assert TOOL.output_path("cia_to_cci", "/v/Game.cia", "/out") == "/out/Game.cci"


def test_romm_coexistence_now_matches_psp_iso_and_3ds_cia_cci():
    assert converter.modes_for_target("psp", "iso") == ["pbp_to_iso"]
    assert converter.modes_for_target("3ds", "cci") == ["cia_to_cci"]
    assert converter.modes_for_target("3ds", "cia") == ["cci_to_cia"]


# --------------------------------------------------------------------------- #
# Which files each mode is offered on
# --------------------------------------------------------------------------- #


def test_a_psp_game_pbp_is_offered_and_a_ps1_classic_is_not(tmp_path):
    game = tmp_path / "Game.pbp"
    game.write_bytes(fx.pbp_bytes(b"NPUMDIMG"))
    classic = tmp_path / "Classic.pbp"
    classic.write_bytes(fx.pbp_bytes(b"PSISOIMG"))
    junk = tmp_path / "junk.pbp"
    junk.write_bytes(b"\0" * 64)
    assert TOOL.converts_path(str(game))
    assert not TOOL.converts_path(str(classic))
    assert not TOOL.converts_path(str(junk))


def test_cia_and_cci_are_offered_by_their_headers(tmp_path):
    cia = tmp_path / "Game.cia"
    cia.write_bytes(fx.cia_bytes())
    cci = tmp_path / "Game.3ds"
    cci.write_bytes(fx.cci_bytes())
    fake_cia = tmp_path / "fake.cia"
    fake_cia.write_bytes(b"\0" * 0x3000)
    fake_cci = tmp_path / "fake.cci"
    fake_cci.write_bytes(b"\0" * 0x400)
    assert TOOL.converts_path(str(cia))
    assert TOOL.converts_path(str(cci))
    assert not TOOL.converts_path(str(fake_cia))
    assert not TOOL.converts_path(str(fake_cci))


@pytest.mark.parametrize(
    ("mode", "word"),
    [("pbp_to_iso", "PSP"), ("cia_to_cci", "CIA"), ("cci_to_cia", "CCI"),
     ("zar_compress", "Xbox 360")],
)
def test_plan_job_names_why_each_mode_skips_a_file(mode, word):
    reason = convert_routes._not_convertible_reason(mode, "rom_converto")
    _status, message = convert_routes._SKIP_HTTP[reason]
    assert word in message


# --------------------------------------------------------------------------- #
# Converting, with the stub binary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pbp_to_iso_writes_the_iso_and_cleans_up(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "Game.pbp"
    src.write_bytes(fx.pbp_bytes(payload=b"game data" * 8))
    out = lib / "Game.iso"

    updates = await _drain(TOOL.convert(str(src), str(out), "pbp_to_iso"))

    assert out.read_bytes()[0x8001:0x8006] == b"CD001"
    assert updates[-1]["progress"] == 100
    assert sorted(p.name for p in lib.iterdir()) == ["Game.iso", "Game.pbp"]
    (args,) = [a for a in _calls(stub["log"]) if "to-iso" in a]
    assert args[:5] == ["--no-update-check", "--no-cache", "psp", "to-iso", str(src)]
    work = Path(args[5]).parent
    assert work.parent == lib and work.name.startswith(".")


@pytest.mark.asyncio
async def test_cia_to_cci_writes_a_trimmed_cci(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "Game.cia"
    src.write_bytes(fx.cia_bytes(payload=b"cia data"))
    out = lib / "Game.cci"

    await _drain(TOOL.convert(str(src), str(out), "cia_to_cci"))

    assert out.read_bytes()[0x100:0x104] == b"NCSD"
    assert sorted(p.name for p in lib.iterdir()) == ["Game.cci", "Game.cia"]
    (args,) = [a for a in _calls(stub["log"]) if "convert" in a]
    assert args[2:5] == ["ctr", "convert", str(src)]
    # The product is written as `.3ds` (the name the spike ran) in the work
    # folder, trimmed so a small game isn't padded to its card's size.
    assert Path(args[5]).suffix == ".3ds"
    assert "--trim" in args


@pytest.mark.asyncio
async def test_cci_to_cia_reads_a_3ds_file(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "Game.3ds"
    src.write_bytes(fx.cci_bytes(payload=b"cci data"))
    out = lib / "Game.cia"

    await _drain(TOOL.convert(str(src), str(out), "cci_to_cia"))

    assert int.from_bytes(out.read_bytes()[:4], "little") == fx.CIA_HEADER_SIZE
    (args,) = [a for a in _calls(stub["log"]) if "convert" in a]
    assert Path(args[5]).suffix == ".cia"
    assert "--trim" not in args


@pytest.mark.asyncio
async def test_an_input_that_needs_keys_fails_and_says_so(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "needkeys.cia"
    src.write_bytes(fx.cia_bytes())

    with pytest.raises(RuntimeError) as excinfo:
        await _drain(TOOL.convert(str(src), str(lib / "needkeys.cci"), "cia_to_cci"))

    message = str(excinfo.value)
    assert "keys or a seed" in message
    assert "never downloads or ships" in message
    assert "title key not found" in message
    assert sorted(p.name for p in lib.iterdir()) == ["needkeys.cia"]


@pytest.mark.asyncio
async def test_a_rom_converto_refusal_is_passed_on(tmp_path, stub, monkeypatch):
    lib = _library(tmp_path)
    src = lib / "Game.pbp"
    src.write_bytes(fx.pbp_bytes(b"NPUMDIMG"))
    # Passes the probe, then rom-converto refuses it (it reads more than we do).
    monkeypatch.setattr(SERVICE, "is_source_for", lambda _mode, _path: True)
    src.write_bytes(fx.pbp_bytes(b"PSISOIMG"))

    with pytest.raises(RuntimeError, match="not NPUMDIMG"):
        await _drain(TOOL.convert(str(src), str(lib / "Game.iso"), "pbp_to_iso"))
    assert sorted(p.name for p in lib.iterdir()) == ["Game.pbp"]


@pytest.mark.asyncio
async def test_convert_refuses_a_file_that_is_not_its_source(tmp_path, stub):
    lib = _library(tmp_path)
    src = lib / "fake.cia"
    src.write_bytes(b"\0" * 0x3000)

    with pytest.raises(ValueError, match="CIA"):
        await _drain(TOOL.convert(str(src), str(lib / "fake.cci"), "cia_to_cci"))
    assert not _calls(stub["log"])


# --------------------------------------------------------------------------- #
# The container check of each output
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "good"),
    [
        ("Game.iso", fx.iso_bytes()),
        ("Game.cci", fx.cci_bytes()),
        ("Game.cia", fx.cia_bytes()),
    ],
)
async def test_a_whole_output_is_intact_and_a_cut_one_is_not(tmp_path, name, good):
    whole = tmp_path / name
    whole.write_bytes(good)
    cut = tmp_path / f"cut{Path(name).suffix}"
    cut.write_bytes(good[: len(good) - 600])
    junk = tmp_path / f"junk{Path(name).suffix}"
    junk.write_bytes(b"\0" * len(good))

    assert (await TOOL.verify(str(whole)))["valid"] is True
    cut_result = await TOOL.verify(str(cut))
    assert cut_result["valid"] is False and "truncated" in cut_result["message"]
    assert (await TOOL.verify(str(junk)))["valid"] is False


def test_the_listing_offers_verify_only_on_a_zar(tmp_path):
    """Only the ZAR's check is a row action; the others run inside a job."""
    iso = tmp_path / "Game.iso"
    iso.write_bytes(fx.iso_bytes())
    assert not TOOL.verifies_path(str(iso))
    assert TOOL.verifies_path(str(tmp_path / "Game.zar"))


# --------------------------------------------------------------------------- #
# End to end: POST /jobs with the stub, and the Recognized proof
# --------------------------------------------------------------------------- #


@pytest.fixture(name="e2e")
def _e2e(tmp_path, monkeypatch, stub, db_path, reset_db_engine):
    from services import db as _db
    from services import hasheous

    _db.init_engine(db_path)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(convert_routes.settings, "hasheous_enabled", False)
    hasheous.set_enabled_override(None)
    yield {"tmp_path": tmp_path, "lib": _library(tmp_path), "stub": stub}
    reset_db_engine()


async def _run(source: Path, mode: str, **fields):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    request = JobCreateRequest(
        file_path=str(source), mode=ConversionMode(mode),
        duplicate_action=DuplicateAction.SKIP, **fields,
    )
    job = await convert_routes.create_job(request)
    await convert_routes.job_manager._process_job(job.id)
    return job


def _dat(name: str, sha1: str) -> str:
    return f"""<?xml version="1.0"?>
<datafile>
  <header><name>{name}</name><description>{name}</description></header>
  <game name="Some Game">
    <rom name="Some Game.iso" size="36864" sha1="{sha1}"/>
  </game>
</datafile>
"""


def _pbp(lib: Path) -> tuple[Path, str]:
    """A PBP, and the SHA-1 of the ISO the stub makes of it."""
    src = lib / "Game.pbp"
    data = fx.pbp_bytes(payload=b"umd image" * 16)
    src.write_bytes(data)
    return src, hashlib.sha1(fx.iso_bytes(payload=data[-64:])).hexdigest()


async def _ledger_row(job) -> dict:
    from services import ledger

    await ledger.record(job)
    (row,) = [r for r in await ledger.recent(10) if r["job_id"] == job.id]
    return row


@pytest.mark.asyncio
async def test_a_recognized_output_lets_the_source_go(e2e):
    from services.dat_store import dat_store

    src, sha1 = _pbp(e2e["lib"])
    await dat_store.import_dat(_dat("Sony - PlayStation Portable (Redump)", sha1))

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert not src.exists()
    assert (e2e["lib"] / "Game.iso").exists()
    assert job.check_passed == "recognized"
    assert job.check_dat == "Sony - PlayStation Portable (Redump)"
    assert "Recognized" in job.message
    row = await _ledger_row(job)
    assert row["check_passed"] == "recognized"
    assert row["check_dat"] == "Sony - PlayStation Portable (Redump)"
    assert row["source_deleted"] is True


@pytest.mark.asyncio
async def test_an_output_nobody_recognizes_keeps_the_source_and_says_why(e2e):
    src, _sha1 = _pbp(e2e["lib"])

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert src.exists()
    assert (e2e["lib"] / "Game.iso").exists()
    assert job.warning == "converted, source kept: output not Recognized"
    assert job.message.endswith("converted, source kept: output not Recognized")
    assert job.check_passed == "intact"
    assert job.check_dat is None
    row = await _ledger_row(job)
    assert row["check_passed"] == "intact"
    assert row["source_deleted"] is False


@pytest.mark.asyncio
async def test_a_hasheous_hit_recognizes_the_output(e2e, monkeypatch):
    from services import hasheous, recognized

    src, sha1 = _pbp(e2e["lib"])
    monkeypatch.setattr(convert_routes.settings, "hasheous_enabled", True)
    lookup = AsyncMock(return_value={
        "dat_name": "Redump", "game_name": "Some Game", "source": "hasheous",
    })
    monkeypatch.setattr(recognized.hasheous, "lookup", lookup)

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert not src.exists()
    assert job.check_passed == "recognized"
    assert job.check_dat == "Redump"
    lookup.assert_awaited_once_with(sha1)
    assert hasheous.enabled()


@pytest.mark.asyncio
async def test_hasheous_unreachable_keeps_the_source(e2e, monkeypatch):
    from services import recognized
    from services.hasheous import HasheousUnavailable

    src, _sha1 = _pbp(e2e["lib"])
    monkeypatch.setattr(convert_routes.settings, "hasheous_enabled", True)
    monkeypatch.setattr(
        recognized.hasheous, "lookup", AsyncMock(side_effect=HasheousUnavailable("timeout")),
    )

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert src.exists()
    assert job.warning.startswith("converted, source kept: output not Recognized")
    assert "Hasheous" in job.warning


# --------------------------------------------------------------------------- #
# A DAT fetched over plain HTTP never vouches for a delete (Codex 4659176015)
# --------------------------------------------------------------------------- #

_PSP = "Sony - PlayStation Portable (Redump)"


@pytest.mark.asyncio
async def test_a_dat_fetched_over_plain_http_does_not_vouch_for_a_delete(e2e):
    """#557 lets the Redump sync fall back to plain HTTP. A DAT swapped on the
    way could name any output, so it never lets a Source go: the job
    completes and the Source stays, saying why."""
    from services.dat_store import dat_store

    src, sha1 = _pbp(e2e["lib"])
    await dat_store.import_dat(_dat(_PSP, sha1), transport="http")

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert src.exists()
    assert (e2e["lib"] / "Game.iso").exists()
    assert job.check_passed == "intact"
    assert job.check_dat is None
    assert job.warning.startswith("converted, source kept: output not Recognized")
    assert "plain HTTP" in job.warning and _PSP in job.warning
    row = await _ledger_row(job)
    assert row["source_deleted"] is False


def _forget_transports() -> None:
    """Every DAT as one imported before 0028 recorded how it arrived."""
    from services import db as _db

    with _db.get_session() as session:
        session.execute(_db.DAT.__table__.update().values(transport=None))
        session.commit()


_NO_INTRO = "Sony - PlayStation Portable (No-Intro)"


@pytest.mark.asyncio
@pytest.mark.parametrize(("transport", "name"), [
    ("https", _PSP), ("manual", _PSP), (None, _NO_INTRO),
])
async def test_a_dat_over_https_or_imported_by_hand_still_vouches(e2e, transport, name):
    """The same match over HTTPS, a hand import, and a non-Redump DAT
    recorded before the transport was (None) prove the output as before."""
    from services.dat_store import dat_store

    src, sha1 = _pbp(e2e["lib"])
    kwargs = {} if transport is None else {"transport": transport}
    await dat_store.import_dat(_dat(name, sha1), **kwargs)
    if transport is None:
        _forget_transports()

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert not src.exists()
    assert job.check_passed == "recognized"
    assert job.check_dat == name


@pytest.mark.asyncio
async def test_a_redump_dat_with_no_recorded_transport_does_not_vouch(e2e):
    """Codex 4659261220: a Redump DAT whose transport was never recorded may
    have come over the plain-HTTP fallback, so it is not trusted either."""
    from services.dat_store import dat_store

    src, sha1 = _pbp(e2e["lib"])
    await dat_store.import_dat(_dat(_PSP, sha1))
    _forget_transports()

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert src.exists()
    assert job.check_passed == "intact"
    assert "plain HTTP" in job.warning


@pytest.mark.asyncio
async def test_an_https_dat_vouches_even_when_an_http_dat_also_knows_it(e2e):
    """Only the plain-HTTP entries are set aside: a trusted DAT knowing the
    same hash still proves it, and is the one the Ledger names."""
    from services.dat_store import dat_store

    src, sha1 = _pbp(e2e["lib"])
    await dat_store.import_dat(_dat("Sony - PSP (Redump, over HTTP)", sha1), transport="http")
    await dat_store.import_dat(_dat(_PSP, sha1), transport="https")

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    assert not src.exists()
    assert job.check_passed == "recognized"
    assert job.check_dat == _PSP


@pytest.mark.asyncio
async def test_hasheous_decides_when_only_a_plain_http_dat_knows_the_output(e2e, monkeypatch):
    """Set aside, the plain-HTTP DAT counts as no DAT at all: Hasheous, when
    switched on, is asked exactly as it would be with no DAT knowing it."""
    from services import recognized
    from services.dat_store import dat_store

    src, sha1 = _pbp(e2e["lib"])
    await dat_store.import_dat(_dat(_PSP, sha1), transport="http")
    monkeypatch.setattr(convert_routes.settings, "hasheous_enabled", True)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(recognized.hasheous, "lookup", lookup)

    job = await _run(src, "pbp_to_iso", delete_on_verify=True)

    lookup.assert_awaited_once_with(sha1)
    assert src.exists()
    assert "plain HTTP" in job.warning


@pytest.mark.asyncio
async def test_a_dat_over_plain_http_still_recognizes_and_names_the_output(e2e):
    """Only the delete proof sets it aside: matching (what the Library shows
    and what the Inbox names an Image after) still uses it."""
    from routes.dat import match_one
    from services.dat_store import dat_store

    src, sha1 = _pbp(e2e["lib"])
    await dat_store.import_dat(_dat(_PSP, sha1), transport="http")
    record = dat_store.lookup_sha1(sha1)
    assert record["dat_name"] == _PSP and record["transport"] == "http"

    await _run(src, "pbp_to_iso", delete_on_verify=True)
    result, _cacheable = await match_one(str(e2e["lib"] / "Game.iso"))

    assert result["matched"] is True
    assert result["dat_name"] == _PSP
    assert result["game_name"] == "Some Game"


@pytest.mark.asyncio
async def test_container_only_never_skips_the_recognized_proof(e2e):
    src, _sha1 = _pbp(e2e["lib"])

    job = await _run(src, "pbp_to_iso", delete_on_verify=True, container_only=True)

    assert job.status.value == "completed", job.error_message
    assert src.exists()
    assert job.warning == "converted, source kept: output not Recognized"


@pytest.mark.asyncio
async def test_cia_to_cci_end_to_end_keeps_an_unrecognized_source(e2e):
    src = e2e["lib"] / "Game.cia"
    src.write_bytes(fx.cia_bytes(payload=b"homebrew"))

    job = await _run(src, "cia_to_cci", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert src.exists() and (e2e["lib"] / "Game.cci").exists()
    assert job.warning == "converted, source kept: output not Recognized"


@pytest.mark.asyncio
async def test_cci_to_cia_end_to_end_verifies_and_keeps_the_source(e2e):
    src = e2e["lib"] / "Game.cci"
    src.write_bytes(fx.cci_bytes(payload=b"homebrew"))

    from app.models import ConversionMode

    # verify_after is a Rule's and the CLI's switch, not the route's.
    manager = convert_routes.job_manager
    job = await manager.create_job(
        str(src), ConversionMode.CCI_TO_CIA,
        output_path=str(e2e["lib"] / "Game.cia"), verify_after=True,
    )
    await manager._process_job(job.id)

    assert job.status.value == "completed", job.error_message
    assert src.exists() and (e2e["lib"] / "Game.cia").exists()
    assert job.check_passed == "intact"
    assert job.warning is None


# --------------------------------------------------------------------------- #
# Rules: the check a Rule's Preview promises
# --------------------------------------------------------------------------- #


def test_a_rule_promises_the_recognized_check():
    from app.services.rules.rules import effective_check_level, normalize_rule

    rule = normalize_rule({
        "enabled": True, "mode": "pbp_to_iso", "delete_on_verify": True,
        "check_level": "container_only",
    })
    assert rule is not None and rule["delete_on_verify"] is True
    # The container-only opt-out is a Round-trip opt-out; it can't skip this.
    assert effective_check_level(rule) == "recognized"
    assert effective_check_level({**rule, "delete_on_verify": False}) is None


def test_the_delete_check_is_one_answer_for_every_caller():
    from app.services.round_trip import delete_check_level

    assert delete_check_level("cia_to_cci", delete_on_verify=True, container_only=False) == (
        "recognized"
    )
    assert delete_check_level("cia_to_cci", delete_on_verify=False, container_only=False) is None
    assert delete_check_level("dolphin_rvz", delete_on_verify=True, container_only=False) == (
        "round_trip"
    )
    assert delete_check_level("dolphin_rvz", delete_on_verify=True, container_only=True) == (
        "container_only"
    )
