"""rar_extract: a multi-part RAR set to the one verified file inside it (#476).

The sets are rarfile's own test archives (``tests/fixtures/rar``), copied into
``tmp_path`` and, where a test needs a payload, with the big member renamed
(``tests/rar_fixtures.py``). Planning reads the headers in Python, so every
refusal runs anywhere. The tests that run ``unar`` itself skip unless it is
installed (it is in the Docker image).
"""
from __future__ import annotations

import os
import shutil
import zlib
from pathlib import Path

import pytest

from .rar_fixtures import (
    BIG,
    BIG_CRC,
    BIG_SIZE,
    FIXTURES,
    RAR4_SET,
    RAR5_SET,
    SMALL,
    copy_set,
    payload_set,
    rename_member,
)

SETS = pytest.mark.parametrize("names", [RAR5_SET, RAR4_SET], ids=["rar5-partN", "rar4-rNN"])
PAYLOAD = "vols/bigfile.gba"


@pytest.fixture(autouse=True)
def _fresh_listing_cache():
    from services import archive_members

    archive_members.clear_cache()
    yield
    archive_members.clear_cache()


@pytest.fixture(autouse=True)
def _tmp_path_is_the_volume(tmp_path, tmp_path_factory, monkeypatch):
    """``tmp_path`` is the configured Volume (the extraction's work folder is
    a staging slot, made only inside one); the journal lives elsewhere."""
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path_factory.mktemp("data")))


def _tool():
    from services.tools import registry

    return registry.get("rar")


def _service():
    from services.rar_extract import rar_service

    return rar_service


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# --------------------------------------------------------------------------- #
# The set: which files are its volumes
# --------------------------------------------------------------------------- #


@SETS
def test_the_first_volume_names_every_later_volume(tmp_path, names):
    from services.archive_members import later_volume_of, rar_later_volumes

    volumes = [str(v) for v in copy_set(names, tmp_path)]

    assert rar_later_volumes(volumes[0]) == volumes[1:]
    assert later_volume_of(volumes[0]) is None
    for later in volumes[1:]:
        assert rar_later_volumes(later) == []
        assert later_volume_of(later) == volumes[0]


def test_an_r00_with_no_rar_beside_it_is_no_later_volume(tmp_path):
    from services.archive_members import later_volume_of

    stray = tmp_path / "Game.r00"
    stray.write_bytes(b"not a volume")

    assert later_volume_of(str(stray)) is None


def test_the_later_volumes_stop_at_the_first_gap(tmp_path):
    from services.archive_members import rar_later_volumes

    volumes = copy_set(RAR4_SET, tmp_path)
    volumes[1].unlink()  # .r00

    assert rar_later_volumes(str(volumes[0])) == []


# A file named like a volume is a companion only when the set's headers read
# it (review of #503): an unrelated file must never be planned for deletion.


@pytest.mark.parametrize(("names", "stray"), [
    (RAR5_SET, "rar5-vols.part4.rar"), (RAR4_SET, "rar3-old.r02"),
], ids=["rar5-part4", "rar4-r02"])
def test_a_stray_volume_name_is_no_companion_and_refuses_the_set(tmp_path, names, stray):
    from services.rar_extract import RarSetRefused
    from services.tools import registry
    from utils.delete_plan import build_delete_plan

    volumes = [str(v) for v in payload_set(names, tmp_path)]
    (tmp_path / stray).write_bytes(b"an unrelated file, no RAR")

    assert registry.source_companions(volumes[0]) == volumes[1:]
    assert sorted(build_delete_plan(volumes[0])["delete_paths"]) == sorted(volumes)
    with pytest.raises(RarSetRefused, match=stray.replace(".", r"\.")):
        _service().plan(volumes[0])


