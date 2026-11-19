"""Shared fixtures and sample payloads for the DB / migration test suite.

Centralised here so the four DB-focused test modules
(``test_db_migration``, ``test_db_startup_integration``,
``test_db_engine_pragmas``, ``test_db_store_operations``,
``test_db_migration_edge_cases``) don't drift apart or duplicate
boilerplate.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Before anything imports config: a test run gets a lock dir of its own. With
# no data dir configured the default is the one fixed /tmp/chd-locks (#306),
# so two test runs at once (two worktrees, a reviewer's copy) would otherwise
# share one conversion slot and queue and wait on each other.
os.environ.setdefault(
    "CHD_CONCURRENCY_LOCK_DIR", tempfile.mkdtemp(prefix="compressatorium-test-locks-"),
)


@pytest.fixture(autouse=True)
def _release_leaked_tickets():
    """A job a test creates but never runs keeps its FIFO ticket in the lock
    dir, held by this live process, so it is never pruned and sits ahead of
    every later test's jobs: a later ``_process_job`` waits for its turn
    forever. Release whatever tickets a test reserved and left behind."""
    from services.concurrency_manager import concurrency_manager

    before = set(concurrency_manager._ticket_handles)
    yield
    for key in set(concurrency_manager._ticket_handles) - before:
        concurrency_manager.release(key)


@pytest.fixture(autouse=True)
def _restore_tool_instances():
    """The registry's tools are process-wide singletons. ``monkeypatch.setattr``
    on one of them (``tool.verify = fake``) undoes by *setting* the class's
    bound method on the instance, not by deleting the attribute, and that pin
    shadows any later test's patch of the class. Put every tool's instance
    attributes back as they were before the test."""
    from services.tools import registry

    before = {tool: dict(vars(tool)) for tool in registry.all()}
    yield
    for tool, attrs in before.items():
        if vars(tool) != attrs:
            vars(tool).clear()
            vars(tool).update(attrs)


@pytest.fixture(autouse=True)
def _programs_as_in_the_image():
    """The suite runs as if inside the Docker image, where every program ships.

    Since #494 a tool whose program is missing is not ready, and CI (like a
    dev checkout) has no chdman, dolphin-tool, maxcso and the rest at their
    configured paths. So the paths those tools are configured with when a test
    starts count as installed, at the shared probe's one seam
    (``binaries._inspect``). Any other path, such as a test's own file in
    ``tmp_path`` or a bare name it puts on ``PATH``, is probed for real, which
    is how ``test_binary_readiness_494`` covers a missing or non-executable
    program. Only the tools that never checked their program before #494 are
    included, so jwud (checked before) and ``zstd`` keep their real answers.
    Every cached answer is dropped around each test.

    Patched by hand rather than through ``monkeypatch``: requesting it here
    would set it up before ``_restore_tool_instances``, so its undo would run
    after that restore and pin a test's instance patches on the singletons.
    """
    from services.tools import binaries, registry

    installed = {
        needed.path
        for tool_id in (
            "chdman", "dolphin", "z3ds", "cso", "romz", "makeps3iso", "xiso", "nkit",
        )
        for needed in registry.get(tool_id).required_binaries
    }
    real = binaries._inspect

    def inspect(path: str) -> tuple[str, str]:
        if path in installed:
            return binaries._OK, path
        return real(path)

    binaries._inspect = inspect
    binaries.forget_binary_probes()
    try:
        yield
    finally:
        binaries._inspect = real
        binaries.forget_binary_probes()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Fresh SQLite file per test (on-disk, not :memory:, because migration
    tests assert persistence across engine disposals)."""
    return str(tmp_path / "compressatorium.db")


@pytest.fixture
def reset_db_engine():
    """Explicit reset helper for tests that re-init the engine mid-test.

    The autouse ``_reset_engine`` fixture in individual test modules
    handles the before/after-test cleanup.  This fixture returns a
    zero-arg callable for tests that need to simulate a process
    restart: ``reset()`` disposes the current engine and clears the
    module-level ``SessionLocal`` so the next ``init_engine`` starts
    fresh.
    """
    def _reset() -> None:
        # Imported here, never at module level: the lock dir above must be set
        # before anything imports config.
        from services import db as _db

        if _db.engine is not None:
            _db.engine.dispose()
        _db.engine = None
        _db.SessionLocal = None

    return _reset


# --- Auto and archive-conversion route fixtures ------------------------------
# Shared by the Auto route tests and the archive end-to-end tests. Module-level
# setup a test file wants for every test (a clear detection cache, tmp_path as
# the Volume) is opted into with `pytest.mark.usefixtures`, not autouse here.

@pytest.fixture
def fresh_detect_cache():
    """The Platform-detection cache empty before and after the test."""
    from services import platform_detect

    platform_detect.clear_cache()
    yield
    platform_detect.clear_cache()


@pytest.fixture
def tmp_volume(tmp_path: Path, monkeypatch):
    """``tmp_path`` is the configured Volume, so the folder walk reaches every
    folder a test builds under it (and nothing above it)."""
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))


@pytest.fixture
def route_env(tmp_path: Path, monkeypatch):
    """Routes confined to ``tmp_path``; the queue and preferences stubbed."""
    from app.routes import convert as convert_routes

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 0)

    queued: list[dict] = []

    async def fake_atomic(job_specs, mode, **kwargs):
        queued.append({
            "mode": mode.value,
            "files": [Path(s["file_path"]).name for s in job_specs],
            "compression": kwargs.get("compression"),
        })
        return []

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)

    async def fake_prefs(key):
        assert key == "conversion"
        return {"chdman": "zlib,lzma", "dolphin": "zstd:19"}

    monkeypatch.setattr(convert_routes.preferences_store, "get", fake_prefs)
    return {"tmp_path": tmp_path, "queued": queued}


