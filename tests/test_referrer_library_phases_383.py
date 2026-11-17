"""The Referrer hook knows the Library, runs in phases, allows bounded async apply (#383).

Prep for opt-in and network Referrers (gamelist.xml #386, the RomM swap #391).
The ``.m3u`` Referrer keeps working exactly as before; its own tests cover that.
Here, test-only Referrers stand in for the ones to come: one opt-in per
Library, one in the network phase, one whose apply is a coroutine.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
from services import db as _db
from services import referrers
from services.referrers import Phase, Referrer, ReferrerChange, ReferrerError, ReferrerRegistry
from services.referrers.m3u import M3uReferrer

from app.models import (
    ConversionMode,
    DeletePlanRequest,
    DuplicateAction,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes

from .test_referrers_m3u import CONTAINER_ONLY
from .test_referrers_m3u import _volume as _m3u_volume

# The `volume` fixture of the .m3u job tests: a temp Volume with stubbed tools.
_FIXTURES = (_m3u_volume,)


class _Fake(Referrer):
    """A Referrer that points at every Image and logs what the hook asked of it."""

    def __init__(self, ident: str, log: list, *, opt_in: bool = False,
                 phase: Phase = Phase.FILE, fail: bool = False) -> None:
        self.id = ident
        self.label = ident
        self.opt_in = opt_in
        self.phase = phase
        self._log = log
        self._fail = fail

    def detect(self, image_path: str) -> bool:
        return True

    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        self._log.append(("plan", self.id))
        return [ReferrerChange(self.id, f"{self.id}-target", tuple(renames.items()))]

    def apply(self, change: ReferrerChange) -> None:
        self._log.append(("apply", self.id))
        if self._fail:
            raise ReferrerError(change.target, "refused")


class _AsyncFake(_Fake):
    """A Referrer whose apply is a coroutine (a network call, say)."""

    def __init__(self, ident: str, log: list, *, gate: asyncio.Event | None = None,
                 **kw) -> None:
        super().__init__(ident, log, **kw)
        self.gate = gate
        self.thread: int | None = None
        self.cancelled = False

    async def apply_async(self, change: ReferrerChange) -> None:
        self.thread = threading.get_ident()
        self._log.append(("apply", self.id))
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self._fail:
            raise ReferrerError(change.target, "refused")


class _Library:
    """The part of a Library the hook asks: whether an opt-in Referrer is on."""

    def __init__(self, enabled=(), broken: bool = False) -> None:
        self.id = "folder:test"
        self.name = "Test"
        self._enabled = set(enabled)
        self._broken = broken

    def referrer_enabled(self, referrer_id: str) -> bool:
        if self._broken:
            raise OSError("settings unreadable")
        return referrer_id in self._enabled


@pytest.fixture(name="fakes")
def _fakes(monkeypatch):
    """An empty registry the test fills, in place of the real one."""
    fresh = ReferrerRegistry()
    monkeypatch.setattr(referrers, "registry", fresh)
    return fresh


def _renames(tmp_path: Path) -> tuple[str, dict[str, str]]:
    old, new = tmp_path / "Game.iso", tmp_path / "Game.chd"
    old.write_bytes(b"iso")
    new.write_bytes(b"chd")
    return str(old), {str(old): str(new)}


# ---- declarations -----------------------------------------------------------


def test_m3u_is_always_on_and_in_the_file_phase():
    assert M3uReferrer.opt_in is False
    assert M3uReferrer.phase is Phase.FILE


def test_file_referrers_run_before_network_ones():
    assert Phase.FILE < Phase.NETWORK


def test_a_referrer_must_declare_its_phase_and_whether_it_is_opt_in():
    class Undeclared(Referrer):
        id = "undeclared"
        label = "undeclared"

        def detect(self, image_path):
            return False

        def plan(self, renames):
            return []

        def apply(self, change):
            return None

    with pytest.raises(TypeError):
        ReferrerRegistry().register(Undeclared())


# ---- opt-in per Library -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [None, _Library(), _Library(enabled={"other"})])
async def test_an_opt_in_referrer_stays_off_unless_the_library_turns_it_on(
    tmp_path, fakes, library,
):
    log: list = []
    fakes.register(_Fake("always", log))
    fakes.register(_Fake("optional", log, opt_in=True))
    image, renames = _renames(tmp_path)

    outcome = await referrers.update_before_source_removal(image, renames, library=library)
    planned = referrers.plan_changes(image, renames, library=library)

    assert outcome.ok
    assert [r["referrer"] for r in outcome.records] == ["always"]
    assert [c.referrer for c in planned] == ["always"]
    assert ("plan", "optional") not in log and ("apply", "optional") not in log


@pytest.mark.asyncio
async def test_an_opt_in_referrer_runs_where_the_library_turns_it_on(tmp_path, fakes):
    log: list = []
    fakes.register(_Fake("always", log))
    fakes.register(_Fake("optional", log, opt_in=True))
    image, renames = _renames(tmp_path)
    library = _Library(enabled={"optional"})

    outcome = await referrers.update_before_source_removal(image, renames, library=library)
    planned = referrers.plan_changes(image, renames, library=library)

    assert outcome.ok
    assert [(r["referrer"], r["status"]) for r in outcome.records] == [
        ("always", "updated"), ("optional", "updated"),
    ]
    assert [c.referrer for c in planned] == ["always", "optional"]


@pytest.mark.asyncio
async def test_a_library_whose_settings_cant_be_read_keeps_the_source(tmp_path, fakes):
    """Fail closed: not knowing whether an opt-in Referrer is on is a failure."""
    log: list = []
    fakes.register(_Fake("always", log))
    fakes.register(_Fake("optional", log, opt_in=True))
    image, renames = _renames(tmp_path)
    library = _Library(broken=True)

    outcome = await referrers.update_before_source_removal(image, renames, library=library)

    assert not outcome.ok
    assert outcome.kept_message.startswith("converted, source kept: ")
    assert ("apply", "always") not in log
    with pytest.raises(ReferrerError):
        referrers.plan_changes(image, renames, library=library)


def test_library_defaults_leave_every_opt_in_referrer_off(tmp_path):
    from services.libraries.folder import FolderLibrary
    from services.romm.library import romm_library

    folder = FolderLibrary(record_id=1, root=str(tmp_path), name="Roms")
    assert folder.referrer_enabled("gamelist") is False
    assert romm_library.referrer_enabled("romm_swap") is False


# ---- phases -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_phases_run_in_order_whatever_the_registration_order(tmp_path, fakes):
    log: list = []
    fakes.register(_Fake("network", log, phase=Phase.NETWORK))
    fakes.register(_Fake("file", log))
    image, renames = _renames(tmp_path)

    outcome = await referrers.update_before_source_removal(image, renames)

    assert outcome.ok
    assert log == [("plan", "file"), ("apply", "file"), ("plan", "network"), ("apply", "network")]
    assert [c.referrer for c in referrers.plan_changes(image, renames)] == ["file", "network"]


@pytest.mark.asyncio
async def test_a_later_phase_never_runs_after_an_earlier_one_failed(tmp_path, fakes):
    log: list = []
    fakes.register(_Fake("network", log, phase=Phase.NETWORK))
    fakes.register(_Fake("file", log, fail=True))
    image, renames = _renames(tmp_path)

    outcome = await referrers.update_before_source_removal(image, renames)

    assert not outcome.ok
    assert outcome.failed == "file-target" and outcome.reason == "refused"
    assert ("plan", "network") not in log and ("apply", "network") not in log
    assert [(r["referrer"], r["status"]) for r in outcome.records] == [
        ("file", "failed"), ("network", "skipped"),
    ]
    # The Source and the output are both still there: the hook removes nothing.
    assert all(Path(p).exists() for p in (image, *renames.values()))


# ---- async apply ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_async_apply_runs_on_the_loop_after_the_file_phase(tmp_path, fakes):
    log: list = []
    network = _AsyncFake("network", log, phase=Phase.NETWORK)
    fakes.register(network)
    fakes.register(_Fake("file", log))
    image, renames = _renames(tmp_path)

    outcome = await referrers.update_before_source_removal(image, renames)

    assert outcome.ok
    assert [(r["referrer"], r["status"]) for r in outcome.records] == [
        ("file", "updated"), ("network", "updated"),
    ]
    assert log[-1] == ("apply", "network")
    assert network.thread == threading.get_ident()


@pytest.mark.asyncio
async def test_apply_async_defaults_to_the_sync_apply_off_the_loop(tmp_path):
    """#391: ``apply`` stays synchronous on every Referrer; ``apply_async`` is
    the async hook, and by default it runs ``apply`` on a worker thread."""
    import inspect

    from services.referrers import Referrer

    log: list = []
    plain = _Fake("plain", log)
    seen: list[int] = []
    plain.apply = lambda change: seen.append(threading.get_ident())
    _image, renames = _renames(tmp_path)
    change = plain.plan(renames)[0]

    assert not inspect.iscoroutinefunction(Referrer.apply)
    assert inspect.iscoroutinefunction(Referrer.apply_async)
    await plain.apply_async(change)
    assert seen and seen[0] != threading.get_ident()
    for referrer in referrers.registry.all():
        assert not inspect.iscoroutinefunction(referrer.apply), referrer.id


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [Phase.FILE, Phase.NETWORK])
async def test_a_sync_applys_removed_source_reaches_the_outcome(tmp_path, fakes, phase):
    """CodeRabbit review 5930768363: what a synchronous ``apply`` returns
    (``Applied``) is kept through its phase and into the hook's outcome, as
    an async one's is."""
    from services.referrers import Applied

    log: list = []
    image, renames = _renames(tmp_path)

    class _Removing(_Fake):
        def apply(self, change):
            super().apply(change)
            return Applied(removed=(image,))

    fakes.register(_Removing("removing", log, phase=phase))

    outcome = await referrers.update_before_source_removal(image, renames)

    assert outcome.ok
    assert outcome.removed == [image]