@pytest.mark.parametrize("confine", [False, True])
def test_an_r00_beside_a_single_volume_rar_is_never_deleted_with_it(tmp_path, confine):
    from services.tools import registry
    from utils.delete_plan import build_delete_plan

    (single,) = copy_set(("rar5-dups.rar",), tmp_path)
    single = single.rename(tmp_path / "Game.rar")
    (tmp_path / "Game.r00").write_bytes(b"someone else's file")

    assert registry.source_companions(str(single)) == []
    # The generic delete plan, and the one the duplicate cleanup builds.
    plan = build_delete_plan(str(single), confine_to_volumes=confine)
    assert plan["delete_paths"] == [str(single)]


def test_a_rom_is_never_read_as_a_volume(tmp_path):
    from services.archive_members import is_rar_volume_name, later_volume_of, rar_later_volumes
    from services.tools import registry

    (single,) = copy_set(("rar5-dups.rar",), tmp_path)
    single = single.rename(tmp_path / "Game.rar")
    assert is_rar_volume_name("Game.rar") and is_rar_volume_name("Game.r00")
    assert is_rar_volume_name("Game.S12") and is_rar_volume_name("Game.part02.rar")
    for ext in sorted(set(registry.convertible_extensions()) - {".rar"}):
        assert not is_rar_volume_name(f"Game{ext}"), ext  # .v64, .z64, .t64...
        rom = tmp_path / f"Game{ext}"
        rom.write_bytes(b"rom")
        assert later_volume_of(str(rom)) is None, ext
    assert rar_later_volumes(str(single)) == []
    assert registry.source_companions(str(single)) == []


def test_a_payload_that_is_itself_a_rar_volume_is_refused(tmp_path):
    from services.rar_extract import RarSetRefused

    volumes = payload_set(RAR5_SET, tmp_path, as_name="vols/bigfile.rar")

    with pytest.raises(RarSetRefused, match=r"another RAR.*bigfile\.rar"):
        _service().plan(str(volumes[0]))


@pytest.mark.asyncio
async def test_without_a_crc_the_round_trip_keeps_the_source(tmp_path, monkeypatch):
    from services import rar_extract
    from services.tools.base import ContentDigestUnavailable

    volumes = payload_set(RAR5_SET, tmp_path)
    real = rar_extract.read_rar_set

    def blake2_only(path):
        found = real(path)
        return found._replace(members=[m._replace(crc=None) for m in found.members])

    monkeypatch.setattr(rar_extract, "read_rar_set", blake2_only)

    with pytest.raises(ContentDigestUnavailable, match="CRC32"):
        await _tool().content_digest(str(volumes[0]))


# --------------------------------------------------------------------------- #
# Junk: release sidecars are never the payload, and stay listed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("name", "sidecar"), [
    ("Game.nfo", True), ("sub/Game.SFV", True), ("readme.txt", True),
    ("Download here.url", True), ("__MACOSX/._Game.gba", True), (".DS_Store", True),
    ("Game.gba", False), ("Game.bin", False), ("nfo", False),
])
def test_release_sidecars_are_never_a_payload(name, sidecar):
    from utils.junk import is_archive_sidecar

    assert is_archive_sidecar(name) is sidecar


def test_a_sidecar_still_lists_in_the_browser():
    from utils.junk import is_junk_entry

    assert not is_junk_entry("readme.txt")
    assert not is_junk_entry("Game.nfo")


# --------------------------------------------------------------------------- #
# The plan: exactly one payload, nothing unsafe, before anything is written
# --------------------------------------------------------------------------- #


@SETS
def test_the_plan_finds_the_one_payload(tmp_path, names):
    volumes = payload_set(names, tmp_path)

    plan = _service().plan(str(volumes[0]))

    assert plan.payload == PAYLOAD
    assert (plan.size, plan.crc) == (BIG_SIZE, BIG_CRC)
    assert plan.unpacked == BIG_SIZE + 2050
    assert list(plan.volumes) == [str(v) for v in volumes]


def test_two_payloads_are_refused_by_name(tmp_path):
    from services.rar_extract import RarSetRefused

    volumes = payload_set(RAR5_SET, tmp_path)
    rename_member(volumes, SMALL, "vols/smallfile.bin")

    with pytest.raises(RarSetRefused, match=r"2 files.*bigfile\.gba.*smallfile\.bin"):
        _service().plan(str(volumes[0]))