@pytest.fixture
def e2e_env(tmp_path: Path, monkeypatch):
    """Confine the route to ``tmp_path`` and stub the converter binary.

    Returns the recorded ``(input_path, output_path)`` the stub was invoked
    with so tests can assert the real extracted file reached the tool.
    """
    from app.routes import convert as convert_routes

    # Confine path validation + extraction temp dir to the test directory.
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)

    calls: list[dict] = []

    async def fake_convert(input_path, output_path, mode, *, compression=None,
                           split=False, cancel_event=None):
        # The member must have been extracted to a real temp file before the
        # tool is invoked, this is the core of the archive-conversion path.
        assert os.path.isfile(input_path), f"member not extracted: {input_path}"
        # Capture the extracted bytes now: the temp dir is removed during the
        # job's cleanup, so it can't be re-read after _process_job returns.
        calls.append({
            "input": input_path,
            "output": output_path,
            "content": Path(input_path).read_bytes(),
        })
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        # Stand in for the binary: produce the output file.
        Path(output_path).write_bytes(Path(input_path).read_bytes())
        yield {"progress": 50, "message": "working"}
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode, **_kwargs):
        # createcd/createdvd would otherwise read a disc serial off the (junk)
        # source via ChdmanTool.post_convert; stub the hook so the test stays
        # deterministic and offline.
        return None

    # registry is the shared singleton job_manager dispatches through.
    for tool in convert_routes.registry.all():
        monkeypatch.setattr(tool, "convert", fake_convert)
        monkeypatch.setattr(tool, "post_convert", noop_post_convert)

    return {"tmp_path": tmp_path, "calls": calls}


SAMPLE_DAT_STORE = {
    "dats": {
        "abc12345": {
            "id": "abc12345",
            "name": "Nintendo - GameCube",
            "description": "Redump GC",
            "version": "0.285",
            "imported_at": "2026-04-11T00:00:00Z",
            "file_count": 2,
        },
        "def67890": {
            "id": "def67890",
            "name": "Nintendo - Wii",
            "description": "Redump Wii",
            "version": "0.285",
            "imported_at": "2026-04-11T00:00:00Z",
            "file_count": 1,
        },
    },
    "hashes": {
        "sha1": {
            "a" * 40: {
                "dat_id": "abc12345",
                "game_name": "Super Mario Sunshine",
                "rom_name": "Super Mario Sunshine.iso",
                "size": 1459978240,
            },
            "b" * 40: {
                "dat_id": "abc12345",
                "game_name": "Metroid Prime",
                "rom_name": "Metroid Prime.iso",
                "size": 1459978240,
            },
            "c" * 40: {
                "dat_id": "def67890",
                "game_name": "Wii Sports",
                "rom_name": "Wii Sports.iso",
                "size": 4699979776,
            },
        },
        "md5": {
            "a" * 32: {
                "dat_id": "abc12345",
                "game_name": "Super Mario Sunshine",
                "rom_name": "Super Mario Sunshine.iso",
                "size": 1459978240,
            },
        },
    },
    "matches": {
        "/data/gc/mario.chd": {
            "path": "/data/gc/mario.chd",
            "matched": True,
            "dat_id": "abc12345",
            "dat_name": "Nintendo - GameCube",
            "game_name": "Super Mario Sunshine",
            "rom_name": "Super Mario Sunshine.iso",
            "match_type": "file_sha1",
            "file_hash": "a" * 40,
        },
        "/data/misc/unknown.chd": {
            "path": "/data/misc/unknown.chd",
            "matched": False,
        },
    },
}

SAMPLE_VERIFICATION = {
    "/data/gc/mario.chd": {
        "chd_path": "/data/gc/mario.chd",
        "source_path": "/data/gc/mario.iso",
        "verified_at": "2026-04-10T12:00:00Z",
    },
    "/data/wii/sports.chd": {
        "chd_path": "/data/wii/sports.chd",
        "source_path": None,
        "verified_at": "2026-04-10T13:00:00Z",
    },
}

SAMPLE_CHD_METADATA = {
    "/data/gc/mario.chd": {
        "chd_path": "/data/gc/mario.chd",
        "metadata": {"file": "mario.chd", "sha1": "a" * 40, "raw_data": "..."},
        "media_type": "dvd",
        "mtime": 1700000000.0,
        "cached_at": "2026-04-10T14:00:00Z",
        "disc_id_checked": True,
        "disc_id_checked_mtime": 1700000000.0,
        "game_id": "GMSE01",
        "title": "Super Mario Sunshine",
    },
    "/data/cd/psx.chd": {
        "chd_path": "/data/cd/psx.chd",
        "metadata": {"file": "psx.chd", "sha1": "b" * 40},
        "media_type": "cd",
        "mtime": 1700001000.0,
        "cached_at": "2026-04-10T14:05:00Z",
        "disc_id_checked": False,
        "disc_id_checked_mtime": None,
        "game_id": None,
        "title": None,
    },
}

SAMPLE_DAT_SYNC = {
    "last_sync_tag": "0.285",
    "last_sync_at": "2026-04-11T00:00:00Z",
    "last_sync_files": 69,
}
