"""Unit tests for the shared disk-headroom preflight (``services.disk``)."""
from __future__ import annotations

import os

import pytest

from app.services import disk
from app.services.disk import InsufficientDiskSpace, ensure_headroom


class _Usage:
    def __init__(self, free):
        self.total = free
        self.used = 0
        self.free = free


def test_passes_when_space_available(monkeypatch, tmp_path):
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda p: _Usage(10_000))
    # Should not raise: each target needs less than free.
    ensure_headroom([(str(tmp_path), 1_000)], margin_bytes=100)


def test_raises_when_below_required_plus_margin(monkeypatch, tmp_path):
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda p: _Usage(1_500))
    with pytest.raises(InsufficientDiskSpace):
        ensure_headroom([(str(tmp_path), 1_000)], margin_bytes=1_000)


def test_same_mount_requirements_are_summed(monkeypatch, tmp_path):
    # Two targets on the same device must be summed, not checked independently.
    monkeypatch.setattr(disk.os, "stat", lambda p: os.stat_result(
        (0, 0, 42, 0, 0, 0, 0, 0, 0, 0)  # st_dev (index 2) == 42 for both
    ))
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda p: _Usage(2_500))
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    # 1500 + 1500 = 3000 > 2500 free -> must raise even though each alone fits.
    with pytest.raises(InsufficientDiskSpace):
        ensure_headroom([(str(a), 1_500), (str(b), 1_500)])


def test_nearest_existing_ancestor_used_for_missing_target(tmp_path):
    # A not-yet-created output dir resolves to its nearest existing ancestor.
    missing = tmp_path / "does" / "not" / "exist"
    assert disk._nearest_existing(str(missing)) == str(tmp_path)
