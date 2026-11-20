"""The containment gate resolves only the Volume roots it needs.

``_confined_real_path`` used to ``realpath()`` every configured Volume before it
matched a path, so one stalled mount blocked containment checks for files on
a healthy Volume (``GET /api/files`` among them). Now a path is matched against
the configured spellings with no filesystem call; only the matching root is
resolved, and the resolved spellings of the others are consulted only when no
configured spelling matches. Nothing is cached across calls: mounts change.
"""
from __future__ import annotations

import os
import time

import pytest
from utils import path_utils


@pytest.fixture(name="volumes")
def _volumes(tmp_path, monkeypatch):
    from config import settings

    healthy = tmp_path / "healthy"
    stalled = tmp_path / "stalled"
    (healthy / "games").mkdir(parents=True)
    stalled.mkdir()
    (healthy / "games" / "game.iso").write_bytes(b"x")
    monkeypatch.setattr(settings, "chd_volumes", f"{healthy},{stalled}")
    assert [os.path.normpath(v) for v in settings.volumes] == [str(healthy), str(stalled)]

    touched: list[str] = []
    real_realpath = os.path.realpath

    def guarded_realpath(path, *args, **kwargs):
        if os.fspath(path).startswith(str(stalled)):
            touched.append(os.fspath(path))
            raise OSError("this mount is not answering")
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", guarded_realpath)
    return {"healthy": healthy, "stalled": stalled, "touched": touched}


@pytest.mark.parametrize(
    "check",
    [
        lambda p: path_utils.is_within_configured_volumes(p),
        lambda p: path_utils.ensure_path_within_volumes(p) is not None,
    ],
    ids=["is_within", "ensure"],
)
def test_a_path_on_a_healthy_volume_never_touches_another(volumes, check):
    path = str(volumes["healthy"] / "games" / "game.iso")

    assert check(path)
    assert volumes["touched"] == []


def test_the_containing_root_never_touches_another_once_the_cache_is_built(volumes):
    # platform_boundary_root also weighs the cached alias spellings (a deeper
    # alias root can hold the path). Building that cache probes every root
    # once, bounded; at startup in production. Once built, finding the root of
    # a path on a healthy Volume touches no other Volume.
    path_utils.refresh_volume_roots()
    volumes["touched"].clear()

    assert path_utils.platform_boundary_root(
        str(volumes["healthy"] / "games" / "game.iso"),
    ) == volumes["healthy"].resolve()
    assert volumes["touched"] == []


def test_a_path_on_a_later_volume_never_touches_an_earlier_stalled_one(volumes, monkeypatch):
    from config import settings

    monkeypatch.setattr(
        settings, "chd_volumes", f"{volumes['stalled']},{volumes['healthy']}",
    )

    assert path_utils.is_within_configured_volumes(
        str(volumes["healthy"] / "games" / "game.iso"),
    )
    assert volumes["touched"] == []


def test_the_healthy_root_itself_never_touches_another(volumes):
    assert path_utils.is_within_configured_volumes(str(volumes["healthy"]))
    assert volumes["touched"] == []


def test_a_path_outside_every_volume_is_still_refused(volumes, tmp_path):
    (tmp_path / "elsewhere").mkdir()

    assert not path_utils.is_within_configured_volumes(str(tmp_path / "elsewhere"))


def test_a_volume_configured_through_a_symlink_still_matches_its_real_spelling(
    tmp_path, monkeypatch,
):
    from config import settings

    real = tmp_path / "real"
    (real / "games").mkdir(parents=True)
    (real / "games" / "game.iso").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", str(link))

    assert path_utils.is_within_configured_volumes(str(link / "games" / "game.iso"))
    # No configured spelling matches; the resolved one does.
    assert path_utils.is_within_configured_volumes(str(real / "games" / "game.iso"))


# --- Cached resolved root spellings -------------------
# The roots' resolved spellings are cached (trusted configuration, never a
# request path), each resolved on its own thread with its own bound. An alias
# then never waits on an unrelated stalled root, and a symlink from one
# Volume into another is accepted without resolving that Volume on the spot.

@pytest.fixture(name="stalled_first")
def _stalled_first(tmp_path, monkeypatch):
    import threading

    from config import settings

    stalled = tmp_path / "mnt" / "stalled"
    stalled.mkdir(parents=True)
    real = tmp_path / "data" / "healthy"
    (real / "games").mkdir(parents=True)
    (real / "games" / "game.iso").write_bytes(b"x")
    alias = tmp_path / "libraries" / "healthy"
    alias.parent.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", f"{stalled},{alias}")
    monkeypatch.setattr(path_utils, "ROOT_PROBE_SECONDS", 0.3)

    wake = threading.Event()
    real_realpath = os.path.realpath

    def stalling_realpath(path, *args, **kwargs):
        if os.fspath(path).startswith(str(stalled)):
            wake.wait(5)                     # a mount that is not answering
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", stalling_realpath)
    yield {"real": real, "alias": alias, "stalled": stalled, "tmp": tmp_path}
    wake.set()