def test_a_set_of_sidecars_alone_is_refused_by_name(tmp_path):
    from services.rar_extract import RarSetRefused

    volumes = copy_set(RAR5_SET, tmp_path)

    with pytest.raises(RarSetRefused, match=r"no file to extract.*bigfile\.txt"):
        _service().plan(str(volumes[0]))


@pytest.mark.parametrize("fixture", ["rar5-psw.rar", "rar5-hpsw.rar"])
def test_an_encrypted_set_is_refused(tmp_path, fixture):
    from services.rar_extract import RarSetRefused

    (first,) = copy_set((fixture,), tmp_path)

    with pytest.raises(RarSetRefused, match="encrypted"):
        _service().plan(str(first))


@pytest.mark.parametrize("bad", ["../s/bigfile.gba", "/vols/bigfil.gba"])
def test_an_absolute_or_dotdot_member_is_refused(tmp_path, bad):
    from services.rar_extract import RarSetRefused

    volumes = payload_set(RAR5_SET, tmp_path, as_name=bad)

    with pytest.raises(RarSetRefused, match="unsafe"):
        _service().plan(str(volumes[0]))


@pytest.mark.parametrize("fixture", ["rar5-evil-symlink-traversal.rar", "rar5-dups.rar"])
def test_a_link_member_is_refused(tmp_path, fixture):
    from services.rar_extract import RarSetRefused

    (first,) = copy_set((fixture,), tmp_path)

    with pytest.raises(RarSetRefused, match="link"):
        _service().plan(str(first))


@SETS
def test_a_missing_middle_volume_is_refused(tmp_path, names):
    from services.rar_extract import RarSetRefused

    volumes = payload_set(names, tmp_path)
    volumes[1].unlink()

    with pytest.raises(RarSetRefused, match="volume"):
        _service().plan(str(volumes[0]))


def test_a_later_volume_is_refused_naming_the_first(tmp_path):
    from services.rar_extract import RarSetRefused

    volumes = payload_set(RAR5_SET, tmp_path)

    with pytest.raises(RarSetRefused, match="rar5-vols.part1.rar"):
        _service().plan(str(volumes[1]))


# --------------------------------------------------------------------------- #
# The plugin: one Source of several files
# --------------------------------------------------------------------------- #


def test_the_mode_is_registered_on_its_own_tool():
    from services.tools import registry
    from services.tools.spec import DeleteProof, ModeKind

    spec = registry.spec("rar_extract")
    assert registry.for_mode("rar_extract").id == "rar"
    assert spec.kind == ModeKind.EXTRACT
    assert spec.input_extensions == frozenset({".rar"})
    assert spec.supports_delete_on_verify and spec.supports_round_trip
    assert spec.delete_proof == DeleteProof.ROUND_TRIP
    assert not spec.allows_archive_input


@SETS
def test_every_later_volume_is_a_companion_and_in_the_delete_plan(tmp_path, names):
    from services.tools import registry
    from utils.delete_plan import build_delete_plan

    volumes = [str(v) for v in payload_set(names, tmp_path)]

    assert registry.source_companions(volumes[0]) == volumes[1:]
    plan = build_delete_plan(volumes[0])
    assert sorted(plan["delete_paths"]) == sorted(volumes)
    assert not plan["errors"] and not plan["unsafe_paths"]


@SETS
def test_only_the_first_volume_converts(tmp_path, names):
    volumes = payload_set(names, tmp_path)
    tool = _tool()

    assert tool.converts_path(str(volumes[0]))
    assert not any(tool.converts_path(str(v)) for v in volumes[1:])


@pytest.mark.asyncio
async def test_the_listing_shows_the_set_once(tmp_path, monkeypatch):
    from routes import files as files_routes

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    folder = tmp_path / "usenet"
    payload_set(RAR5_SET, folder)

    listing = await files_routes.list_files(path=str(folder))

    archives = [e for e in listing.entries if e.type == "archive"]
    assert [e.name for e in archives] == ["rar5-vols.part1.rar"]
    for entry in listing.entries:
        if entry.type != "archive":
            assert "rar" not in entry.convertible_by


