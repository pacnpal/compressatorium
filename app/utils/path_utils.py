from __future__ import annotations

import ctypes
import errno
import functools
import hashlib
import os
import secrets
import stat
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple, Optional

from config import settings


def strip_archive_path(path: str) -> str:
    """Return the filesystem portion of an archive path (archive::internal)."""
    return path.split("::", 1)[0] if "::" in path else path


def is_special_file(path: str | os.PathLike) -> bool:
    """Whether ``path`` exists and is neither a regular file nor a directory.

    A FIFO, socket or device. Opening a FIFO with no writer blocks forever, so
    a reader that must not hang (a header probe) asks this first. ``lstat``
    first; a symlink is judged by its target. False for a missing path or a
    dangling link: nothing there can block an open.
    """
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            st = os.stat(path)
    except OSError:
        return False
    return not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))


# errnos meaning "this filesystem has no hard links" (FAT/exFAT, some network
# shares), as opposed to a real failure of the link itself.
_NO_HARDLINK_ERRNOS = frozenset({
    errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK,
})


def place_without_clobbering(src: str, dst: str) -> None:
    """Move ``src`` to ``dst``, never replacing anything already at ``dst``.

    ``os.replace`` silently overwrites, and a move can block on a stuck mount
    and complete long after its caller gave up, replacing whatever has appeared
    at ``dst`` by then. Every path here is an atomic no-replace operation, so a
    file at ``dst`` raises ``FileExistsError`` and is never touched, however
    late the move lands, and nothing is created at ``dst`` before it succeeds:

    1. A hard link, then removing ``src``. Most filesystems.
    2. Without hard links (FAT/exFAT), the kernel's no-replace rename:
       ``renameat2(RENAME_NOREPLACE)`` on Linux, ``renamex_np(RENAME_EXCL)``
       on macOS.
    3. Neither available: refuse with ``OSError(ENOTSUP)`` rather than fall
       back to a check-then-rename that could overwrite another file.

    Both paths must be on one filesystem, as for ``os.replace``.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _NO_HARDLINK_ERRNOS:
            raise
        if _rename_noreplace(src, dst):
            return
        raise OSError(
            errno.ENOTSUP,
            "the output filesystem supports neither hard links nor an atomic "
            "no-replace rename, so the file can't be placed without risking "
            "overwriting another one",
            dst,
        ) from exc
    # The move is complete once the link exists. Failing to remove the source
    # (already swept by the caller's cleanup while the link was stuck, or an
    # I/O error) must not be reported as a failed placement: the product is at
    # dst, and a caller told otherwise would leave it orphaned there. A stray
    # source sits in the caller's work dir, which the caller sweeps anyway.
    try:
        os.unlink(src)
    except OSError:
        pass


# errnos meaning "this kernel/filesystem has no no-replace rename", as opposed
# to a real failure of the rename itself.
_NO_NOREPLACE_ERRNOS = frozenset({
    errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP,
})
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1  # Linux <linux/fs.h>
_RENAME_EXCL = 0x00000004  # macOS <stdio.h>


@functools.lru_cache(maxsize=1)
def _noreplace_rename_call():
    """The platform's no-replace rename as ``f(src_bytes, dst_bytes) -> int``,
    or ``None`` where libc has none."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if sys.platform.startswith("linux"):
        fn = getattr(libc, "renameat2", None)
        if fn is None:
            return None
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
        fn.restype = ctypes.c_int
        return lambda a, b: fn(_AT_FDCWD, a, _AT_FDCWD, b, _RENAME_NOREPLACE)
    if sys.platform == "darwin":
        fn = getattr(libc, "renamex_np", None)
        if fn is None:
            return None
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return lambda a, b: fn(a, b, _RENAME_EXCL)
    return None


def _rename_noreplace(src: str, dst: str) -> bool:
    """Atomically rename ``src`` to ``dst`` unless ``dst`` exists.

    Returns ``False`` when no such rename exists here (no libc call, or the
    kernel/filesystem rejects the flag); raises ``FileExistsError`` if ``dst``
    exists and ``OSError`` for any other failure.
    """
    call = _noreplace_rename_call()
    if call is None:
        return False
    if call(os.fsencode(src), os.fsencode(dst)) == 0:
        return True
    err = ctypes.get_errno()
    if err in _NO_NOREPLACE_ERRNOS:
        return False
    raise OSError(err, os.strerror(err), src, None, dst)


def is_same_file(path: str, ours: os.stat_result) -> bool:
    """True when ``path`` is still the file ``ours`` describes (device + inode)."""
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (ours.st_dev, ours.st_ino)


def remove_if_same_file(path: str, ours: os.stat_result) -> None:
    """Unlink ``path`` only if it is still the file ``ours`` describes.

    For cleanup that must never remove something another process put at the
    same name in the meantime. POSIX has no atomic "unlink if this inode", so
    a replacement in the instant between the check and the unlink can't be
    excluded; everything slower than that is. Errors are ignored: this is
    best-effort cleanup.
    """
    if is_same_file(path, ours):
        try:
            os.unlink(path)
        except OSError:
            pass


