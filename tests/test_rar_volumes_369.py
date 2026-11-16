"""A later volume of a multi-volume RAR is not an archive of its own (#369).

``Pack.part1.rar`` lists every member of the set (#367). ``Pack.part2.rar``
used to list as an archive with 0 items, and every listing or search of its
folder logged rarfile's "Need to start from first volume" as an ERROR
traceback. One shared check in ``services.archive_members``
(``later_volume_of``) decides by name and confirms with rarfile; a later
volume lists as a plain file and is never searched as an archive.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import zlib

import pytest

from .test_scan_walk_361 import _rar3_block, _two_volume_rar

BOUND = 5.0


def _single_volume_rar(path, name="Disc.iso", data=b"one"):
    """A plain one-volume RAR 2.9 archive, whatever its file name says."""
    encoded = name.encode()
    body = struct.pack(
        "<LLBLLBBHL", len(data), len(data), 3, zlib.crc32(data), 0, 29, 0x30,
        len(encoded), 0o100644 << 16,
    ) + encoded
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(
        b"Rar!\x1a\x07\x00"
        + _rar3_block(0x73, 0, b"\x00" * 6)
        + _rar3_block(0x74, 0x8000, body) + data
        + _rar3_block(0x7B, 0),
    )
    return path


@pytest.fixture(name="members")
def _members():
    from services import archive_members

    if not archive_members.HAS_RAR:
        pytest.skip("rarfile is not installed")
    archive_members.clear_cache()
    yield archive_members
    archive_members.clear_cache()


@pytest.fixture(name="files_routes")
def _files_routes(tmp_path, monkeypatch, members):
    from routes import files as files_routes

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    return files_routes


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_later_volume_names_its_first_volume(tmp_path, members):
    first = _two_volume_rar(tmp_path / "Packs")
    second = first.with_name("Pack.part2.rar")
    lone = _single_volume_rar(tmp_path / "Lone" / "x.part2.rar")

    assert members.later_volume_of(str(second)) == str(first)
    assert members.later_volume_of(str(first)) is None
    assert members.later_volume_of(str(lone)) is None


@pytest.mark.asyncio
async def test_the_browser_lists_a_later_volume_as_a_plain_file(
    tmp_path, files_routes, caplog,
):
    first = _two_volume_rar(tmp_path / "Packs")
    with caplog.at_level(logging.WARNING):
        listing = await asyncio.wait_for(
            files_routes.list_files(path=str(first.parent), show_archives=True,
                                    summarize_archives=True),
            BOUND,
        )

    by_name = {entry.name: entry for entry in listing.entries}
    assert by_name["Pack.part1.rar"].type == "archive"
    assert by_name["Pack.part1.rar"].archive_items == 2
    later = by_name["Pack.part2.rar"]
    assert later.type == "file"
    assert later.archive_items is None
    assert later.verifiable_by == [] and later.convertible_by == []
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_a_search_never_opens_a_later_volume_as_an_archive(
    tmp_path, files_routes, caplog,
):
    first = _two_volume_rar(tmp_path / "Packs")
    with caplog.at_level(logging.WARNING):
        found = await asyncio.wait_for(
            files_routes.search_files(path=str(first.parent), recursive=True,
                                      include_archives=True),
            BOUND,
        )

    containers = sorted(row["name"] for row in found["files"] if row.get("type") == "archive")
    assert containers == ["Pack.part1.rar"]
    members = sorted(
        (row["archive_path"].rsplit("/", 1)[-1], row["name"]) for row in found["archives"]
    )
    assert members == [("Pack.part1.rar", "Disc 1.iso"), ("Pack.part1.rar", "Disc 2.iso")]
    assert _errors(caplog) == []


def test_listing_a_later_volume_directly_logs_no_error(tmp_path, members, caplog):
    from services.archive import archive_service

    second = _two_volume_rar(tmp_path / "Packs").with_name("Pack.part2.rar")
    with caplog.at_level(logging.WARNING):
        result = archive_service.list_archive_contents(str(second), include_meta=True)

    assert result["entries"] == []
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_a_single_volume_rar_named_like_a_later_part_still_lists(
    tmp_path, files_routes, caplog,
):
    lone = _single_volume_rar(tmp_path / "Lone" / "x.part2.rar")
    with caplog.at_level(logging.WARNING):
        listing = await asyncio.wait_for(
            files_routes.list_files(path=str(lone.parent), show_archives=True,
                                    summarize_archives=True),
            BOUND,
        )

    entry = {e.name: e for e in listing.entries}["x.part2.rar"]
    assert entry.type == "archive" and entry.archive_items == 1
    assert _errors(caplog) == []


def test_a_first_volume_missing_its_later_part_still_refuses_to_list(tmp_path, members):
    first = _two_volume_rar(tmp_path / "Packs")
    first.with_name("Pack.part2.rar").unlink()

    with pytest.raises(members.IncompleteArchive):
        members.read_archive_members(str(first))