def test_an_alias_validates_within_the_bound_despite_a_stalled_first_root(stalled_first):
    started = time.monotonic()

    ok = path_utils.is_within_configured_volumes(
        str(stalled_first["real"] / "games" / "game.iso"),
    )

    assert ok
    assert time.monotonic() - started < 1.5


def test_an_outside_path_is_still_refused_with_a_stalled_root(stalled_first):
    outside = stalled_first["tmp"] / "elsewhere"
    outside.mkdir()

    assert not path_utils.is_within_configured_volumes(str(outside))


def test_a_root_that_times_out_keeps_its_last_good_spelling(tmp_path, monkeypatch):
    import threading

    from config import settings

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", str(alias))
    path_utils.refresh_volume_roots()
    assert path_utils._resolved_root_spellings()[str(alias)] == str(real)

    wake = threading.Event()
    real_realpath = os.path.realpath

    def stalling_realpath(path, *args, **kwargs):
        if os.fspath(path).startswith(str(alias)):
            wake.wait(5)
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", stalling_realpath)
    try:
        path_utils.refresh_volume_roots(timeout=0.2)
        assert path_utils._resolved_root_spellings()[str(alias)] == str(real)
    finally:
        wake.set()


def test_a_symlink_from_one_volume_into_another_is_accepted(tmp_path, monkeypatch):
    from config import settings

    a, b = tmp_path / "share_a", tmp_path / "share_b"
    a.mkdir()
    (b / "games").mkdir(parents=True)
    (b / "games" / "game.iso").write_bytes(b"x")
    (a / "link").symlink_to(b / "games", target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.iso").write_bytes(b"x")
    (a / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", f"{a},{b}")

    assert path_utils.is_within_configured_volumes(str(a / "link" / "game.iso"))
    assert path_utils.platform_boundary_root(str(a / "link" / "game.iso")) == b.resolve()
    assert not path_utils.is_within_configured_volumes(str(a / "escape" / "secret.iso"))


# --- Cached spellings select; a fresh probe grants ---------
# A Volume configured as a symlink can be repointed. The cache would still
# cover the old target until it refreshes, and ensure_path_within_volumes
# guards delete_file. So a cached spelling only selects the root; access needs
# a fresh, bounded resolution of exactly that root, and a probe that times
# out refuses.

def test_a_repointed_volume_stops_covering_its_old_target_at_once(tmp_path, monkeypatch):
    from config import settings

    old, new = tmp_path / "old", tmp_path / "new"
    for target in (old, new):
        target.mkdir()
        (target / "game.iso").write_bytes(b"x")
    volume = tmp_path / "volume"
    volume.symlink_to(old, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    path_utils.refresh_volume_roots()
    assert path_utils.is_within_configured_volumes(str(old / "game.iso"))

    # Repoint the Volume; the cache still says "old" until it refreshes.
    volume.unlink()
    volume.symlink_to(new, target_is_directory=True)
    assert path_utils._resolved_root_spellings()[str(volume)] == str(old)

    assert not path_utils.is_within_configured_volumes(str(old / "game.iso"))
    with pytest.raises(ValueError):
        path_utils.ensure_path_within_volumes(str(old / "game.iso"))
    assert not path_utils.is_within_configured_volumes(str(old))   # the root itself
    assert path_utils.is_within_configured_volumes(str(volume / "game.iso"))


def test_a_fresh_probe_that_times_out_refuses(tmp_path, monkeypatch):
    import threading

    from config import settings

    root = tmp_path / "volume"
    root.mkdir()
    (root / "game.iso").write_bytes(b"x")
    monkeypatch.setattr(settings, "chd_volumes", str(root))
    monkeypatch.setattr(path_utils, "ROOT_PROBE_SECONDS", 0.3)
    wake = threading.Event()
    real_realpath = os.path.realpath

    def stalling_root(path, *args, **kwargs):
        if os.fspath(path) == str(root):          # only the root's own probe
            wake.wait(5)
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", stalling_root)
    started = time.monotonic()
    try:
        ok = path_utils.is_within_configured_volumes(str(root / "game.iso"))
    finally:
        wake.set()

    assert not ok                                   # fail closed, never the cache
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("order", ["outer-first", "inner-first"])
def test_nested_symlinked_volumes_give_the_most_specific_root(tmp_path, monkeypatch, order):
    # /outer -> /data/PS2 and /inner -> /data/PS2/library;
    # a path spelled through the targets belongs to the deepest Volume.
    from config import settings

    data = tmp_path / "data" / "PS2"
    (data / "library").mkdir(parents=True)
    (data / "library" / "game.iso").write_bytes(b"x")
    outer, inner = tmp_path / "outer", tmp_path / "inner"
    outer.symlink_to(data, target_is_directory=True)
    inner.symlink_to(data / "library", target_is_directory=True)
    volumes = [outer, inner] if order == "outer-first" else [inner, outer]
    monkeypatch.setattr(settings, "chd_volumes", ",".join(map(str, volumes)))

    root = path_utils.platform_boundary_root(str(data / "library" / "game.iso"))

    assert root == (data / "library").resolve()


def test_a_deeper_alias_root_wins_over_a_configured_spelling(tmp_path, monkeypatch):
    # /mnt matches by its configured spelling, and
    # /games -> /mnt/ps2/library by its alias. The alias root is deeper, so it
    # holds /mnt/ps2/library/game.iso, and the folder walk must stop there,
    # never reading "ps2" above it.
    from services import platform_detect

    from config import settings

    mnt = tmp_path / "mnt"
    library = mnt / "ps2" / "library"
    library.mkdir(parents=True)
    image = library / "game.iso"
    image.write_bytes(b"\0" * 4096)                  # headerless
    games = tmp_path / "games"
    games.symlink_to(library, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", f"{mnt},{games}")
    platform_detect.clear_cache()

    assert path_utils.platform_boundary_root(str(image)) == library.resolve()
    assert platform_detect.detect_platform(str(image)).platform is None
    platform_detect.clear_cache()


def test_refreshes_over_a_blocked_root_keep_one_live_probe(tmp_path, monkeypatch):
    # each refresh used to start its own probe thread,
    # so a mount that stays blocked accumulated one stuck thread per refresh.
    import threading

    from config import settings

    root = tmp_path / "blocked"
    root.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(root))
    wake = threading.Event()
    real_realpath = os.path.realpath

    def blocking_root(path, *args, **kwargs):
        if os.fspath(path) == str(root):
            wake.wait(10)
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", blocking_root)
    monkeypatch.setattr(path_utils, "ROOT_PROBE_SECONDS", 0.05)

    def live_probes() -> int:
        # Named per root by the registry; an unnamed probe thread (one started
        # outside it) shows up as "Thread-N (_resolve_root_into)".
        return sum(
            1 for t in threading.enumerate()
            if t.is_alive() and (
                t.name == f"volume-root-probe:{root}" or "_resolve_root_into" in t.name
            )
        )

    try:
        for _ in range(5):
            path_utils.refresh_volume_roots(timeout=0.05)
        path_utils.is_within_configured_volumes(str(root / "game.iso"))   # _fresh_root too
        assert live_probes() == 1
    finally:
        wake.set()


def test_a_probe_started_before_the_check_never_authorises(tmp_path, monkeypatch):
    # a probe resolved /vol to /old, then paused; /vol
    # is repointed to /new. A check that starts after that must not take the
    # paused probe's /old as a fresh observation.
    import threading

    from config import settings

    old, new = tmp_path / "old", tmp_path / "new"
    for target in (old, new):
        target.mkdir()
        (target / "game.iso").write_bytes(b"x")
    volume = tmp_path / "volume"
    volume.symlink_to(old, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    path_utils.refresh_volume_roots()            # cache: volume -> old
    monkeypatch.setattr(path_utils, "ROOT_PROBE_SECONDS", 3.0)

    hold = threading.Event()
    held_once = threading.Event()
    real_realpath = os.path.realpath

    def pausing_realpath(path, *args, **kwargs):
        answer = real_realpath(path, *args, **kwargs)
        if os.fspath(path) == str(volume) and not held_once.is_set():
            held_once.set()
            hold.wait(5)                          # resolved to /old, then paused
        return answer

    monkeypatch.setattr(os.path, "realpath", pausing_realpath)
    path_utils._root_probe(str(volume))          # the probe that will report /old
    assert held_once.wait(2)
    volume.unlink()
    volume.symlink_to(new, target_is_directory=True)
    threading.Timer(0.2, hold.set).start()       # it finishes during the check

    try:
        accepted = path_utils.is_within_configured_volumes(str(old / "game.iso"))
    finally:
        hold.set()

    assert not accepted
    assert path_utils.is_within_configured_volumes(str(volume / "game.iso"))
