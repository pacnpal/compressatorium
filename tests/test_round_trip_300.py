"""Round-trip check for chdman, DVD and CD (#300).

A ``createdvd`` / ``createcd`` job with delete-source on deletes its Source
only when the image the CHD decodes to equals the Source image. chdman is faked
at the ``SubprocessRunner.run_capture`` seam (``info`` and ``extractcd``), so
these tests need no real binary; the Source side is a real SHA1 of real temp
files.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from services.tools import registry
from services.tools.base import ContentDigest, ContentDigestUnavailable

KIND = "image_data_sha1"
OVERALL_SHA1 = "11" * 20


def _chdman():
    return registry.get("chdman")


def _sha1(*chunks: bytes) -> str:
    h = hashlib.sha1()
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()


def _info_stdout(*, tag: str, data_sha1: str | None, logical: int = 4096) -> bytes:
    """``chdman info`` output, in the shape chdman 0.285 prints."""
    lines = [
        "chdman - MAME Compressed Hunks of Data (CHD) manager 0.285 (mame0285)",
        "Input file:   Game.chd",
        "File Version: 5",
        f"Logical size: {logical:,} bytes",
        "Hunk Size:    2,048 bytes",
        "Compression:  zlib (Deflate)",
        f"SHA1:         {OVERALL_SHA1}",
    ]
    if data_sha1 is not None:
        lines.append(f"Data SHA1:    {data_sha1}")
    lines.append(f"Metadata:     Tag='{tag}'  Index=0  Length=91 bytes")
    lines.append("              TRACK:1 TYPE:MODE2_RAW SUBTYPE:NONE FRAMES:2")
    return ("\n".join(lines) + "\n").encode()


class FakeChdman:
    """Answers ``chdman info`` and ``chdman extractcd`` like the real binary.

    ``extract_files`` maps a file name to its bytes; ``extractcd`` writes a cue
    naming them (in that order) plus the files, into the ``-o`` directory.
    """

    def __init__(self) -> None:
        self.info = b""
        self.info_rc = 0
        self.extract_rc = 0
        self.extract_files: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.on_extract = None

    async def run_capture(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if "info" in cmd:
            return self.info_rc, self.info, b""
        if "extractcd" in cmd:
            sheet = Path(cmd[cmd.index("-o") + 1])
            for name, data in self.extract_files.items():
                (sheet.parent / name).write_bytes(data)
            if sheet.suffix == ".gdi":
                sheet.write_text(f"{len(self.extract_files)}\n" + "".join(
                    f'{i} 0 4 2352 "{name}" 0\n'
                    for i, name in enumerate(self.extract_files, start=1)
                ))
            else:
                sheet.write_text("".join(
                    f'FILE "{name}" BINARY\n  TRACK {i:02d} MODE2/2352\n'
                    "    INDEX 01 00:00:00\n"
                    for i, name in enumerate(self.extract_files, start=1)
                ))
            if self.on_extract is not None:
                return self.on_extract(sheet)
            return self.extract_rc, b"", b""
        raise AssertionError(f"unexpected chdman call {cmd}")

    def commands(self) -> list[str]:
        return [c for call in self.calls for c in call if c in ("info", "extractcd")]


@pytest.fixture(name="fake")
def _fake(monkeypatch, tmp_path):
    from config import settings

    fake = FakeChdman()
    monkeypatch.setattr(_chdman()._service._runner, "run_capture", fake.run_capture)
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "scratch"))
    return fake


def _scratch_left(tmp_path) -> list[str]:
    scratch = tmp_path / "scratch"
    return os.listdir(scratch) if scratch.exists() else []


# ---------------------------------------------------------------------------
# Source side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iso_source_digest_is_the_file_sha1_without_spawning(tmp_path, fake):
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"dvd sectors" * 1000)

    digest = await _chdman().content_digest(str(iso))

    assert digest == ContentDigest(kind=KIND, value=_sha1(iso.read_bytes()))
    assert fake.calls == []


@pytest.mark.asyncio
async def test_cue_source_digest_hashes_its_track_files_in_sheet_order(tmp_path, fake):
    (tmp_path / "Game (Track 1).bin").write_bytes(b"data track")
    (tmp_path / "Game (Track 2).bin").write_bytes(b"audio track")
    cue = tmp_path / "Game.cue"
    cue.write_text(
        'FILE "Game (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n'
        "    INDEX 01 00:00:00\n"
        'FILE "Game (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n'
        "    INDEX 00 00:00:00\n    INDEX 01 00:02:00\n"
    )

    digest = await _chdman().content_digest(str(cue))

    assert digest == ContentDigest(kind=KIND, value=_sha1(b"data track", b"audio track"))
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_cue_with_a_missing_track_file_is_unavailable(tmp_path, fake):
    cue = tmp_path / "Game.cue"
    cue.write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    with pytest.raises(ContentDigestUnavailable):
        await _chdman().content_digest(str(cue))


@pytest.mark.asyncio
async def test_a_cue_naming_no_track_file_is_unavailable(tmp_path, fake):
    cue = tmp_path / "Game.cue"
    cue.write_text("REM nothing here\n")

    with pytest.raises(ContentDigestUnavailable):
        await _chdman().content_digest(str(cue))


# ---------------------------------------------------------------------------
# Output side: DVD and HD CHDs read the data SHA1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["DVD ", "GDDD"])
async def test_dvd_and_hd_chd_digest_is_the_data_sha1_never_the_overall_sha1(
    tmp_path, fake, tag,
):
    data_sha1 = "ab" * 20
    fake.info = _info_stdout(tag=tag, data_sha1=data_sha1.upper())
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    digest = await _chdman().content_digest(str(chd))

    assert digest == ContentDigest(kind=KIND, value=data_sha1)
    assert digest.value != OVERALL_SHA1
    assert fake.commands() == ["info"]


@pytest.mark.asyncio
async def test_a_dvd_chd_without_a_data_sha1_is_unavailable(tmp_path, fake):
    # The overall SHA1 alone must never stand in for the content digest.
    fake.info = _info_stdout(tag="DVD ", data_sha1=None)
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _chdman().content_digest(str(chd))


@pytest.mark.asyncio
async def test_a_chd_whose_info_fails_is_unavailable(tmp_path, fake):
    fake.info_rc = 1
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _chdman().content_digest(str(chd))


@pytest.mark.asyncio
async def test_a_chd_of_unknown_media_is_unavailable(tmp_path, fake):
    fake.info = _info_stdout(tag="AVAV", data_sha1="ab" * 20)
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _chdman().content_digest(str(chd))


# ---------------------------------------------------------------------------
# Output side: CD CHDs are extracted to a temp dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["CHT2", "CHCD", "CHTR"])
async def test_cd_chd_digest_extracts_and_hashes_the_tracks(tmp_path, fake, tag):
    fake.info = _info_stdout(tag=tag, data_sha1="ab" * 20)
    fake.extract_files = {"Game.bin": b"data trackaudio track"}
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    digest = await _chdman().content_digest(str(chd))

    # The track bytes, never the CD data SHA1 (2448-byte frames).
    assert digest == ContentDigest(kind=KIND, value=_sha1(b"data trackaudio track"))
    assert fake.commands() == ["info", "extractcd"]
    extract = next(c for c in fake.calls if "extractcd" in c)
    assert extract[extract.index("-i") + 1] == str(chd)
    assert extract[extract.index("-o") + 1].endswith(".cue")


@pytest.mark.asyncio
async def test_gdrom_chd_is_extracted_as_a_gdi(tmp_path, fake):
    fake.info = _info_stdout(tag="CHGD", data_sha1="ab" * 20)
    fake.extract_files = {"image01.bin": b"low density", "image03.bin": b"high density"}
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    digest = await _chdman().content_digest(str(chd))

    assert digest.value == _sha1(b"low density", b"high density")
    extract = next(c for c in fake.calls if "extractcd" in c)
    assert extract[extract.index("-o") + 1].endswith(".gdi")


@pytest.mark.asyncio
async def test_gdi_source_digest_hashes_its_track_files_in_sheet_order(tmp_path, fake):
    (tmp_path / "track01.bin").write_bytes(b"low density")
    (tmp_path / "track02.raw").write_bytes(b"audio")
    gdi = tmp_path / "Game.gdi"
    gdi.write_text(
        "2\n1 0 4 2352 track01.bin 0\n2 600 0 2352 track02.raw 0\n",
    )

    digest = await _chdman().content_digest(str(gdi))

    assert digest.value == _sha1(b"low density", b"audio")


@pytest.mark.asyncio
async def test_the_extract_dir_is_removed_after_a_match(tmp_path, fake):
    fake.info = _info_stdout(tag="CHT2", data_sha1="ab" * 20)
    fake.extract_files = {"Game.bin": b"track"}
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    await _chdman().content_digest(str(chd))

    assert _scratch_left(tmp_path) == []


@pytest.mark.asyncio
async def test_a_failed_extract_is_unavailable_and_removes_the_extract_dir(
    tmp_path, fake,
):
    fake.info = _info_stdout(tag="CHT2", data_sha1="ab" * 20)
    fake.extract_files = {"Game.bin": b"half a track"}
    fake.extract_rc = 1
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _chdman().content_digest(str(chd))

    assert _scratch_left(tmp_path) == []


@pytest.mark.asyncio
async def test_a_cancelled_extract_removes_the_extract_dir(tmp_path, fake):
    import asyncio

    cancel = asyncio.Event()
    fake.info = _info_stdout(tag="CHT2", data_sha1="ab" * 20)
    fake.extract_files = {"Game.bin": b"half a track"}

    def cancel_mid_extract(sheet):
        # The runner reports a cancelled child as a None returncode.
        cancel.set()
        return None, b"", b""

    fake.on_extract = cancel_mid_extract
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _chdman().content_digest(str(chd), cancel_event=cancel)

    assert _scratch_left(tmp_path) == []


@pytest.mark.asyncio
async def test_too_little_space_to_extract_is_unavailable_without_extracting(
    tmp_path, fake,
):
    fake.info = _info_stdout(tag="CHT2", data_sha1="ab" * 20, logical=10 ** 18)
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable, match="space"):
        await _chdman().content_digest(str(chd))

    assert fake.commands() == ["info"]
    assert _scratch_left(tmp_path) == []


# ---------------------------------------------------------------------------
# Which modes Round-trip
# ---------------------------------------------------------------------------


def test_createcd_and_createdvd_declare_round_trip():
    assert registry.mode_supports_round_trip("createcd")
    assert registry.mode_supports_round_trip("createdvd")


@pytest.mark.parametrize("mode", ["createhd", "createraw", "createld", "copy",
                                  "extractcd", "extractdvd"])
def test_other_chdman_modes_keep_the_container_check(mode):
    assert not registry.mode_supports_round_trip(mode)


# ---------------------------------------------------------------------------
# End to end: POST /jobs -> plan_job -> job_manager -> delete decision
# ---------------------------------------------------------------------------


@pytest.fixture(name="e2e")
def _e2e(tmp_path, monkeypatch, db_path, reset_db_engine, fake):
    """Confine the route to ``tmp_path``; stub chdman's convert and its
    container check; answer ``info`` / ``extractcd`` with :class:`FakeChdman`."""
    from services import db as _db

    from app.routes import convert as convert_routes

    _db.init_engine(db_path)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)

    async def fake_convert(input_path, output_path, mode="", *, compression=None,
                           cancel_event=None):
        Path(output_path).write_bytes(b"compressed hunks")
        yield {"progress": 100, "message": "done"}

    async def container_check_passes(path, *, cancel_event=None):
        return {"valid": True, "message": "ok"}

    service = _chdman()._service
    monkeypatch.setattr(service, "convert", fake_convert)
    monkeypatch.setattr(service, "verify", container_check_passes)
    yield {"tmp_path": tmp_path, "routes": convert_routes, "fake": fake}
    reset_db_engine()


async def _run(e2e, source, mode, **request_fields):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    routes = e2e["routes"]
    job = await routes.create_job(JobCreateRequest(
        file_path=str(source),
        mode=ConversionMode(mode),
        duplicate_action=DuplicateAction.SKIP,
        **request_fields,
    ))
    await routes.job_manager._process_job(job.id)
    return job


def _source_cue(tmp_path):
    """A two-track Redump-style cue, one file per track."""
    tracks = {"Game (Track 1).bin": b"data" * 588, "Game (Track 2).bin": b"cdda" * 588}
    for name, data in tracks.items():
        (tmp_path / name).write_bytes(data)
    cue = tmp_path / "Game.cue"
    cue.write_text("".join(
        f'FILE "{name}" BINARY\n  TRACK {i:02d} {kind}\n    INDEX 01 00:00:00\n'
        for i, (name, kind) in enumerate(
            zip(tracks, ("MODE2/2352", "AUDIO")), start=1,
        )
    ))
    return cue, [tmp_path / name for name in tracks], b"".join(tracks.values())


@pytest.mark.asyncio
async def test_createdvd_with_matching_data_sha1_deletes_the_iso(e2e):
    from services import ledger

    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(b"dvd sectors" * 4096)
    e2e["fake"].info = _info_stdout(tag="DVD ", data_sha1=_sha1(iso.read_bytes()))

    job = await _run(e2e, iso, "createdvd", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert not iso.exists()
    assert job.check_passed == "round_tripped"
    assert ledger.row_from_job(job)["check_passed"] == "round_tripped"


@pytest.mark.asyncio
async def test_createdvd_with_a_different_data_sha1_keeps_the_iso(e2e):
    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(b"dvd sectors" * 4096)
    # The overall SHA1 happens to equal the ISO's; it must not count.
    e2e["fake"].info = _info_stdout(tag="DVD ", data_sha1="0" * 40).replace(
        OVERALL_SHA1.encode(), _sha1(iso.read_bytes()).encode(),
    )

    job = await _run(e2e, iso, "createdvd", delete_on_verify=True)

    assert job.status.value == "failed"
    assert iso.exists()
    assert "content differs" in job.error_message and "Source kept" in job.error_message
    assert job.check_passed == "intact"


@pytest.mark.asyncio
async def test_createcd_with_matching_tracks_deletes_the_cue_and_its_tracks(e2e):
    cue, tracks, track_bytes = _source_cue(e2e["tmp_path"])
    fake = e2e["fake"]
    fake.info = _info_stdout(tag="CHT2", data_sha1="ab" * 20)
    # chdman extracts one .bin holding both tracks; the digest is layout-free.
    fake.extract_files = {"image.bin": track_bytes}

    job = await _run(e2e, cue, "createcd", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert not cue.exists() and not any(t.exists() for t in tracks)
    assert job.check_passed == "round_tripped"
    assert fake.commands().count("extractcd") == 1
    assert _scratch_left(e2e["tmp_path"]) == []


@pytest.mark.asyncio
async def test_createcd_with_different_tracks_keeps_the_cue_and_its_tracks(e2e):
    cue, tracks, track_bytes = _source_cue(e2e["tmp_path"])
    fake = e2e["fake"]
    fake.info = _info_stdout(tag="CHT2", data_sha1="ab" * 20)
    fake.extract_files = {"image.bin": track_bytes[:-1] + b"\x00"}

    job = await _run(e2e, cue, "createcd", delete_on_verify=True)

    assert job.status.value == "failed"
    assert cue.exists() and all(t.exists() for t in tracks)
    assert "content differs" in job.error_message and "Source kept" in job.error_message
    assert job.check_passed == "intact"
    assert _scratch_left(e2e["tmp_path"]) == []


@pytest.mark.asyncio
async def test_createcd_cancelled_during_extract_keeps_the_source_and_the_temp_dir_goes(
    e2e,
):
    from utils.delete_plan import build_delete_snapshot

    from app.models import ConversionMode

    cue, tracks, track_bytes = _source_cue(e2e["tmp_path"])
    fake = e2e["fake"]
    fake.info = _info_stdout(tag="CHT2", data_sha1="ab" * 20)
    fake.extract_files = {"image.bin": track_bytes[:100]}
    manager = e2e["routes"].job_manager

    def cancel_mid_extract(sheet):
        manager._cancel_events[job.id].set()
        return None, b"", b""

    fake.on_extract = cancel_mid_extract
    job = await manager.create_job(
        str(cue), ConversionMode.CREATECD,
        output_path=str(cue.with_suffix(".chd")),
        delete_on_verify=True,
        delete_snapshot=build_delete_snapshot(str(cue)),
    )
    await manager._process_job(job.id)

    assert job.status.value == "cancelled"
    assert cue.exists() and all(t.exists() for t in tracks)
    assert job.error_message is None
    assert _scratch_left(e2e["tmp_path"]) == []


@pytest.mark.asyncio
async def test_createcd_container_only_skips_the_extract(e2e):
    cue, _tracks, _ = _source_cue(e2e["tmp_path"])

    job = await _run(
        e2e, cue, "createcd", delete_on_verify=True, container_only=True,
    )

    assert job.status.value == "completed", job.error_message
    assert not cue.exists()
    assert job.check_passed == "intact"
    assert "extractcd" not in e2e["fake"].commands()