def path_fingerprint(path: str) -> str:
    """``"size:mtime_ns"`` for whatever is at *path*, or ``""`` if nothing is.

    Cheap enough to take on every recorded row (one stat), and precise enough
    for the only question asked of it: is the file here still the one that was
    here before the conversion ran? Not a content hash -- it never needs to be,
    because a converter that rewrites a path always changes both fields, and a
    false "changed" only costs one hash that then fails to match.
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


def match_extension(name: str, extensions: Iterable[str]) -> Optional[str]:
    """Return the longest declared extension ``name`` ends with, else ``None``.

    The shared primitive behind every "does this tool handle this file?"
    check (``input_extensions`` / ``verify_extensions`` / the archive-member
    gate). It is a **suffix** match rather than ``Path(name).suffix``
    equality so a tool can declare a *compound* extension: nkit2iso's sources
    are ``.nkit.iso`` / ``.nkit.gcz``, whose single ``Path.suffix`` is the
    generic ``.iso`` / ``.gcz`` that CHDMAN and Dolphin already own. Matching
    on the full suffix lets the specific format be declared without either
    tool having to claim (or disclaim) the generic one.

    For the single-component extensions every other tool declares this is
    exactly the old behaviour: ``game.iso`` ends with ``.iso`` and nothing
    else. Longest-match resolution means a name that matches both a compound
    and its generic tail reports the compound one, so a caller that needs the
    concrete key (the archive listing) gets the most specific answer.

    ``name`` may be a full path, a bare filename, or an extension string
    (``".nkit.iso"`` still ends with ``".iso"``), so callers holding only a
    member's extension can use the same helper.
    """
    lower = name.lower()
    best: Optional[str] = None
    for ext in extensions:
        if lower.endswith(ext) and (best is None or len(ext) > len(best)):
            best = ext
    return best


# Per thread: how many times the containment gate (`_confined_real_path` and
# its helpers) gave up because of an error rather than a verdict: a failed
# or timed-out root probe, an OSError resolving the path, a root missing from
# the resolved-spelling cache. `volume_containment` turns a refusal that
# happened alongside one of these into "couldn't tell" instead of "outside".
_unknown_failures = threading.local()


def _note_unknown() -> None:
    _unknown_failures.count = getattr(_unknown_failures, "count", 0) + 1


def _unknown_count() -> int:
    return getattr(_unknown_failures, "count", 0)


def _resolve_path(raw_path: str, *, strict: bool = False) -> Optional[Path]:
    """Resolve a path that ``_confined_real_path`` already confined.

    Only the gate calls this with a request-supplied value; it is not a
    containment check on its own.

    Explicitly rejects symlink loops: prior to Python 3.13, ``Path.resolve()``
    raised :class:`RuntimeError` for an infinite-loop symlink, which this
    helper caught and treated as "cannot be safely resolved".  Python 3.13+
    instead returns the path unchanged from ``resolve()``, which would let a
    dangling loop slip past the volume-containment check as if it were a real
    file.  We restore the pre-3.13 semantics by probing the path with
    ``os.stat`` (follows symlinks), ELOOP surfaces as ``OSError`` and we
    return ``None`` so the caller rejects the path.
    """
    try:
        path_obj = Path(raw_path).expanduser()
        resolved = path_obj.resolve(strict=strict)
    except RuntimeError:
        return None                  # a symlink loop (before 3.13): a verdict
    except OSError as exc:
        if exc.errno != errno.ELOOP and not (strict and exc.errno == errno.ENOENT):
            _note_unknown()          # EIO, a stalled mount, ...: couldn't tell
        return None

    # Additional ELOOP probe for Python 3.13+ where ``resolve()`` no longer
    # raises for symlink loops.
    try:
        os.stat(resolved)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        # ENOENT / EACCES / other, not necessarily a security issue; fall
        # through and let the caller decide.  strict=False callers expect
        # non-existent paths to be resolvable (used for "would this output
        # path be valid?" checks).
    return resolved


def _resolve_volume(volume_path: str) -> Optional[Path]:
    try:
        return Path(volume_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _configured_root(volume: str) -> str:
    """A Volume root as configured, made absolute; no filesystem call."""
    return os.path.normpath(os.path.abspath(os.path.expanduser(volume)))


# --- Resolved spellings of the configured Volume roots ------------------------
# The roots come from the deployment, never from a request, so their resolved
# spellings can be cached: a request then never waits on another Volume's
# stalled mount to match an alias (a root configured through a symlink, the
# path spelled through its target) or to accept a symlink from one Volume
# into another. Each root is resolved on its own daemon thread with its own
# bound; one that does not answer keeps its last good spelling, or none. The
# cache is rebuilt (bounded) when the configured Volumes change and refreshed
# in the background when it is older than `ROOT_REFRESH_SECONDS`. Request
# paths are never cached: the final check always resolves the path itself.
ROOT_PROBE_SECONDS = 2.0
ROOT_REFRESH_SECONDS = 60.0
_root_lock = threading.Lock()
_root_spellings: dict[str, str] = {}        # configured root -> resolved root
_root_spellings_for: tuple[str, ...] | None = None
_root_spellings_at = 0.0
_root_refreshing = False


def _resolve_root_into(configured: str, box: dict) -> None:
    try:
        box["real"] = str(Path(configured).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        box["failed"] = True


_probe_lock = threading.Lock()
_probes_in_flight: dict[str, tuple[threading.Thread, dict, float]] = {}


def _root_probe(configured: str) -> tuple[threading.Thread, dict, float]:
    """The live probe resolving ``configured``, started if none is running.

    Returns ``(thread, box, started_at)``, ``started_at`` on the monotonic
    clock, so a caller can tell a probe that began before it from one that
    began after. One registry for every resolution of a root (cache
    refreshes and ``_fresh_root``): there is never more than one live probe
    per root, so a mount that stays blocked strands one thread, however many
    refreshes and requests ask. A finished probe is never reused; its answer
    may be stale.
    """
    with _probe_lock:
        entry = _probes_in_flight.get(configured)
        if entry is None or not entry[0].is_alive():
            box: dict = {}
            thread = threading.Thread(
                target=_resolve_root_into, args=(configured, box), daemon=True,
                name=f"volume-root-probe:{configured}",
            )
            entry = (thread, box, time.monotonic())
            _probes_in_flight[configured] = entry
            thread.start()
        return entry


def refresh_volume_roots(*, timeout: float | None = None) -> None:
    """Re-resolve every configured root, each bounded on its own thread.

    All roots start together, and each gets ``timeout`` (default
    ``ROOT_PROBE_SECONDS``) from that start: a stalled root holds up neither
    the others nor the caller past the bound. A root that times out keeps its
    last good spelling; one that fails to resolve has none.
    """
    global _root_spellings, _root_spellings_for, _root_spellings_at
    bound = ROOT_PROBE_SECONDS if timeout is None else timeout
    volumes = tuple(settings.volumes)
    probes = []
    for volume in volumes:
        configured = _configured_root(volume)
        # A probe already running may have started before this refresh; the
        # cache only selects roots, so its answer is good enough here.
        thread, box, _started = _root_probe(configured)
        probes.append((configured, thread, box))
    deadline = time.monotonic() + bound
    for _configured, thread, _box in probes:
        thread.join(max(0.0, deadline - time.monotonic()))
    with _root_lock:
        previous = _root_spellings
        fresh: dict[str, str] = {}
        for configured, _thread, box in probes:
            if "real" in box:
                fresh[configured] = box["real"]
            elif "failed" not in box and configured in previous:
                fresh[configured] = previous[configured]   # timed out: last good
        _root_spellings = fresh
        _root_spellings_for = volumes
        _root_spellings_at = time.monotonic()


def _refresh_in_background() -> None:
    global _root_refreshing
    try:
        refresh_volume_roots()
    finally:
        with _root_lock:
            _root_refreshing = False


def _resolved_root_spellings() -> dict[str, str]:
    """The cached resolved spelling of each configured root.

    Rebuilt, bounded, when the configured Volumes changed since the last
    build; refreshed in the background when merely old.
    """
    global _root_refreshing
    volumes = tuple(settings.volumes)
    with _root_lock:
        current = _root_spellings_for == volumes
        stale = time.monotonic() - _root_spellings_at > ROOT_REFRESH_SECONDS
        spawn = current and stale and not _root_refreshing
        if spawn:
            _root_refreshing = True
    if not current:
        refresh_volume_roots()
    elif spawn:
        threading.Thread(target=_refresh_in_background, daemon=True).start()
    with _root_lock:
        return dict(_root_spellings)


def _lexically_under(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep)


def _matching_volumes(candidate: str) -> list[str]:
    """Every Volume whose root spells a prefix of ``candidate``: candidates
    for the most specific holder (``platform_boundary_root``).

    Both kinds of spelling, merged: the configured ones (no filesystem call)
    and the roots' cached resolved spellings (a root configured through a
    symlink, the path spelled through its target; ``_resolved_root_spellings``,
    which never waits on a stalled mount). A configured match must not hide
    a deeper alias: with ``/mnt`` configured and ``/games -> /mnt/ps2/library``,
    ``/mnt/ps2/library/game.iso`` belongs to ``/games``. Nested aliases all
    match too. Both spellings are trusted (they come from the deployment, not
    a request); the caller's fresh, bounded check of each candidate decides.
    """
    volumes = list(settings.volumes)
    resolved_roots = _resolved_root_spellings()
    matching = []
    for volume in volumes:
        configured = _configured_root(volume)
        alias = resolved_roots.get(configured)
        if _lexically_under(candidate, configured) or (
            alias is not None and alias != configured and _lexically_under(candidate, alias)
        ):
            matching.append(volume)
    return matching


def _has_parent_segment(raw_path: str) -> bool:
    """Whether ``raw_path`` has a ``..`` segment (split on ``os.sep`` and ``/``)."""
    return ".." in raw_path.replace(os.sep, "/").split("/")


def _contains(real_volume: Path | None, real_path: Path) -> bool:
    return real_volume is not None and (
        real_path == real_volume or real_volume in real_path.parents
    )


def _real_inside_volumes(
    path: str, *, roots: Sequence[str], strict: bool,
) -> Path | None:
    """``path`` resolved on disk (symlinks followed), if that is inside a Volume.

    Granting needs a fresh resolution: see ``_holding_roots``.
    """
    real_path = _resolve_path(path, strict=strict)
    if real_path is None:
        return None
    return real_path if _holding_roots(real_path, roots) else None


def _holding_roots(real_path: Path, matched: Sequence[str]) -> list[Path]:
    """The freshly resolved Volume roots that hold ``real_path``.

    The cached spellings (``_resolved_root_spellings``) only *select* a root;
    access is granted against a fresh, bounded resolution of exactly that root
    (``_fresh_root``), so a Volume whose symlink was repointed stops covering
    its old target at once, and a probe that times out refuses. ``matched``
    are the Volumes the gate matched the path under; they are resolved first.
    A real path outside them (a symlink from one Volume into another, as
    Unraid shares do) selects, by cached spelling, the Volume that may hold
    it, and only that one is resolved. No unrelated root is ever resolved, so
    a stalled one is never waited on.
    """
    matched_roots = [_configured_root(volume) for volume in matched]
    holders = [
        root for root in map(_fresh_root_noted, matched_roots) if _contains(root, real_path)
    ]
    if holders:
        return holders
    for configured, cached in _resolved_root_spellings().items():
        if configured in matched_roots or not _contains(Path(cached), real_path):
            continue
        fresh = _fresh_root_noted(configured)
        if _contains(fresh, real_path):
            holders.append(fresh)
    return holders


def _fresh_root(configured: str) -> Path | None:
    """``configured`` resolved now, within ``ROOT_PROBE_SECONDS``; None otherwise.

    Fails closed: a probe that times out or fails gives None, never the
    cached spelling. Only a resolution that *started* at or after this check
    began authorises: a probe already running may have resolved the root
    before it was repointed. Through the per-root registry (``_root_probe``),
    so one live probe per root still holds: a check that finds an older probe
    running waits for it to finish, then starts its own, all within one
    ``ROOT_PROBE_SECONDS`` budget. The older probe's answer is never used.
    """
    check_started = time.monotonic()
    deadline = check_started + ROOT_PROBE_SECONDS
    thread, box, probe_started = _root_probe(configured)
    if probe_started < check_started:
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            return None              # the older probe is stuck: fail closed
        # The older probe is done, so the next one began after this check did.
        thread, box, probe_started = _root_probe(configured)
        if probe_started < check_started:
            return None
    thread.join(max(0.0, deadline - time.monotonic()))
    if thread.is_alive() or "real" not in box:
        return None
    return Path(box["real"])


def _fresh_root_noted(configured: str) -> Path | None:
    """``_fresh_root``, noting a failed or timed-out probe as unknown."""
    root = _fresh_root(configured)
    if root is None:
        _note_unknown()
    return root


def _confined_real_path(raw_path: str, *, strict: bool = False) -> Path | None:
    """The one gate every resolver of a request-supplied path goes through.

    Fails closed, in order:

    1. Any ``..`` segment is refused outright. Nothing in the app needs one,
       and folding it lexically is wrong when it follows a symlink:
       ``/vol/link/../x`` normalises to ``/vol/x`` while the OS opens the
       link target's parent.
    2. The path, made absolute and normalised, must equal a Volume root or
       start with ``root + os.sep``, checked inline right before the path is
       resolved. The configured spellings come first, with no filesystem
       call; when none matches, the cached resolved spellings of the roots
       (a Volume configured through a symlink, the path spelled through its
       target; ``_resolved_root_spellings``), so an alias never waits on an
       unrelated stalled mount. A path outside every Volume never reaches the
       filesystem.
    3. The path is resolved on disk and the real path must lie inside the
       matched root, resolved, or inside any Volume's cached resolved root (a
       symlink across Volumes). This catches a symlink inside a Volume
       pointing out of every Volume. With ``..`` refused, normalising only
       drops ``.`` segments and repeated separators, so this resolves exactly
       the path the OS would open for ``raw_path``.

    Returns the resolved path, or None.
    """
    if _has_parent_segment(raw_path):
        return None
    candidate = os.path.normpath(os.path.abspath(os.path.expanduser(raw_path)))
    volumes = list(settings.volumes)
    for volume in volumes:
        root = _configured_root(volume)
        # Two arms, kept apart: the root itself is resolved as the trusted
        # spelling, and a path below it only after the prefix check.
        if candidate == root:
            return _real_inside_volumes(root, roots=[volume], strict=strict)
        if candidate.startswith(root.rstrip(os.sep) + os.sep):
            return _real_inside_volumes(candidate, roots=[volume], strict=strict)
    resolved_roots = _resolved_root_spellings()
    for volume in volumes:
        configured = _configured_root(volume)
        root = resolved_roots.get(configured)
        if root is None or root == configured:
            continue
        if candidate == root:
            return _real_inside_volumes(root, roots=[volume], strict=strict)
        if candidate.startswith(root.rstrip(os.sep) + os.sep):
            return _real_inside_volumes(candidate, roots=[volume], strict=strict)
    # Under no configured or cached spelling. That is a verdict only if every
    # root's resolved spelling is known: a root whose probe failed or timed
    # out has none, so a path spelled through its target can't be matched.
    if any(_configured_root(volume) not in resolved_roots for volume in volumes):
        _note_unknown()
    return None


def platform_boundary_root(path: str) -> Path | None:
    """The deepest resolved Volume root that holds ``path``, or None.

    For **Platform inference only**: the folder walk reads folder names from
    the Image up to this root, and a folder above an inner Volume says
    nothing about the Images in it. Never a lock or mutation boundary: a
    folder locked above an inner root would be missed (``lock_boundary_root``
    is that one). ``path`` may be an archive member (``a.zip::disc.iso``); the
    archive's own location decides.
    """
    disk_path = strip_archive_path(path)
    real_path = _confined_real_path(disk_path)
    if real_path is None:
        return None
    # Only the Volumes the path is spelled under are resolved, freshly and
    # bounded (no stalled mount elsewhere is waited on). A symlink into another
    # Volume is held by that one, selected by cached spelling (`_holding_roots`).
    candidate = os.path.normpath(os.path.abspath(os.path.expanduser(disk_path)))
    holders = _holding_roots(real_path, _matching_volumes(candidate))
    return max(holders, key=lambda root: len(root.parts), default=None)


def lock_boundary_root(path: str) -> Path | None:
    """The outermost Volume root over ``path``: where a lock walk stops.

    A folder lock (another process packing a folder) can sit anywhere above
    the path, up to the outermost Volume that contains it, so a walk looking
    for one must not stop at an inner Volume. With Volumes ``/mnt`` and
    ``/games -> /mnt/MyGame/PS3_GAME``, a walk from ``PS3_GAME/PARAM.SFO``
    (spelled either way) goes up to ``/mnt``, past ``/mnt/MyGame``.

    Every overlapping root counts, not only those the request's spelling
    matches: the freshly verified holders of the real path, and every root
    whose configured or cached resolved spelling is a prefix of it. A
    boundary only bounds how far up the walk probes, and probing further up
    is safer, so a cached spelling may extend it. None when the path is
    outside every Volume; a caller then walks to the filesystem root.
    """
    disk_path = strip_archive_path(path)
    real_path = _confined_real_path(disk_path)
    if real_path is None:
        return None
    candidate = os.path.normpath(os.path.abspath(os.path.expanduser(disk_path)))
    tops = list(_holding_roots(real_path, _matching_volumes(candidate)))
    real = str(real_path)
    resolved_roots = _resolved_root_spellings()
    for volume in settings.volumes:
        configured = _configured_root(volume)
        for spelling in (configured, resolved_roots.get(configured)):
            if spelling and _lexically_under(real, spelling):
                tops.append(Path(spelling))
    return min(tops, key=lambda root: len(root.parts), default=None)


def volume_containment(path: str, *, treat_archives: bool = True) -> bool | None:
    """Whether *path* lies inside a configured Volume: True, False, or None.

    The three-way form of ``is_within_configured_volumes``, through the same
    gate (``_confined_real_path``). True: inside. False: confirmed outside
    (refused lexically, or every root it could fall under was freshly
    resolved and doesn't hold it). None: couldn't tell, because a fresh root
    probe the answer depended on failed or went over its bound. A caller that
    must not mistake a stalled mount for "outside" (the ``.m3u`` Referrer
    deciding which playlists to leave alone) uses this.
    """
    base_path = strip_archive_path(path) if treat_archives else path
    before = _unknown_count()
    if _confined_real_path(base_path) is not None:
        return True
    return None if _unknown_count() != before else False


def is_within_configured_volumes(path: str, *, treat_archives: bool = True) -> bool:
    """Check whether the given path lies inside one of the configured CHD volumes.

    Through the one gate, ``_confined_real_path``: no ``..``, confined
    lexically before the filesystem sees it, then resolved and checked again.
    "Couldn't tell" (``volume_containment`` returns None) counts as outside.
    """
    return volume_containment(path, treat_archives=treat_archives) is True


class FileIdentity(NamedTuple):
    """What identifies a file across two stats of it."""

    dev: int
    ino: int
    size: int
    mtime_ns: int
    realpath: str


def file_identity(path: str) -> FileIdentity | None:
    """*path*'s identity (following links), or None if it can't be stat'ed."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return FileIdentity(
        st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, os.path.realpath(path),
    )


