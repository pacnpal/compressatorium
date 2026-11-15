"""services.archive_edit: rewrite a zip into a new file, crash-safe.

The helper copies a zip's members into a new archive, letting the caller
rename or drop members and add new ones, and only ever places a finished file
at the destination.
"""
from __future__ import annotations

import os
import zipfile

import pytest
from services.archive_edit import rewrite_zip
from services.subprocess_runner import ConversionCancelled


def _make_zip(path, members, *, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=(1995, 6, 1, 12, 0, 0))
            info.compress_type = compression
            zf.writestr(info, data)
    return str(path)


def _read(path):
    with zipfile.ZipFile(path) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def _leftovers(directory, keep):
    return sorted(n for n in os.listdir(directory) if n not in keep)


def test_renames_members_and_adds_new_ones(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"Game/A.EXE": b"exe", "Game/D/B.DAT": b"dat"})
    dst = str(tmp_path / "out.zip")

    rewrite_zip(
        src, dst,
        rename=lambda n: n.removeprefix("Game/"),
        add={"dosbox.conf": b"[autoexec]\r\n"},
    )

    assert _read(dst) == {
        "A.EXE": b"exe",
        "D/B.DAT": b"dat",
        "dosbox.conf": b"[autoexec]\r\n",
    }


def test_rename_returning_none_drops_the_member(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"Game/": b"", "Game/A.EXE": b"exe"})
    dst = str(tmp_path / "out.zip")

    rewrite_zip(src, dst, rename=lambda n: None if n == "Game/" else n)

    assert list(_read(dst)) == ["Game/A.EXE"]


def test_an_added_member_replaces_a_member_of_the_same_name(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"Game/dosbox.conf": b"old", "Game/A.EXE": b"x"})
    dst = str(tmp_path / "out.zip")

    rewrite_zip(
        src, dst,
        rename=lambda n: n.removeprefix("Game/"),
        add={"dosbox.conf": b"new"},
    )

    with zipfile.ZipFile(dst) as zf:
        names = zf.namelist()
        assert names.count("dosbox.conf") == 1
        assert zf.read("dosbox.conf") == b"new"


def test_two_members_renamed_onto_one_name_are_refused(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"a/X": b"1", "b/X": b"2"})
    dst = str(tmp_path / "out.zip")

    with pytest.raises(ValueError, match="X"):
        rewrite_zip(src, dst, rename=lambda n: n.split("/", 1)[1])

    assert _leftovers(tmp_path, {"in.zip"}) == []


def test_an_existing_destination_is_never_replaced(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"new"})
    dst = tmp_path / "out.zip"
    dst.write_bytes(b"precious")

    with pytest.raises(FileExistsError):
        rewrite_zip(src, str(dst))

    assert dst.read_bytes() == b"precious"
    assert _leftovers(tmp_path, {"in.zip", "out.zip"}) == []


def test_cancel_leaves_no_output_and_no_temp_file(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a" * 100_000, "B": b"b"})
    dst = str(tmp_path / "out.zip")

    with pytest.raises(ConversionCancelled):
        rewrite_zip(src, dst, is_cancelled=lambda: True)

    assert _leftovers(tmp_path, {"in.zip"}) == []


def test_a_corrupt_member_fails_without_leaving_output(tmp_path):
    src = tmp_path / "in.zip"
    _make_zip(src, {"A": b"a" * 50_000}, compression=zipfile.ZIP_STORED)
    raw = bytearray(src.read_bytes())
    raw[200] ^= 0xFF  # inside A's stored data, so its CRC no longer matches
    src.write_bytes(bytes(raw))
    dst = str(tmp_path / "out.zip")

    with pytest.raises(zipfile.BadZipFile):
        rewrite_zip(str(src), dst)

    assert _leftovers(tmp_path, {"in.zip"}) == []


def test_progress_counts_up_to_the_total_uncompressed_size(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a" * 3000, "B": b"b" * 1000})
    dst = str(tmp_path / "out.zip")
    seen = []

    rewrite_zip(src, dst, on_progress=lambda done, total: seen.append((done, total)))

    assert seen[-1] == (4000, 4000)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_member_dates_and_compression_carry_over(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a"}, compression=zipfile.ZIP_STORED)
    dst = str(tmp_path / "out.zip")

    rewrite_zip(src, dst)

    with zipfile.ZipFile(dst) as zf:
        info = zf.getinfo("A")
    assert info.date_time == (1995, 6, 1, 12, 0, 0)
    assert info.compress_type == zipfile.ZIP_STORED