@SETS
def test_the_output_is_the_members_own_name_beside_the_set(tmp_path, names):
    volumes = payload_set(names, tmp_path)
    tool = _tool()

    assert tool.output_path("rar_extract", str(volumes[0])) == str(tmp_path / "bigfile.gba")
    assert tool.expected_output_size(str(volumes[0]), "rar_extract") == BIG_SIZE + 2050


@pytest.mark.asyncio
async def test_a_missing_unar_hides_the_rar_tool_only(monkeypatch, tmp_path):
    from services.tools import binaries, registry

    rar = registry.get("rar")
    monkeypatch.setattr(rar._service, "unar_path", str(tmp_path / "no-unar"))
    binaries.forget_binary_probes()

    assert not await registry.tool_is_ready(rar)
    assert "unar" in rar.not_ready_reason
    assert await registry.tool_is_ready(registry.get("romz"))


@pytest.mark.asyncio
async def test_plan_job_says_what_it_found(tmp_path, monkeypatch):
    from routes import convert as convert_routes

    from app.models import DuplicateAction

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    volumes = payload_set(RAR5_SET, tmp_path)
    rename_member(volumes, SMALL, "vols/smallfile.bin")

    with pytest.raises(convert_routes.SkipFile) as caught:
        await convert_routes.plan_job(
            str(volumes[0]), spec=convert_routes.registry.spec("rar_extract"),
            mode="rar_extract", output_dir=None,
            duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
        )
    assert "smallfile.bin" in caught.value.detail
    assert _names(tmp_path) == sorted(v.name for v in volumes)


def test_the_cli_route_says_what_it_found(tmp_path):
    from services.platform_detect import format_route
    from services.tools import registry

    volumes = copy_set(RAR5_SET, tmp_path)

    mode, reason = format_route(str(volumes[0]), "rar_extract", None, registry)

    assert mode is None
    assert "bigfile.txt" in reason


# --------------------------------------------------------------------------- #
# The checks: a file read in full, and its content against the set's headers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_set_and_its_file_have_the_same_digest(tmp_path):
    volumes = payload_set(RAR5_SET, tmp_path / "set")
    loose = tmp_path / "bigfile.gba"
    with __import__("rarfile").RarFile(str(volumes[0])) as rf:
        loose.write_bytes(rf.read(PAYLOAD))
    tool = _tool()

    source = await tool.content_digest(str(volumes[0]))
    output = await tool.content_digest(str(loose))

    assert source.same_content(output)
    loose.write_bytes(b"x" * BIG_SIZE)
    assert not source.same_content(await tool.content_digest(str(loose)))


@pytest.mark.asyncio
async def test_the_container_check_reads_the_file_in_full(tmp_path):
    loose = tmp_path / "bigfile.gba"
    loose.write_bytes(b"rom" * 1000)

    result = await _tool().verify(str(loose))

    assert result["valid"], result


def test_the_container_only_opt_out_never_skips_the_round_trip():
    from services.round_trip import delete_check_level, round_trip_applies

    # The extracted file has no container, so reading it in full proves
    # nothing about its content: the Round-trip is the proof a delete rests on.
    assert round_trip_applies("rar_extract", delete_on_verify=True, container_only=True)
    assert delete_check_level(
        "rar_extract", delete_on_verify=True, container_only=True,
    ) == "round_trip"
    # Every other Round-trip mode keeps its opt-out.
    assert not round_trip_applies("romz_7z", delete_on_verify=True, container_only=True)


# --------------------------------------------------------------------------- #
# A delete-source job: every volume goes after a pass, none after a failure
# --------------------------------------------------------------------------- #