def same_identity(a: FileIdentity | None, b: FileIdentity | None) -> bool:
    """Whether two identities are the same, unchanged file.

    Size and ``mtime_ns`` must always match, so a file rewritten in place
    (same inode, new bytes) is not the file that was checked. Then either
    ``(st_dev, st_ino)`` or the resolved path must match: FUSE/SMB mounts such
    as Unraid's shfs user shares can report a different ``st_ino`` for the
    same file on each stat, and their inodes can also be reused, which is why
    the inode is never enough on its own. A missing file (None) is never the
    same as anything.
    """
    if a is None or b is None:
        return False
    if (a.size, a.mtime_ns) != (b.size, b.mtime_ns):
        return False
    return (a.dev, a.ino) == (b.dev, b.ino) or a.realpath == b.realpath


def same_file(a: str, b: str) -> bool:
    """``os.path.samefile`` that tolerates unstable inodes (see ``same_identity``)."""
    return same_identity(file_identity(a), file_identity(b))


def _strip_trailing_dot_and_seps(path: str) -> str:
    """Strip only *trailing* separators and ``.`` components from ``path``.

    Used to find the submitted root entry to ``lstat`` before resolution. Unlike
    :func:`os.path.normpath`, interior ``..`` and symlinked components are left
    intact: normalizing ``/vol/anchor/../LinkGame`` to ``/vol/LinkGame`` would
    lexically cancel a symlinked ``anchor`` against the following ``..``, so a
    pre-resolve ``lstat`` would probe a benign lexical path instead of the real
    final component the kernel reaches (``/outside/LinkGame``). Keeping the
    interior intact lets ``lstat`` resolve the ancestors exactly as the kernel
    does and report the true final entry (catching a symlinked root), while the
    trailing ``/`` / ``/.`` / ``/./`` are removed so the probe lands on that
    entry itself rather than following it.
    """
    seps = os.sep + (os.altsep or "")
    drive, tail = os.path.splitdrive(path)
    prev = None
    while tail != prev:
        prev = tail
        tail = tail.rstrip(seps)
        # Drop a standalone trailing "." component (".", ".../.") — but never the
        # "." inside a ".." component, which stays meaningful.
        if tail.endswith(".") and (len(tail) == 1 or tail[-2] in seps):
            tail = tail[:-1]
    return (drive + tail) or path


