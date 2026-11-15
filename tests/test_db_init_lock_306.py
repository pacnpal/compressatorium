"""Concurrent first-run database initialisation (codex 4577304461).

Several CLI processes started at once on a fresh database each ran Alembic,
and all but one failed with "table already exists". ``init_app_database``
now runs its migrations and legacy import under an exclusive ``init.lock``
flock in the shared lock dir, so the first creates the schema and the rest
find it at head.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

APP = str(Path(__file__).resolve().parents[1] / "app")

_INIT = textwrap.dedent("""
    import sys, time
    from config import Settings
    from services import db
    settings = Settings(data_dir=sys.argv[1])
    # Line every process up, so they really run at the same moment.
    while time.time() < float(sys.argv[2]):
        time.sleep(0.005)
    db.init_app_database(settings)
    print("ok", flush=True)
""")


def test_three_processes_initialise_one_fresh_database(tmp_path):
    import time

    data_dir = tmp_path / "config"
    start = time.time() + 1.5
    env = {**os.environ, "PYTHONPATH": APP}
    env.pop("CHD_CONCURRENCY_LOCK_DIR", None)  # the lock dir under this data dir
    env.pop("COMPRESSATORIUM_DB_PATH", None)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _INIT, str(data_dir), repr(start)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(3)
    ]
    results = [proc.communicate(timeout=120) for proc in procs]

    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, err
        assert out.strip() == "ok", err
    db_file = next(data_dir.glob("*.db"))
    with sqlite3.connect(db_file) as conn:
        revisions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
    assert len(revisions) == 1
    assert "verifications" in tables
    assert (data_dir / "locks" / "init.lock").exists()
