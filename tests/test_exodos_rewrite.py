"""services.exodos.rewrite_conf: the dosbox.conf path rewrite.

eXoDOS writes its mounts relative to the pack root (``.\\eXoDOS\\<Game>\\...``).
In the repacked file the game's own folder is the root, so those paths lose
the ``eXoDOS\\<Game>`` part. Only mount/imgmount/boot lines are touched, and
the bytes and line endings around them are kept exactly.
"""
from __future__ import annotations

from services.exodos import EXODOS, rewrite_conf


def _rw(text: bytes, top: str = "Abaron"):
    return rewrite_conf(text, top, EXODOS)


def test_strips_the_pack_and_game_folders_from_mount_paths():
    conf = (
        b"[autoexec]\r\n"
        b"mount c .\\eXoDOS\\Abaron\r\n"
        b"imgmount d .\\eXoDOS\\Abaron\\CD\\ABARON.CUE -t cdrom\r\n"
    )

    result = _rw(conf)

    assert result.data == (
        b"[autoexec]\r\n"
        b"mount c .\\\r\n"
        b"imgmount d .\\CD\\ABARON.CUE -t cdrom\r\n"
    )
    assert result.leftovers == []


def test_keeps_lf_endings_and_a_last_line_without_one():
    conf = b"mount c .\\eXoDOS\\Abaron\nboot .\\eXoDOS\\Abaron\\DISK.IMG"

    assert _rw(conf).data == b"mount c .\\\nboot .\\DISK.IMG"


def test_matches_keywords_and_paths_in_any_case_with_an_at_sign():
    conf = b"  @ MOUNT C .\\EXODOS\\abaron\\GAME\r\n@imgmount d .\\exodos\\ABARON\\cd.iso\r\n"

    assert _rw(conf).data == b"  @ MOUNT C .\\GAME\r\n@imgmount d .\\cd.iso\r\n"


def test_a_quoted_path_keeps_its_quotes():
    conf = b'imgmount d ".\\eXoDOS\\Abaron\\CD\\A B.CUE" -t cdrom\r\n'

    assert _rw(conf).data == b'imgmount d ".\\CD\\A B.CUE" -t cdrom\r\n'


def test_a_bare_pack_folder_becomes_the_root():
    conf = b"mount c .\\eXoDOS\\ -t dir\r\n"

    assert _rw(conf).data == b"mount c .\\ -t dir\r\n"


def test_a_game_name_that_merely_starts_the_same_is_left_alone():
    conf = b"mount d .\\eXoDOS\\Abcd\\CD\r\n"

    result = _rw(conf, top="Abc")

    assert result.data == conf
    assert result.leftovers == ["mount d .\\eXoDOS\\Abcd\\CD"]


def test_lines_that_are_not_mounts_are_never_rewritten():
    conf = (
        b"# mount c .\\eXoDOS\\Abaron\r\n"
        b"rem mount c .\\eXoDOS\\Abaron\r\n"
        b"cd .\\eXoDOS\\Abaron\r\n"
        b"mountain .\\eXoDOS\\Abaron\r\n"
    )

    assert _rw(conf).data == conf


def test_reports_lines_that_still_point_into_the_pack():
    conf = (
        b"# mount c .\\eXoDOS\\Other\r\n"
        b"mount c .\\eXoDOS\\Abaron\r\n"
        b"imgmount e .\\eXoDOS\\Other\\CD.CUE -t cdrom\r\n"
        b"call .\\eXoDOS\\!dos\\Abaron\\run.bat\r\n"
    )

    result = _rw(conf)

    assert result.leftovers == [
        "imgmount e .\\eXoDOS\\Other\\CD.CUE -t cdrom",
        "call .\\eXoDOS\\!dos\\Abaron\\run.bat",
    ]


def test_bytes_outside_ascii_pass_through_untouched():
    conf = b"echo Caf\x82 \x9b\r\nmount c .\\eXoDOS\\Abaron\\D\x82J\r\n"

    assert _rw(conf).data == b"echo Caf\x82 \x9b\r\nmount c .\\D\x82J\r\n"


def test_folder_mounts_on_other_drives_are_reported():
    """DOSBox Pure's MOUNT reads the host filesystem, so inside the file only
    C: (the file itself), IMGMOUNT and BOOT can work."""
    conf = (
        b"mount c .\\eXoDOS\\Abaron\r\n"
        b"mount d .\\eXoDOS\\Abaron\\CD -t cdrom\r\n"
        b"@MOUNT A: .\\eXoDOS\\Abaron\\FLOPPY -t floppy\r\n"
        b"mount -u d\r\n"
        b"imgmount d .\\eXoDOS\\Abaron\\CD\\A.CUE -t cdrom\r\n"
    )

    result = _rw(conf)

    assert result.folder_mounts == [
        "mount d .\\CD -t cdrom",
        "@MOUNT A: .\\FLOPPY -t floppy",
    ]
    assert result.leftovers == []