def source_companions_are_safe(
    file_path: str, mode: str, *, within: Sequence[str] | None = None,
) -> bool:
    """Whether every sibling input this mode's tool also consumes stays in-volume.

    The single-file analogue of :func:`is_safe_directory_tree`, and it exists
    for the same reason: a multi-file source drags in files the request never
    named, and the native converter opens them *itself* — JNUSLib enumerates a
    split Wii U set's parts from part 1 rather than taking a list from us — so
    the volume boundary the route enforced on ``file_path`` has to be enforced
    on the companions too. Rejects a symlinked companion outright rather than
    following it, plus anything resolving outside the configured volumes.

    Scoped to ``mode``'s own tool, not the registry-wide union: another
    plugin's multi-file semantics must not veto a job it has no part in (a
    chdman ``createhd`` on a raw file that happens to be named
    ``game_part1.wud`` never reads the sibling parts). A no-op for the tools
    whose ``source_companions`` is ``[]``.

    Like the directory check this is a *point-in-time* answer, so both callers
    matter: ``plan_job`` rejects at queue time, and the worker re-checks after
    taking the job's locks, since a queued job's companion can be swapped for a
    symlink in between.

    A track sheet (``.cue`` / ``.gdi``) is checked from its own text, whatever
    the tool reports: the converter opens every file the sheet names, so a
    reference that is absolute, leaves the sheet's folder, or resolves outside
    the volumes rejects the job (``delete_plan.sheet_reference_problems``). An
    empty companion list is never taken to mean safe.

    ``within`` swaps the Volumes for explicit trusted roots, as in
    ``utils.sheet_text.resolve_track``: the worker passes ``[the job's own
    extraction folder]`` when the source came out of an archive, since that
    folder is not a Volume. Companions and track references must then resolve
    inside it. Omitted, the Volumes apply; an empty list is refused.

    Blocking I/O (an ``lstat`` and a ``realpath`` per companion); call it off
    the event loop.
    """
    from services.tools import registry  # local: avoids an import cycle
    from utils.delete_plan import sheet_reference_problems  # local: cycle
    from utils.sheet_text import resolve_track  # local: cycle

    if sheet_reference_problems(file_path, within=within):
        return False

    try:
        tool = registry.for_mode(mode)
    except KeyError:
        return True
    for companion in tool.source_companions(file_path):
        if os.path.islink(companion):
            return False
        # The one confined resolver, as for a sheet's tracks: the companion's
        # real path must sit under the trusted roots.
        folder, name = os.path.split(companion)
        try:
            resolve_track(folder, name, within=within)
        except ValueError:
            return False
    return True


