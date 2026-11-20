"""The default lock directory sits in the data dir (#306).

Two containers (the server, and a CLI container such as
docker-compose.cli.yml) share the /config mount but not /tmp, so their output
locks and conversion slots only meet when the lock files live under /config.

The choice is a pure function of configuration, never of whether a folder
exists yet: with a data dir configured it is always
``<data dir>/locks``, created when chosen, and an uncreatable one fails at
startup instead of quietly moving the locks somewhere no other process looks.
"""
from __future__ import annotations

import os
import stat

import pytest

from config import Settings


@pytest.fixture(autouse=True)
def _no_lock_dir_override(monkeypatch):
    monkeypatch.delenv("CHD_CONCURRENCY_LOCK_DIR", raising=False)


def test_default_lock_dir_is_under_the_data_dir(tmp_path):
    settings = Settings(data_dir=str(tmp_path))

    assert settings.concurrency_lock_dir == str(tmp_path / "locks")
    # Made when chosen, owner only.
    assert (tmp_path / "locks").is_dir()
    assert stat.S_IMODE(os.stat(tmp_path / "locks").st_mode) == 0o700


def test_an_explicit_lock_dir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CHD_CONCURRENCY_LOCK_DIR", str(tmp_path / "elsewhere"))

    settings = Settings(data_dir=str(tmp_path))

    assert settings.concurrency_lock_dir == str(tmp_path / "elsewhere")


def test_a_data_dir_that_does_not_exist_yet_gets_the_same_lock_dir_every_time(
    tmp_path, db_path, reset_db_engine,
):
    """Before: the first process saw no data dir and chose $TMPDIR/chd-locks;
    database init then created the data dir, and the next process chose
    <data dir>/locks. Two processes, two lock dirs."""
    from services import db as _db

    data_dir = tmp_path / "not" / "yet"
    before = Settings(data_dir=str(data_dir))
    try:
        _db.init_app_database(before)
        after = Settings(data_dir=str(data_dir))
    finally:
        reset_db_engine()

    assert before.concurrency_lock_dir == after.concurrency_lock_dir
    assert before.concurrency_lock_dir == str(data_dir / "locks")


def test_the_data_dir_is_configured_through_the_environment_too(tmp_path, monkeypatch):
    monkeypatch.setenv("CHD_DATA_DIR", str(tmp_path / "config"))

    assert Settings().concurrency_lock_dir == str(tmp_path / "config" / "locks")


def test_an_uncreatable_data_dir_fails_at_startup(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory")

    with pytest.raises(RuntimeError, match="lock"):
        Settings(data_dir=str(blocked))


def test_without_a_data_dir_every_process_uses_one_fixed_path(monkeypatch, tmp_path):
    """No data dir configured at all (a dev checkout without /config): the
    fallback names one path whatever TMPDIR says."""
    monkeypatch.delenv("CHD_DATA_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "one"))
    first = Settings().concurrency_lock_dir
    monkeypatch.setenv("TMPDIR", str(tmp_path / "two"))
    second = Settings().concurrency_lock_dir

    assert first == second
