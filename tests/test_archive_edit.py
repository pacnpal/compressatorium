"""services.archive_edit: rewrite a zip into a new file, crash-safe.

The helper copies a zip's members into a new archive, letting the caller
rename or drop members and add new ones, and only ever places a finished file
at the destination.
"""
from __future__ import annotations

import os
import stat
import struct
import zipfile
from pathlib import Path

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


def test_the_temp_file_is_hidden_and_ends_in_tmp_while_it_is_written(tmp_path):
    """RomM's scanner skips ``.tmp`` files by default; a hidden name alone is not enough."""
    src = _make_zip(tmp_path / "in.zip", {"A": b"a" * 3000})
    dst = str(tmp_path / "out.dosz")
    seen = []

    def watch(_done, _total):
        seen.append(sorted(set(os.listdir(tmp_path)) - {"in.zip"}))

    rewrite_zip(src, dst, on_progress=watch)

    in_flight = [names for names in seen if names and names != ["out.dosz"]]
    assert in_flight, "no progress was reported while the temp file existed"
    for names in in_flight:
        # The temp file and its sidecar lock (#331): both hidden, and both
        # carry an extension RomM's scanner skips by default.
        assert len(names) == 2
        assert all(name.startswith(".") for name in names)
        temp, lock = sorted(names)
        assert temp.endswith(".tmp")
        assert lock == temp + ".lock"


def test_added_members_come_first(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A.CONF": b"other", "B": b"b"})
    dst = str(tmp_path / "out.zip")

    rewrite_zip(src, dst, add={"dosbox.conf": b"conf"})

    with zipfile.ZipFile(dst) as zf:
        assert zf.namelist() == ["dosbox.conf", "A.CONF", "B"]


def test_added_members_carry_the_given_timestamp(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a"})
    first, second = str(tmp_path / "one.zip"), str(tmp_path / "two.zip")

    for dst in (first, second):
        rewrite_zip(src, dst, add={"x": b"x"}, added_date_time=(2001, 2, 3, 4, 5, 6))

    with zipfile.ZipFile(first) as zf:
        assert zf.getinfo("x").date_time == (2001, 2, 3, 4, 5, 6)
    assert Path(first).read_bytes() == Path(second).read_bytes()


def test_small_members_get_no_zip64_local_header(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a" * 100})
    dst = str(tmp_path / "out.zip")

    rewrite_zip(src, dst)

    with zipfile.ZipFile(dst) as zf:
        info = zf.getinfo("A")
    raw = Path(dst).read_bytes()
    extra_len = struct.unpack_from("<H", raw, info.header_offset + 28)[0]
    assert extra_len == 0


def test_a_failed_check_places_nothing(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a"})
    dst = str(tmp_path / "out.zip")
    checked = []

    def check(path):
        checked.append(path)
        raise ValueError("bad product")

    with pytest.raises(ValueError, match="bad product"):
        rewrite_zip(src, dst, check=check)

    assert checked and checked[0] != dst
    assert _leftovers(tmp_path, {"in.zip"}) == []


def test_the_check_reads_the_finished_temp_file(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a"})
    dst = str(tmp_path / "out.zip")
    names = []

    rewrite_zip(src, dst, add={"c": b"c"}, check=lambda p: names.extend(zipfile.ZipFile(p).namelist()))

    assert names == ["c", "A"]


def test_a_destination_that_appears_mid_write_is_never_replaced(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a" * 3000})
    dst = tmp_path / "out.zip"

    def squat(_done, _total):
        if not dst.exists():
            dst.write_bytes(b"someone else's")

    with pytest.raises(FileExistsError):
        rewrite_zip(src, str(dst), on_progress=squat)

    assert dst.read_bytes() == b"someone else's"
    assert _leftovers(tmp_path, {"in.zip", "out.zip"}) == []


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_the_product_takes_the_sources_permissions(tmp_path, mode):
    """mkstemp makes 0600 files; a player running as another user must still read it."""
    src = _make_zip(tmp_path / "in.zip", {"A": b"a"})
    os.chmod(src, mode)
    dst = tmp_path / "out.zip"

    rewrite_zip(src, str(dst))

    assert stat.S_IMODE(os.stat(dst).st_mode) == mode


def test_special_permission_bits_are_not_copied(tmp_path):
    src = _make_zip(tmp_path / "in.zip", {"A": b"a"})
    os.chmod(src, 0o6755)
    dst = tmp_path / "out.zip"

    rewrite_zip(src, str(dst))

    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o755


def test_a_cancel_after_the_check_still_places_nothing(tmp_path):
    """The last gate before placement: an abandoned run must never publish."""
    src = _make_zip(tmp_path / "in.zip", {"A": b"a"})
    dst = str(tmp_path / "out.zip")
    state = {"checked": False}

    def check(_path):
        state["checked"] = True

    with pytest.raises(ConversionCancelled):
        rewrite_zip(src, dst, check=check, is_cancelled=lambda: state["checked"])

    assert _leftovers(tmp_path, {"in.zip"}) == []