def is_safe_directory_tree(path: str) -> bool:
    """Return whether a directory tree is safe for native recursive readers.

    Native tools such as makeps3iso recursively read the source tree outside of
    Python's path guards.  Reject symlinks and non-regular filesystem entries so
    the confined root's real subtree is the only thing the native reader sees.
    """
    # Reject a symlinked source root before resolving it. A trailing separator
    # or "." component (".../LinkGame/", ".../LinkGame/.", ".../LinkGame/./")
    # makes ``os.path.islink``/``os.lstat`` follow the link to its target, so a
    # request like ``/volume/LinkGame/`` would otherwise resolve the symlink
    # away and hand the native packer a root pointing outside the configured
    # volume. Strip only those trailing components (see the helper) and ``lstat``
    # the resulting root: the kernel resolves any symlinked ancestors while
    # ``lstat`` reports the final entry without following it, so a symlinked
    # root is caught even behind a "." or a ".."-cancelled symlinked ancestor.
    raw_root = _strip_trailing_dot_and_seps(path)
    try:
        raw_lstat = os.lstat(raw_root)
    except OSError:
        return False
    if stat.S_ISLNK(raw_lstat.st_mode):
        return False

    root = _confined_real_path(path, strict=True)
    if root is None or not root.is_dir():
        return False

    root_str = str(root)
    if not is_within_configured_volumes(root_str, treat_archives=False):
        return False

    walk_errors: list[OSError] = []

    def _record_walk_error(error: OSError) -> None:
        walk_errors.append(error)

    for current, dirs, files in os.walk(
        root_str, topdown=True, followlinks=False, onerror=_record_walk_error,
    ):
        if walk_errors:
            return False
        entries = [*dirs, *files]
        for name in entries:
            entry = os.path.join(current, name)
            try:
                entry_lstat = os.lstat(entry)
            except OSError:
                return False

            mode = entry_lstat.st_mode
            if stat.S_ISLNK(mode):
                return False
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                return False
            # No per-entry resolve/volume re-check: ``os.walk(followlinks=False)``
            # never descends through a symlink, and every symlink or special
            # entry is rejected above, so each visited entry is a genuine child
            # of the already volume-confined ``root``. Resolving and
            # re-verifying containment for every file would add tens of
            # thousands of redundant syscalls on a large PS3 tree while proving
            # something the traversal already guarantees.

    return not walk_errors