@pytest.mark.asyncio
async def test_a_failed_async_apply_keeps_the_source(tmp_path, fakes):
    log: list = []
    fakes.register(_AsyncFake("network", log, phase=Phase.NETWORK, fail=True))
    image, renames = _renames(tmp_path)

    outcome = await referrers.update_before_source_removal(image, renames)

    assert not outcome.ok
    assert outcome.reason == "refused"


@pytest.mark.asyncio
async def test_a_hanging_async_apply_is_bounded_and_never_holds_the_loop(tmp_path, fakes):
    log: list = []
    network = _AsyncFake("network", log, phase=Phase.NETWORK, gate=asyncio.Event())
    fakes.register(network)
    image, renames = _renames(tmp_path)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticking = asyncio.create_task(ticker())
    try:
        outcome = await asyncio.wait_for(
            referrers.update_before_source_removal(image, renames, bound=0.3), 5,
        )
    finally:
        ticking.cancel()

    assert not outcome.ok
    assert "did not answer" in outcome.reason
    assert ticks > 5
    await asyncio.sleep(0)
    assert network.cancelled


@pytest.mark.asyncio
async def test_a_cancel_stops_an_async_apply_at_once(tmp_path, fakes):
    log: list = []
    network = _AsyncFake("network", log, phase=Phase.NETWORK, gate=asyncio.Event())
    fakes.register(network)
    image, renames = _renames(tmp_path)
    cancel = asyncio.Event()

    hook = asyncio.create_task(
        referrers.update_before_source_removal(image, renames, cancel_event=cancel),
    )
    while ("apply", "network") not in log:
        await asyncio.sleep(0.01)
    cancel.set()
    outcome = await asyncio.wait_for(hook, 5)

    assert not outcome.ok
    assert outcome.reason == "cancelled"
    await asyncio.sleep(0)
    assert network.cancelled