@pytest.fixture(name="jobs")
def _jobs(tmp_path, monkeypatch, db_path, reset_db_engine):
    """The route confined to ``tmp_path``; the extraction itself stubbed to
    write what the test says (rarfile reads the stored payload), so the job's
    own checks and delete step are what run."""
    import rarfile
    from services import db as _db

    from app.routes import convert as convert_routes

    _db.init_engine(db_path)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    state = {"bytes": None}

    async def fake_convert(input_path, output_path, mode="", *, cancel_event=None):
        with rarfile.RarFile(input_path) as rf:
            data = rf.read(PAYLOAD)
        Path(output_path).write_bytes(state["bytes"] or data)
        yield {"progress": 100, "message": "done"}

    monkeypatch.setattr(_tool()._service, "convert", fake_convert)
    yield state, convert_routes
    reset_db_engine()


async def _run_delete_job(convert_routes, first: Path, *, container_only: bool = False):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    request = JobCreateRequest(
        file_path=str(first), mode=ConversionMode("rar_extract"),
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=container_only,
    )
    job = await convert_routes.create_job(request)
    await convert_routes.job_manager._process_job(job.id)
    return job


@SETS
@pytest.mark.asyncio
async def test_a_passing_check_deletes_every_volume(tmp_path, jobs, names):
    _state, convert_routes = jobs
    folder = tmp_path / "set"
    volumes = payload_set(names, folder)

    job = await _run_delete_job(convert_routes, volumes[0])

    assert job.status.value == "completed", job.error_message
    assert job.check_passed == "round_tripped"
    assert _names(folder) == ["bigfile.gba"]
    assert zlib.crc32((folder / "bigfile.gba").read_bytes()) == BIG_CRC


@pytest.mark.parametrize("container_only", [False, True])
@pytest.mark.asyncio
async def test_a_failing_check_deletes_no_volume(tmp_path, jobs, container_only):
    state, convert_routes = jobs
    folder = tmp_path / "set"
    volumes = payload_set(RAR5_SET, folder)
    state["bytes"] = b"\0" * BIG_SIZE  # reads in full, holds other content

    job = await _run_delete_job(convert_routes, volumes[0], container_only=container_only)

    assert job.status.value == "failed"
    assert "Source kept" in job.error_message
    assert all(v.exists() for v in volumes)


# --------------------------------------------------------------------------- #
# The real extractor (in the Docker image; skipped without unar)
# --------------------------------------------------------------------------- #


def _unar_missing() -> bool:
    from config import settings

    return shutil.which(getattr(settings, "unar_path", "unar")) is None


real = pytest.mark.skipif(_unar_missing(), reason="unar is not installed")


async def _extract(first: Path) -> Path:
    tool = _tool()
    output = Path(tool.output_path("rar_extract", str(first)))
    async for _ in tool.convert(str(first), str(output), "rar_extract"):
        pass
    return output


@real
@SETS
@pytest.mark.asyncio
async def test_unar_extracts_the_one_file_intact(tmp_path, names):
    from services.round_trip import round_trip

    volumes = payload_set(names, tmp_path)
    before = {v.name: v.read_bytes() for v in volumes}

    output = await _extract(volumes[0])

    data = output.read_bytes()
    assert (len(data), zlib.crc32(data)) == (BIG_SIZE, BIG_CRC)
    assert _names(tmp_path) == sorted([*before, "bigfile.gba"])  # no work folder, no sidecar
    assert {v.name: v.read_bytes() for v in volumes} == before
    assert (await _tool().verify(str(output)))["valid"]
    assert (await round_trip(_tool(), str(volumes[0]), str(output))).passed


@real
@SETS
@pytest.mark.asyncio
async def test_a_crc_error_fails_and_leaves_nothing(tmp_path, names):
    volumes = payload_set(names, tmp_path)
    middle = bytearray(volumes[1].read_bytes())
    middle[50000:50004] = b"\0\0\0\0"  # stored data, not a header
    volumes[1].write_bytes(bytes(middle))

    with pytest.raises(Exception, match="(?i)crc|checksum|unar"):
        await _extract(volumes[0])

    assert _names(tmp_path) == sorted(v.name for v in volumes)