def is_configured_volume_root(path: str, *, treat_archives: bool = True) -> bool:
    """Check whether the given path is exactly one of the configured volume roots."""
    base_path = strip_archive_path(path) if treat_archives else path
    real_path = _confined_real_path(base_path)
    if real_path is None:
        return False

    for volume in settings.volumes:
        real_volume = _resolve_volume(volume)
        if real_volume is not None and real_path == real_volume:
            return True

    return False


def ensure_path_within_volumes(path: str, *, treat_archives: bool = True) -> Path:
    """Return the resolved path if it is within configured volumes, else raise ValueError."""
    resolved = _confined_real_path(
        strip_archive_path(path) if treat_archives else path,
    )
    if resolved is None:
        raise ValueError("Path outside configured volumes")
    return resolved


def get_volume_name_for_path(path: str) -> Optional[str]:
    """Return the configured volume name that contains the given path, if any."""
    base_path = strip_archive_path(path)
    real_path = _confined_real_path(base_path)
    if real_path is None:
        return None

    for volume in settings.volumes:
        real_volume = _resolve_volume(volume)
        if real_volume is None:
            continue

        try:
            real_path.relative_to(real_volume)
            return settings.get_volume_name(volume)
        except ValueError:
            if real_path == real_volume:
                return settings.get_volume_name(volume)
            continue

    return None