# ---- the Image's Library ----------------------------------------------------


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path):
    from services import libraries
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    yield
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def test_library_for_finds_the_library_that_holds_a_path(tmp_path, db, monkeypatch):
    from services import libraries
    from services.romm import client as romm_client_module
    from services.romm.library import romm_library

    from config import settings as app_settings

    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    (tmp_path / "roms" / "ps2").mkdir(parents=True)
    (tmp_path / "romm").mkdir()
    (tmp_path / "loose").mkdir()
    folder = libraries.register(str(tmp_path / "roms"))
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str((tmp_path / "romm").resolve())),
    )

    assert libraries.library_for(str(tmp_path / "roms" / "ps2" / "Ico.iso")).id == folder.id
    assert libraries.library_for(str(tmp_path / "romm" / "Game.iso")) is romm_library
    assert libraries.library_for(str(tmp_path / "loose" / "Game.iso")) is None


@pytest.mark.asyncio
async def test_the_delete_confirmation_and_the_job_pass_the_image_library(
    volume, db, monkeypatch,
):
    from services import libraries

    library = libraries.register(str(volume))
    seen: dict[str, object] = {}
    real_plan, real_update = referrers.plan_changes_bounded, referrers.update_before_source_removal

    async def plan(image_path, renames, library=None):
        seen["plan"] = library
        return await real_plan(image_path, renames, library)

    async def update(image_path, renames, **kw):
        seen["hook"] = kw.get("library")
        return await real_update(image_path, renames, **kw)

    monkeypatch.setattr(referrers, "plan_changes_bounded", plan)
    monkeypatch.setattr(referrers, "update_before_source_removal", update)
    source = str(volume / "Game (Disc 1).cue")

    plan_result = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[source], mode=ConversionMode.CREATECD,
    ))
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=source, mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=CONTAINER_ONLY,
    ))
    await convert_routes.job_manager._process_job(job.id)

    assert plan_result["items"][0]["referrer_changes"]
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert not Path(source).exists()
    assert seen["plan"].id == library.id
    assert seen["hook"].id == library.id


@pytest.mark.asyncio
async def test_a_job_that_cant_tell_the_image_library_keeps_the_source(
    volume, db, monkeypatch,
):
    """Fail closed: without the Library the hook can't tell which opt-in
    Referrers apply, so nothing is deleted."""
    from services import libraries

    def unreadable(_path):
        raise OSError("the Library list can't be read")

    monkeypatch.setattr(libraries, "library_for", unreadable)
    source = volume / "Game (Disc 1).cue"

    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=CONTAINER_ONLY,
    ))
    await convert_routes.job_manager._process_job(job.id)

    assert source.is_file() and (volume / "Game (Disc 1).bin").is_file()
    assert (volume / "Game (Disc 1).chd").is_file()
    assert job.deleted_paths == []
    assert "Library" in (job.error_message or job.message or "")


# ---- review fixes (#395) ----------------------------------------------------


def test_a_symlinked_romm_root_still_maps_its_images_to_romm(tmp_path, db, monkeypatch):
    """codex 4588646131: the Image path is resolved, so the root must be too."""
    from services import libraries
    from services.romm import client as romm_client_module
    from services.romm.library import romm_library

    real = tmp_path / "romm-real"
    real.mkdir()
    link = tmp_path / "romm-link"
    link.symlink_to(real)
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str(link)),
    )

    assert libraries.library_for(str(link / "ps2" / "Ico.iso")) is romm_library
    assert libraries.library_for(str(real / "ps2" / "Ico.iso")) is romm_library


@pytest.mark.asyncio
async def test_the_canonical_romm_root_is_kept_until_romm_settings_are_saved(
    tmp_path, db, monkeypatch,
):
    from services import libraries
    from services.romm import client as romm_client_module
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "romm-link"
    link.symlink_to(first)
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str(link)),
    )
    assert libraries.library_for(str(first / "Game.iso")) is romm_library
    link.unlink()
    link.symlink_to(second)

    # Not resolved again on every lookup...
    assert libraries.library_for(str(first / "Game.iso")) is romm_library
    # ...but a save of RomM's settings refreshes it.
    await romm_settings.save({})
    assert libraries.library_for(str(first / "Game.iso")) is None
    assert libraries.library_for(str(second / "Game.iso")) is romm_library


class _CheckingAsyncFake(_AsyncFake):
    """Awaits, then checks the write guard before writing, as a real one would."""

    async def apply_async(self, change: ReferrerChange) -> None:
        from services.referrers.base import check_write

        self._log.append(("apply", self.id))
        await self.gate.wait()
        check_write(change.target)
        self._log.append(("wrote", self.id))


@pytest.mark.asyncio
async def test_an_async_apply_keeps_the_write_guard(tmp_path, fakes):
    """codex 4588646144: an output replaced during the await is refused."""
    log: list = []
    network = _CheckingAsyncFake("network", log, phase=Phase.NETWORK, gate=asyncio.Event())
    fakes.register(network)
    image, renames = _renames(tmp_path)
    output = Path(next(iter(renames.values())))

    hook = asyncio.create_task(referrers.update_before_source_removal(image, renames))
    while ("apply", "network") not in log:
        await asyncio.sleep(0.01)
    output.unlink()
    output.write_bytes(b"another disc altogether")
    network.gate.set()
    outcome = await asyncio.wait_for(hook, 5)

    assert not outcome.ok
    assert "changed after its check" in outcome.reason
    assert ("wrote", "network") not in log


class _SlowCleanupFake(_AsyncFake):
    """Never answers; its cleanup takes a while, then fails."""

    def __init__(self, *a, cleanup: asyncio.Event, **kw) -> None:
        super().__init__(*a, **kw)
        self.cleanup = cleanup

    async def apply_async(self, change: ReferrerChange) -> None:
        self._log.append(("apply", self.id))
        try:
            await asyncio.Event().wait()
        finally:
            await self.cleanup.wait()
            raise RuntimeError("cleanup broke")


@pytest.mark.asyncio
async def test_an_apply_still_cleaning_up_holds_the_caller_locks_until_it_ends(
    tmp_path, fakes, monkeypatch, caplog,
):
    """codex 4588646155: a cancelled apply is waited for briefly; one still
    running is abandoned, keeps the Source, and owns the holds until it ends."""
    monkeypatch.setattr(referrers, "CANCEL_GRACE_SECONDS", 0.05)
    log: list = []
    cleanup = asyncio.Event()
    fakes.register(_SlowCleanupFake("network", log, phase=Phase.NETWORK, cleanup=cleanup))
    image, renames = _renames(tmp_path)
    released = asyncio.Event()

    outcome = await referrers.update_before_source_removal(image, renames, bound=0.1)
    outcome.outlive(released.set)

    assert not outcome.ok
    assert [r["status"] for r in outcome.records] == ["abandoned"]
    await asyncio.sleep(0.1)
    assert not released.is_set()
    cleanup.set()
    await asyncio.wait_for(released.wait(), 5)
    await asyncio.sleep(0)
    assert "cleanup broke" in caplog.text


@pytest.mark.asyncio
async def test_a_job_keeps_its_holds_while_an_abandoned_apply_cleans_up(
    volume, fakes, monkeypatch,
):
    from services.lock_manager import lock_manager

    monkeypatch.setattr(referrers, "CANCEL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(referrers, "conversion_stall_bound", lambda: 0.1)
    log: list = []
    cleanup = asyncio.Event()
    fakes.register(_SlowCleanupFake("network", log, phase=Phase.NETWORK, cleanup=cleanup))
    source = volume / "Game (Disc 1).cue"

    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
        container_only=CONTAINER_ONLY,
    ))
    await convert_routes.job_manager._process_job(job.id)

    assert source.is_file()
    assert job.deleted_paths == []
    assert lock_manager.is_locked(str(source))
    cleanup.set()
    for _ in range(200):
        if not lock_manager.is_locked(str(source)):
            break
        await asyncio.sleep(0.02)
    assert not lock_manager.is_locked(str(source))


@pytest.mark.asyncio
async def test_cancelling_the_hook_mid_phase_stops_the_worker_writing(tmp_path, fakes):
    """CodeRabbit 4588773569: a cancelled awaiter must abandon the thread."""
    from services.referrers.base import check_write

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    wrote: list = []

    class Stuck(_Fake):
        def apply(self, change):
            entered.set()
            release.wait(10)
            try:
                check_write(change.target)
                wrote.append(change.target)
            finally:
                finished.set()

    fakes.register(Stuck("file", []))
    image, renames = _renames(tmp_path)

    hook = asyncio.create_task(referrers.update_before_source_removal(image, renames))
    assert await asyncio.to_thread(entered.wait, 5)
    hook.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hook
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)

    assert wrote == []