@real
@pytest.mark.asyncio
async def test_a_taken_name_fails_and_replaces_nothing(tmp_path):
    volumes = payload_set(RAR5_SET, tmp_path)
    taken = tmp_path / "bigfile.gba"
    tool = _tool()
    output = tool.output_path("rar_extract", str(volumes[0]))
    taken.write_bytes(b"mine")

    with pytest.raises(RuntimeError, match="not overwriting"):
        async for _ in tool.convert(str(volumes[0]), output, "rar_extract"):
            pass

    assert taken.read_bytes() == b"mine"
    assert _names(tmp_path) == sorted([*(v.name for v in volumes), "bigfile.gba"])


@pytest.mark.asyncio
async def test_a_refused_set_writes_nothing_and_never_runs_unar(tmp_path, monkeypatch):
    volumes = payload_set(RAR5_SET, tmp_path)
    rename_member(volumes, SMALL, "vols/smallfile.bin")
    tool = _tool()
    spawned = []

    async def no_run(*args, **kwargs):
        spawned.append(args)
        yield {}

    monkeypatch.setattr(tool._service._runner, "run", no_run)

    with pytest.raises(ValueError, match="smallfile.bin"):
        async for _ in tool.convert(str(volumes[0]), str(tmp_path / "x.gba"), "rar_extract"):
            pass

    assert not spawned
    assert _names(tmp_path) == sorted(v.name for v in volumes)


@pytest.mark.asyncio
async def test_too_little_free_space_is_refused_before_unar_runs(tmp_path, monkeypatch):
    import collections

    from services import disk

    volumes = payload_set(RAR5_SET, tmp_path)
    tool = _tool()
    spawned = []

    async def no_run(*args, **kwargs):
        spawned.append(args)
        yield {}

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(tool._service._runner, "run", no_run)
    # Room for the payload alone, not for every file the set unpacks.
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda _p: usage(0, 0, BIG_SIZE))

    with pytest.raises(disk.InsufficientDiskSpace):
        async for _ in tool.convert(str(volumes[0]), str(tmp_path / "x.gba"), "rar_extract"):
            pass

    assert not spawned
    assert _names(tmp_path) == sorted(v.name for v in volumes)


@pytest.mark.asyncio
async def test_the_work_folder_is_a_journaled_staging_slot(tmp_path, monkeypatch):
    from services import partial_writes

    volumes = payload_set(RAR5_SET, tmp_path)
    tool = _tool()
    seen = {}

    async def fake_run(cmd, **kwargs):
        work = cmd[cmd.index("-o") + 1]
        seen["work"] = work
        seen["recorded"] = work in partial_writes.recorded_paths()
        yield {"progress": 50}

    monkeypatch.setattr(tool._service._runner, "run", fake_run)

    with pytest.raises(RuntimeError, match="without writing"):
        async for _ in tool.convert(str(volumes[0]), str(tmp_path / "x.gba"), "rar_extract"):
            pass

    assert "/.compressatorium-stage/slot-" in seen["work"]
    assert seen["recorded"]  # the startup sweep finds it after a crash
    assert not os.path.exists(seen["work"])
    assert seen["work"] not in partial_writes.recorded_paths()
    assert _names(tmp_path) == sorted(v.name for v in volumes)


def test_the_fixtures_are_the_checked_in_ones():
    assert all((FIXTURES / name).is_file() for name in (*RAR5_SET, *RAR4_SET))
    assert (FIXTURES / "LICENSE.rarfile").is_file()
    assert BIG == "vols/bigfile.txt" and os.path.basename(PAYLOAD) == "bigfile.gba"


# --------------------------------------------------------------------------- #
# The output may never be the Source or one of its companions (any tool)
# --------------------------------------------------------------------------- #


async def _plan(convert_routes, path: str, mode: str, action):
    return await convert_routes.plan_job(
        path, spec=convert_routes.registry.spec(mode), mode=mode, output_dir=None,
        duplicate_action=action, delete_on_verify=False,
    )