# Flags for one component of a confined, no-follow walk. O_NONBLOCK so a FIFO
# planted where a file is expected cannot block the open; it is then refused by
# the regular-file check.
_BENEATH_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | os.O_NONBLOCK
_BENEATH_DIR_FLAGS = _BENEATH_FLAGS | getattr(os, "O_DIRECTORY", 0)
_BENEATH_CHUNK = 64 * 1024


def _beneath_parts(relative: str) -> list[str]:
    if not relative or relative.startswith("/"):
        raise ValueError(f"Not a relative path below the root: {relative!r}")
    parts = relative.split("/")
    if any(part in ("", ".", "..") or "\\" in part for part in parts):
        raise ValueError(f"Path leaves or bends the tree: {relative!r}")
    return parts


def open_dir_beneath(root: str, relative: str = "") -> int:
    """Open the directory ``root/relative`` without following any symlink.

    Every component, of ``root`` from ``/`` down and then of ``relative``, is
    opened ``O_NOFOLLOW | O_DIRECTORY`` relative to the descriptor above it,
    so a symlink anywhere fails the open (``OSError``, usually ``ELOOP``).
    Returns the directory's descriptor; the caller closes it. A caller that
    checked a path and then works inside it (``*at`` calls with ``dir_fd``)
    is bound to the directory it opened, whatever the path names later.

    ``root`` must be absolute and already symlink-free (``os.path.realpath``);
    ``relative`` (empty for ``root`` itself) uses ``/`` and may not hold
    ``..``, ``.`` or empty parts (``ValueError``). Blocking: run it off the
    event loop.
    """
    if not os.path.isabs(root):
        raise ValueError(f"The root must be absolute: {root!r}")
    root_parts = [part for part in root.split(os.sep) if part]
    parts = _beneath_parts(relative) if relative else []
    fd = os.open("/", _BENEATH_DIR_FLAGS)
    try:
        for part in (*root_parts, *parts):
            child = os.open(part, _BENEATH_DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_beneath(root: str, relative: str, *, max_bytes: int) -> bytes:
    """Read the regular file ``root/relative`` without following any symlink.

    For a file that someone with write access to a Volume could swap between a
    check and the read (a template a tool copies into its product). Every
    component, of ``root`` from ``/`` down and then of ``relative``, is opened
    with ``O_NOFOLLOW`` relative to the directory descriptor above it, so a
    symlink anywhere fails the open (``OSError``, usually ``ELOOP``). The file
    itself must be a regular file (``fstat``), and the bytes come only from
    that one descriptor: no second, path-based open. Linux's ``openat2`` with
    ``RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`` would do the same in one call, but
    Python exposes neither it nor those flags, and this walk works on macOS too.

    The directories are opened by :func:`open_dir_beneath`. ``root`` must be
    absolute and already symlink-free (``os.path.realpath``); ``relative``
    uses ``/`` and may not hold ``..``, ``.`` or empty parts (``ValueError``).
    A file over ``max_bytes`` is refused with ``ValueError``.
    Blocking: run it off the event loop.
    """
    parts = _beneath_parts(relative)
    fd = open_dir_beneath(root, "/".join(parts[:-1]))
    try:
        child = os.open(parts[-1], _BENEATH_FLAGS, dir_fd=fd)
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", os.path.join(root, relative))
        if info.st_size > max_bytes:
            raise ValueError(f"{relative} is larger than {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, _BENEATH_CHUNK):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{relative} is larger than {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


# The characters ``tempfile.mkstemp`` draws its eight random ones from.
_TEMP_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"
_TEMP_NAME_ATTEMPTS = 100


def short_temp_name(prefix: str = ".tmp-", suffix: str = "") -> str:
    """A short, fixed-length temp file name: ``prefix``, eight random
    characters from ``mkstemp``'s alphabet, ``suffix``. Never built from a
    target's name, so a target near the 255-byte name limit can still be
    written beside it. For a writer that must set something up under the name
    before the file exists (``services.partial_writes``)."""
    return prefix + "".join(secrets.choice(_TEMP_NAME_CHARS) for _ in range(8)) + suffix


def short_temp_file(directory: str, *, prefix: str = ".tmp-", suffix: str = "") -> tuple[int, str]:
    """A new hidden temp file in *directory*: ``(fd, path)``, as ``mkstemp``.

    Named by :func:`short_temp_name`, created ``O_EXCL`` with mode ``0600``
    (retrying another name if one is taken), and never through a symlink.
    Shared by every writer that places a finished file next to its target
    (the Referrers' atomic writes).
    """
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_TEMP_NAME_ATTEMPTS):
        path = os.path.join(directory, short_temp_name(prefix, suffix))
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "No free temp file name", directory)


def companion_name(name: str, *, prefix: str, suffix: str = "") -> str:
    """A fixed-length name derived from *name*: ``prefix``, 16 hex characters
    of its SHA-256, ``suffix``. For a file that goes with another one (a temp
    file's lock file, ``services.partial_writes``): the same *name* always
    gives the same companion, and its length never grows with *name*, so a
    file near the 255-byte name limit still has one."""
    return prefix + hashlib.sha256(os.fsencode(name)).hexdigest()[:16] + suffix


def safe_join(base_dir: str, *parts: str) -> Path:
    """Join parts to a base directory while ensuring the result stays inside the base.

    The base goes through the same gate as every request path, so it must be
    an existing directory inside a Volume.
    """
    resolved_base = _confined_real_path(base_dir, strict=True)
    if resolved_base is None:
        raise ValueError("Base directory does not exist")

    candidate = resolved_base.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError("Resulting path escapes base directory") from exc

    return candidate


def cleanup_orphan_lock(lock_path: str):
    """Best-effort removal of leftover lock files."""
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass
