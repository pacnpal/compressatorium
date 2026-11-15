"""The default lock directory sits in the data dir (#306).

Two containers (the server, and a CLI container such as
docker-compose.cli.yml) share the /config mount but not /tmp, so their output
locks and conversion slots only meet when the lock files live under /config.
"""
from __future__ import annotations

import os

from config import Settings


def test_default_lock_dir_is_under_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CHD_CONCURRENCY_LOCK_DIR", raising=False)

    settings = Settings(data_dir=str(tmp_path))

    assert settings.concurrency_lock_dir == str(tmp_path / "locks")
    assert (tmp_path / "locks").is_dir()


def test_an_explicit_lock_dir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CHD_CONCURRENCY_LOCK_DIR", str(tmp_path / "elsewhere"))

    settings = Settings(data_dir=str(tmp_path))

    assert settings.concurrency_lock_dir == str(tmp_path / "elsewhere")


def test_an_unwritable_data_dir_falls_back_to_tmp(tmp_path, monkeypatch):
    monkeypatch.delenv("CHD_CONCURRENCY_LOCK_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory")

    settings = Settings(data_dir=str(blocked))

    assert settings.concurrency_lock_dir == os.path.join(str(tmp_path / "tmp"), "chd-locks")
