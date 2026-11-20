"""Libraries may overlap; the nearest root owns a file (#514).

A folder can be registered inside RomM's library folder, inside or around
another folder Library, and RomM's folder can be set around one. A file
belongs to the Library with the most specific root (``libraries.library_for``):
its Referrers apply, and only its Rules sweep it. An outer Library's listing
leaves out every file an inner Library owns, so no file is swept or converted
by two Libraries. What still refuses: the same root twice
(``tests/test_folder_library_303.py``), an Inbox overlapping a Library
(``tests/test_inbox_431.py``), and a file listed in one Library but living in
another (``tests/test_library_for_listed_path_413.py``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_folder_library_303 import _IS_ROOT, _RULES, _register
from .test_folder_library_303 import _db_fixture as _db303
from .test_folder_library_303 import _queue as _queue303
from .test_folder_library_303 import _ready as _ready303
from .test_folder_library_303 import _tree as _tree303
from .test_platform_detect import _gamecube, _ps2

_FIXTURES = (_db303, _tree303, _ready303, _queue303)


async def _romm_root(path: Path) -> None:
    from services.romm import settings as romm_settings

    await romm_settings.save({"url": "http://romm:8080", "library_root": str(path)})


# ---- registering -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_folder_inside_romms_library_registers(db: Path) -> None:
    library = db / "library"
    (library / "roms" / "gc").mkdir(parents=True)
    await _romm_root(library)

    created = await _register(library / "roms" / "gc")

    assert created["root"] == str((library / "roms" / "gc").resolve())
    assert created["within"] == {"id": "romm", "name": "RomM"}


@pytest.mark.asyncio
async def test_a_folder_inside_or_around_another_folder_library_registers(tree: Path) -> None:
    outer = await _register(tree)
    inner = await _register(tree / "gc")
    around = await _register(tree.parent)

    assert inner["within"]["id"] == outer["id"]
    assert around["within"] is None
    from routes import libraries as routes

    listed = {lib["id"]: lib for lib in await routes.list_libraries()}
    assert [c["id"] for c in listed[outer["id"]]["contains"]] == [inner["id"]]
    assert {c["id"] for c in listed[around["id"]]["contains"]} == {outer["id"], inner["id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["parent", "child"])
async def test_romms_folder_may_be_set_around_or_inside_a_folder_library(
    db: Path, where: str,
) -> None:
    from routes import romm as romm_routes
    from services.romm import settings as romm_settings

    folder = db / "games" / "gc"
    (folder / "sub").mkdir(parents=True)
    await _register(folder, name="GameCube")
    root = {"parent": db / "games", "child": folder / "sub"}[where]

    await romm_routes.put_romm_settings(romm_routes.RommSettingsPatch(
        url="http://romm:8080", library_root=str(root),
    ))

    assert romm_settings.effective().get("library_root") == str(root)


# ---- who owns a file ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_nearest_root_owns_a_file(db: Path) -> None:
    from services import libraries

    library = db / "library"
    (library / "roms" / "gc").mkdir(parents=True)
    (library / "roms" / "ps2").mkdir(parents=True)
    await _romm_root(library)
    inner = await _register(library / "roms" / "gc")

    owner = libraries.library_for(str(library / "roms" / "gc" / "Melee.iso"))
    assert owner is not None and owner.id == inner["id"]
    assert libraries.library_for(str(library / "roms" / "ps2" / "Ico.iso")).id == "romm"


@pytest.mark.asyncio
async def test_output_in_a_folder_library_inside_romm_is_still_staged(db: Path) -> None:
    """RomM's scanner walks its whole folder, so an output written into a
    folder Library inside it is hidden while it is written (#398)."""
    from services import libraries
    from services.job_manager import job_manager

    library = db / "library"
    (library / "roms" / "gc").mkdir(parents=True)
    await _romm_root(library)
    await _register(library / "roms" / "gc")
    output = str(library / "roms" / "gc" / "Melee.rvz")

    assert libraries.hidden_staging(output) is True
    job = SimpleNamespace(_source_identity=None, output_path=output)
    assert await job_manager._needs_stage(job) is True
    assert libraries.hidden_staging(str(db / "elsewhere" / "Melee.rvz")) is False


# ---- nothing is swept twice ---------------------------------------------------


@pytest.mark.asyncio
async def test_an_outer_folder_library_skips_what_an_inner_one_owns(
    tree: Path, ready, queue,
) -> None:
    from routes import libraries as routes

    outer = await _register(tree)
    await routes.put_library_rules(outer["id"], {"rules": _RULES})
    # A walk kept from before the inner Library existed must not be reused.
    await routes.library_platforms(outer["id"])
    inner = await _register(tree / "gc")
    await routes.put_library_rules(inner["id"], {"rules": _RULES})

    await routes.run_library(outer["id"])
    by_outer = sorted(Path(p).name for p in queue)
    queue.clear()
    await routes.run_library(inner["id"])
    by_inner = sorted(Path(p).name for p in queue)

    assert by_outer == ["Ico.iso"]
    expected_inner = ["Melee.iso", "Sunshine.iso"]
    if _IS_ROOT:
        expected_inner = ["Locked.iso", *expected_inner]
    assert by_inner == expected_inner
    summary = await routes.library_platforms(outer["id"])
    assert "ngc" not in {p["id"] for p in summary["platforms"]}


@pytest.mark.asyncio
async def test_removing_the_inner_library_gives_its_files_back(
    tree: Path, ready, queue,
) -> None:
    from routes import libraries as routes

    outer = await _register(tree)
    await routes.put_library_rules(outer["id"], {"rules": _RULES})
    inner = await _register(tree / "gc")
    await routes.library_platforms(outer["id"])
    await routes.remove_library(inner["id"])

    await routes.run_library(outer["id"])

    assert "Melee.iso" in {Path(p).name for p in queue}


@pytest.mark.asyncio
async def test_romm_skips_the_roms_a_folder_library_inside_it_owns(db: Path, monkeypatch) -> None:
    from services.romm import client as romm_client_module
    from services.romm.library import romm_library

    library = db / "library"
    _gamecube(library / "roms" / "gc" / "Melee.iso")
    _ps2(library / "roms" / "ps2" / "Ico.iso")
    await _romm_root(library)
    await _register(library / "roms" / "gc")
    roms = [
        {"id": 1, "fs_name": "Melee.iso", "full_path": "roms/gc/Melee.iso"},
        {"id": 2, "fs_name": "Ico.iso", "full_path": "roms/ps2/Ico.iso"},
    ]
    monkeypatch.setattr(romm_client_module.romm_client, "roms", lambda _pid: roms)
    real_local_path = romm_client_module.romm_client.local_path
    mapped: list[str] = []

    def counting(rom):
        mapped.append(rom["fs_name"])
        return real_local_path(rom)

    monkeypatch.setattr(romm_client_module.romm_client, "local_path", counting)

    images = romm_library.images("1")

    # Codex PR #518: no ROM's path is worked out while listing; it is, lazily,
    # inside the bounded candidate probe, and the inner Library's is none.
    assert mapped == []
    paths = {i.name: i.path for i in images}
    assert paths["Melee.iso"] is None
    assert paths["Ico.iso"] == str((library / "roms" / "ps2" / "Ico.iso").resolve())


# ---- Platform mappings -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_folder_mapping_belongs_to_the_nearest_library(tree: Path) -> None:
    from services import platform_mappings
    from services.db import PlatformMappingRecord, SessionLocal

    await _register(tree)
    inner = await _register(tree / "gc")

    platform_mappings.set_mapping(str(tree / "gc"), "ngc")

    with SessionLocal() as session:
        rows = [(r.library_id, r.folder) for r in session.query(PlatformMappingRecord).all()]
    assert rows == [(inner["id"], "")]


@pytest.mark.asyncio
async def test_clearing_a_mapping_made_before_the_folder_became_a_library(tree: Path) -> None:
    """Codex PR #518: the mapping stays stored against the outer Library;
    clearing it through the inner one must still remove it."""
    from services import platform_mappings
    from services.db import PlatformMappingRecord, SessionLocal

    await _register(tree)
    platform_mappings.set_mapping(str(tree / "gc"), "ngc")
    await _register(tree / "gc")

    platform_mappings.set_mapping(str(tree / "gc"), None)

    with SessionLocal() as session:
        assert session.query(PlatformMappingRecord).count() == 0
    assert str((tree / "gc").resolve()) not in platform_mappings.mapped_folders()


@pytest.mark.asyncio
async def test_an_inner_library_registered_mid_sweep_stops_the_outer_queueing(
    tree: Path, ready, queue,
) -> None:
    """Codex PR #518: the outer sweep listed GameCube before ``gc`` became a
    Library; it must not queue those files under the outer Rules."""
    from routes import libraries as routes
    from services import libraries

    outer = await _register(tree)
    await routes.put_library_rules(outer["id"], {"rules": _RULES})
    library = libraries.get(outer["id"])
    listed = library.images

    def images_then_nest(platform_id):
        out = listed(platform_id)
        if platform_id == "ngc" and not any(
            lib.root == str((tree / "gc").resolve()) for lib in libraries.folder_libraries()
        ):
            libraries.register(str(tree / "gc"))
        return out

    library.images = images_then_nest

    result = await routes.run_library(outer["id"])

    assert not {"Melee.iso", "Sunshine.iso"} & {Path(p).name for p in queue}
    assert any(e["error"] == "libraries_changed" for e in result["errors"])


@pytest.mark.asyncio
async def test_the_queue_admits_a_batch_inside_its_admission(tmp_path) -> None:
    """CodeRabbit PR #518: the ownership check and the queueing are one step.
    The admission is entered before any job is registered and left after all
    are; one that refuses leaves nothing queued."""
    import contextlib

    from models import ConversionMode
    from services.job_manager import JobManager
    from services.rules.library import LibrariesChanged

    mgr = JobManager(max_concurrent=1, max_job_history=10)
    spec = [{"file_path": str(tmp_path / "a.iso"), "output_path": str(tmp_path / "a.chd")}]
    seen: list[int] = []

    @contextlib.contextmanager
    def recording():
        seen.append(len(mgr.jobs))
        yield
        seen.append(len(mgr.jobs))

    await mgr.create_jobs_atomic(spec, ConversionMode.CREATEDVD, admission=recording)
    assert seen == [0, 1]

    def refusing():
        raise LibrariesChanged

    other = JobManager(max_concurrent=1, max_job_history=10)
    with pytest.raises(LibrariesChanged):
        await other.create_jobs_atomic(spec, ConversionMode.CREATEDVD, admission=refusing)
    assert other.jobs == {}


@pytest.mark.asyncio
async def test_a_registration_waits_for_an_admission_in_progress(tree: Path) -> None:
    import threading

    from services import libraries
    from services.rules.library import LibrariesChanged

    await _register(tree)
    owned_at = libraries.ownership_generation()
    done = threading.Event()
    worker = threading.Thread(
        target=lambda: (libraries.register(str(tree / "gc")), done.set()),
    )
    with libraries.owned_as_listed(owned_at):
        worker.start()
        assert not done.wait(0.3)
        assert libraries.ownership_generation() == owned_at
    worker.join(10)

    assert done.is_set()
    with pytest.raises(LibrariesChanged), libraries.owned_as_listed(owned_at):
        pass


@pytest.mark.asyncio
async def test_registering_drops_every_cached_resolved_root(tree: Path) -> None:
    """CodeRabbit PR #518: an ancestor relinked after its root was cached
    must not hide a new inner Library from the outer walk."""
    from services import libraries

    with libraries._lock:
        libraries._canonical_roots["/stale/root"] = Path("/elsewhere")
    await _register(tree)

    assert "/stale/root" not in libraries._canonical_roots


@pytest.mark.asyncio
async def test_a_folder_inside_a_linked_romm_folder_is_seen_inside_it(db: Path) -> None:
    """CodeRabbit PR #518: RomM's root is a link; its resolved path is cached
    when the root is saved and on registration, so nesting is right before
    any scan."""
    from routes import romm as romm_routes

    real = db / "real"
    (real / "roms" / "gc").mkdir(parents=True)
    (db / "link").symlink_to(real)
    await romm_routes.put_romm_settings(romm_routes.RommSettingsPatch(
        url="http://romm:8080", library_root=str(db / "link"),
    ))

    created = await _register(real / "roms" / "gc")

    assert created["within"] == {"id": "romm", "name": "RomM"}


def test_the_catalogue_is_read_again_after_a_library_is_added_or_removed() -> None:
    """CodeRabbit PR #518: the outer Library's ``contains`` must not keep
    naming a removed Library."""
    import re

    root = Path(__file__).resolve().parents[1]
    store = (root / "src/lib/stores/libraries.svelte.js").read_text()
    for method in ("register", "remove"):
        body = re.search(rf"async {method}\(.*?\n  \}}\n", store, re.DOTALL).group(0)
        assert "this.load()" in body, method
    editor = (root / "src/lib/components/views/RuleEditor.svelte").read_text()
    assert "nothing was '\n      + 'queued for this platform" in editor


@pytest.mark.asyncio
async def test_removing_a_library_keeps_a_linked_romm_folders_nesting(db: Path) -> None:
    """Codex PR #551: removing any Library dropped RomM's resolved root, so
    a Library under its real path lost its ``within`` until a scan. Saving
    RomM's settings without a root did the same."""
    from routes import libraries as routes
    from routes import romm as romm_routes

    real = db / "real"
    (real / "roms" / "gc").mkdir(parents=True)
    (db / "other").mkdir()
    (db / "link").symlink_to(real)
    await romm_routes.put_romm_settings(romm_routes.RommSettingsPatch(
        url="http://romm:8080", library_root=str(db / "link"),
    ))
    inner = await _register(real / "roms" / "gc")
    other = await _register(db / "other")

    await routes.remove_library(other["id"])
    listed = {lib["id"]: lib for lib in await routes.list_libraries()}
    assert listed[inner["id"]]["within"] == {"id": "romm", "name": "RomM"}

    await romm_routes.put_romm_settings(romm_routes.RommSettingsPatch(url="http://romm:8081"))
    listed = {lib["id"]: lib for lib in await routes.list_libraries()}
    assert listed[inner["id"]]["within"] == {"id": "romm", "name": "RomM"}