@pytest.mark.parametrize(("action", "reason"), [
    ("overwrite", "OUTPUT_IS_PART_OF_SOURCE"), ("skip", "OUTPUT_EXISTS"), ("rename", None),
])
@pytest.mark.asyncio
async def test_an_output_named_like_a_later_volume_is_refused(
    tmp_path, monkeypatch, action, reason,
):
    from routes import convert as convert_routes
    from services.tools import registry

    from app.models import DuplicateAction

    volumes = payload_set(RAR5_SET, tmp_path)
    monkeypatch.setattr(
        registry.get("rar"), "output_path", lambda *_a, **_k: str(volumes[2]),
    )

    if reason is None:
        # A rename picks a free name, which is never one of the volumes.
        plan = await _plan(convert_routes, str(volumes[0]), "rar_extract", DuplicateAction(action))
        assert plan.output_path not in {str(v) for v in volumes}
        return
    with pytest.raises(convert_routes.SkipFile) as caught:
        await _plan(convert_routes, str(volumes[0]), "rar_extract", DuplicateAction(action))
    assert caught.value.reason == getattr(convert_routes.SkipReason, reason)


@pytest.mark.asyncio
async def test_an_output_named_like_a_split_dump_part_is_refused(tmp_path, monkeypatch):
    from routes import convert as convert_routes
    from services.tools import registry

    from app.models import DuplicateAction

    first = tmp_path / "game_part1.wud"
    second = tmp_path / "game_part2.wud"
    first.write_bytes(b"\0" * 64)
    second.write_bytes(b"\0" * 64)
    assert registry.source_companions(str(first)) == [str(second)]
    monkeypatch.setattr(
        registry.get("jwud"), "output_path", lambda *_a, **_k: str(second),
    )

    with pytest.raises(convert_routes.SkipFile) as caught:
        await _plan(convert_routes, str(first), "jwud_compress", DuplicateAction.OVERWRITE)
    assert caught.value.reason == convert_routes.SkipReason.OUTPUT_IS_PART_OF_SOURCE
    assert second.read_bytes() == b"\0" * 64


# --------------------------------------------------------------------------- #
# Re-review of #503: the guard sits on the final output, in the one place
# every job creator goes through; header reads are bounded and cached.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("action", "outcome"), [
    ("rename", "Game_1.rvz"),
    ("skip", "OUTPUT_EXISTS"),
    ("overwrite", "DOLPHIN_SAME_PATH"),
])
@pytest.mark.asyncio
async def test_a_same_format_reencode_keeps_its_old_answers(tmp_path, action, outcome):
    from routes import convert as convert_routes

    from app.models import DuplicateAction

    rvz = tmp_path / "Game.rvz"
    rvz.write_bytes(b"\0" * 64)

    if outcome.endswith(".rvz"):
        plan = await _plan(convert_routes, str(rvz), "dolphin_rvz", DuplicateAction(action))
        assert os.path.basename(plan.output_path) == outcome
        return
    with pytest.raises(convert_routes.SkipFile) as caught:
        await _plan(convert_routes, str(rvz), "dolphin_rvz", DuplicateAction(action))
    assert caught.value.reason == getattr(convert_routes.SkipReason, outcome)


@pytest.mark.asyncio
async def test_the_job_manager_refuses_an_output_that_is_part_of_the_source(
    tmp_path, db_path, reset_db_engine,
):
    from services import db as _db
    from services.job_manager import OutputIsSourceError, job_manager

    from app.models import ConversionMode

    _db.init_engine(db_path)
    volumes = payload_set(RAR5_SET, tmp_path)
    before = len(job_manager.jobs)

    with pytest.raises(OutputIsSourceError):
        await job_manager.create_batch_jobs(
            [str(volumes[0])], ConversionMode("rar_extract"),
            output_paths={str(volumes[0]): str(volumes[2])}, allow_overwrite=True,
        )
    assert len(job_manager.jobs) == before
    reset_db_engine()


