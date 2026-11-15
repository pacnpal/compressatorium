"""utils.path_utils.read_beneath: one confined, no-follow read.

Every component, of the root and of the relative path, is opened without
following a symlink, the final file must be a regular file, and the bytes come
only from that one descriptor. A swap after any earlier check cannot redirect
the read.
"""
from __future__ import annotations

import os

import pytest
from utils.path_utils import read_beneath


@pytest.fixture(name="root")
def _root(tmp_path):
    root = tmp_path / "tree"
    (root / "Game").mkdir(parents=True)
    (root / "Game" / "dosbox.conf").write_bytes(b"[autoexec]\r\n")
    return os.path.realpath(root)


def test_reads_a_regular_file_below_the_root(root):
    assert read_beneath(root, "Game/dosbox.conf", max_bytes=1024) == b"[autoexec]\r\n"


def test_a_symlinked_file_is_refused_even_when_it_points_inside(root, tmp_path):
    os.remove(os.path.join(root, "Game", "dosbox.conf"))
    (tmp_path / "tree" / "real.conf").write_bytes(b"inside")
    os.symlink(os.path.join(root, "real.conf"), os.path.join(root, "Game", "dosbox.conf"))

    with pytest.raises(OSError):
        read_beneath(root, "Game/dosbox.conf", max_bytes=1024)


def test_a_symlinked_parent_directory_is_refused(root):
    os.symlink(os.path.join(root, "Game"), os.path.join(root, "Alias"))

    with pytest.raises(OSError):
        read_beneath(root, "Alias/dosbox.conf", max_bytes=1024)


def test_a_symlink_in_the_root_itself_is_refused(root, tmp_path):
    link = tmp_path / "link-to-tree"
    os.symlink(root, link)

    with pytest.raises(OSError):
        read_beneath(str(link), "Game/dosbox.conf", max_bytes=1024)


@pytest.mark.parametrize("relative", ["../x", "Game/../Game/dosbox.conf", "/etc/passwd", "", "Game//dosbox.conf", "./Game/dosbox.conf"])
def test_paths_that_leave_or_bend_the_tree_are_refused(root, relative):
    with pytest.raises(ValueError):
        read_beneath(root, relative, max_bytes=1024)


def test_a_fifo_is_refused_without_blocking(root):
    os.mkfifo(os.path.join(root, "Game", "pipe.conf"))

    with pytest.raises(OSError):
        read_beneath(root, "Game/pipe.conf", max_bytes=1024)


def test_a_directory_is_refused(root):
    with pytest.raises(OSError):
        read_beneath(root, "Game", max_bytes=1024)


def test_a_file_over_the_limit_is_refused(root):
    with pytest.raises(ValueError, match="larger"):
        read_beneath(root, "Game/dosbox.conf", max_bytes=4)