@pytest.mark.asyncio
async def test_a_rule_sweep_never_queues_an_output_onto_its_source(
    tmp_path, monkeypatch, db_path, reset_db_engine,
):
    from unittest.mock import patch

    from services import db as _db
    from services.job_manager import job_manager
    from services.rules import sweep as rules_sweep
    from services.tools import registry

    from .test_rules_library_296 import _FolderLike

    _db.init_engine(db_path, create_schema=True)
    root = tmp_path / "lib"
    volumes = payload_set(RAR5_SET, root / "gc")
    monkeypatch.setattr(
        registry.get("rar"), "output_path", lambda *_a, **_k: str(volumes[2]),
    )
    from services.rules.library import CONVERTED_HISTORY

    class _WithHistory(_FolderLike):
        # Overwrite needs the converted history (rules.duplicate_actions).
        capabilities = frozenset({CONVERTED_HISTORY})
        name = "Downloads"

    folder = _WithHistory(root)
    saved = await rules_sweep.set_rules(folder, {
        "gc": {"mode": "rar_extract", "enabled": True, "duplicate_action": "overwrite"},
    })
    assert saved["gc"]["duplicate_action"] == "overwrite"
    before = set(job_manager.jobs)

    with patch.object(job_manager, "get_active_job_candidates", return_value=[]):
        await rules_sweep.sweep(folder, ignore_schedule=True)

    queued = [j for i, j in job_manager.jobs.items() if i not in before]
    assert not [j for j in queued if j.output_path == str(volumes[2])]
    assert all(v.exists() for v in volumes)
    reset_db_engine()


def test_an_unreadable_set_is_read_once_per_version(tmp_path, monkeypatch):
    from services import archive_members

    volumes = payload_set(RAR5_SET, tmp_path)
    calls = []

    def broken(path):
        calls.append(path)
        raise archive_members.IncompleteArchive("a volume is missing")

    monkeypatch.setattr(archive_members, "read_rar_set", broken)

    for _ in range(3):
        with pytest.raises(archive_members.RarVolumesUnknown):
            archive_members.rar_set_volumes(str(volumes[0]))
    assert len(calls) == 1


def test_a_header_read_that_hangs_lists_no_companions_and_blocks_deletes(
    tmp_path, monkeypatch,
):
    import threading

    from services import archive_members
    from services.tools import registry
    from utils.delete_plan import build_delete_plan

    volumes = payload_set(RAR5_SET, tmp_path)
    release = threading.Event()

    def hangs(path):
        release.wait(5)
        raise archive_members.IncompleteArchive("never answered")

    monkeypatch.setattr(archive_members, "read_rar_set", hangs)
    monkeypatch.setattr(archive_members, "RAR_HEADER_READ_SECONDS", 0.05)
    try:
        # A listing: no companions, the volumes stay listed.
        assert registry.source_companions(str(volumes[0])) == []
        # Anything that deletes: refused.
        plan = build_delete_plan(str(volumes[0]))
        assert plan["errors"], plan
    finally:
        release.set()


@pytest.mark.asyncio
async def test_the_delete_plan_route_is_bounded(tmp_path, monkeypatch):
    import threading

    from fastapi import HTTPException
    from routes import convert as convert_routes
    from routes import volume_guard

    from app.models import ConversionMode, DeletePlanRequest

    volumes = payload_set(RAR5_SET, tmp_path)
    release = threading.Event()

    def hangs(*_a, **_k):
        release.wait(5)
        return {}

    monkeypatch.setattr(convert_routes, "build_delete_plan", hangs)
    monkeypatch.setattr(volume_guard, "VOLUME_PROBE_TIMEOUT_SECONDS", 0.1)
    try:
        with pytest.raises(HTTPException) as caught:
            await convert_routes.delete_plan(DeletePlanRequest(
                file_paths=[str(volumes[0])], mode=ConversionMode("rar_extract"),
            ))
        assert caught.value.status_code == 503
    finally:
        release.set()
