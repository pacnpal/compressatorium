"""Rename Images to their DAT name (#425, #426).

A person picks Images and asks for their DAT names. Nothing here hashes:
the name comes from the match the app already has
(``DATStore.ranked_match_for_image``), and only a Recognized Image has one.

* **The preview** (:func:`preview`): for each Image, the old and new path,
  the DAT that names it and every Referrer update the rename makes (planned
  through the same hook and placement the rename uses, so "RomM: rename ROM
  12", "games.m3u", "gamelist.xml, 1 entry"), or a reason it is skipped: not
  Recognized, already named, the new name taken, a track of a sheet, or two
  equally ranked DATs that disagree. The new name is the top-ranked DAT's
  game name with the file's own extension, made safe by the one shared rule
  (``safe_file_name``).
* **A sheet's Image** (#426) is renamed whole: a ``.cue`` or ``.gdi`` and
  every track it names (the grouping ``image_sets`` uses: a track only this
  sheet names). Each track takes the file name the DAT that named the sheet
  lists for it, paired by size, and in track order among tracks of one size
  (:func:`_track_names`). The preview lists every file (``files``), the
  sheet's reference changes (``sheet_rewrite``) and a ``folder`` renamed
  with it: one that holds exactly the Image's files and carries its name
  (:func:`_folder_for`). Any other Image of several files is skipped.
* **A conflict** gets a ``rename_conflict`` Finding naming every DAT that
  disagrees; it clears once the ranking names the Image
  (:func:`settle_conflicts`, run when the ranking is saved and when the
  Findings are listed).
* **The run** (:func:`start_run`): an external job, like the duplicate
  delete. It plans every Image again: one whose plan is no longer the one
  confirmed is skipped and listed. Each confirmed Image goes through the
  shared rename (``image_rename.rename_image``). The job's result lists every
  change and every skip, and a summary goes out through the notify path.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from logging_setup import get_logger
from models import ConversionMode
from services import (
    findings,
    image_rename,
    image_sets,
    libraries,
    notifications,
    referrers,
)
from services.dat_store import dat_store
from services.job_manager import job_manager
from services.referrers import ReferrerError
from services.subprocess_runner import (
    DeadlineExpired,
    ProgressAbandoned,
    ReadCancelled,
    Stalled,
    run_detached,
    run_detached_bounded,
    run_detached_within_stall_bound,
)
from services.tools import registry as tools
from utils.path_utils import (
    lock_boundary_root,
    safe_file_name,
    same_file,
    volume_containment,
)
from utils.sheet_text import sheet_track_list

logger = get_logger("dat_rename")

RENAME = "rename"
SKIP = "skip"
# How many Images one preview or run may name.
MAX_IMAGES = 2000
# How long planning one Image may take: it reads the folders above it for a
# sheet that lists it, on a mount that may stop answering.
PLAN_SECONDS = 60.0
# The cap on Images listed by name in the notification.
MAX_LISTED = 20

# How long a request waits for the rename-conflict settle (#425).
SETTLE_SECONDS = 5.0
# Held by the rename-conflict settle that is running, across threads, until
# its worker really ends: at most one in flight (CodeRabbit 4602978437).
_conflicts_settling = threading.Lock()

# The job running now, if any: one rename at a time.
_running: str | None = None
_tasks: set[asyncio.Task] = set()


class RenameRefused(Exception):
    """The run may not start: *status* is the HTTP answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ----------------------------------------------------------------------
# planning one Image (blocking)
# ----------------------------------------------------------------------


def _ranked(path: str):
    """The ranked match for *path*, as listed or along its real path."""
    ranked = dat_store.ranked_match_for_image(path)
    if ranked.top is None and not ranked.conflict and not ranked.matches:
        real = os.path.realpath(path)
        if real != os.path.normpath(path):
            ranked = dat_store.ranked_match_for_image(real)
    return ranked


def _dat_of(record: dict) -> dict:
    return {
        "dat_name": record.get("dat_name") or "Hasheous",
        "family": record.get("family"),
        "game_name": record.get("game_name"),
    }


def dat_file_name(game_name: str, name: str) -> str | None:
    """The file name the DAT's *game_name* gives a file now called *name*:
    the game name with the file's own extension (compound ones whole, Codex
    4602560251), made safe by the one shared rule (``safe_file_name``). None
    when it can't be made a file name. The one naming rule: the rename to DAT
    name and the Inbox (#432) both name Images with it.

    A numbered part of a split output (``Game.iso.0``, the registry's
    ``split_owner_name``) keeps its logical extension and its part number:
    ``Canonical Game.iso.0`` (Codex 4608919686)."""
    logical = tools.split_owner_name(name)
    if logical is not None:
        named = safe_file_name(f"{game_name}{tools.extension_of(logical)}")
        return None if named is None else f"{named}{name[len(logical):]}"
    return safe_file_name(f"{game_name}{tools.extension_of(name)}")


def dat_file_names(game_name: str, names: list[str]) -> list[str] | None:
    """Each of an Image's files' names under the DAT's *game_name*, its main
    file first (*names* in that order): the main file by
    :func:`dat_file_name`, and every other part of a split output alike
    (``Game.iso.1`` -> ``Canonical Game.iso.1``), so the set stays one; a
    sheet's tracks keep theirs, so the sheet still names them. None when the
    name can't be made a file name."""
    main = dat_file_name(game_name, names[0])
    if main is None:
        return None
    logical = tools.split_owner_name(names[0])
    renamed = [main]
    for name in names[1:]:
        if logical is not None and tools.split_owner_name(name) == logical:
            renamed.append(dat_file_name(game_name, name) or name)
        else:
            renamed.append(name)
    return renamed


def conflict_text(conflict: Iterable[dict]) -> tuple[str, list[dict]]:
    """Which DAT names an Image how, for a conflict's Finding: the words
    (each DAT and the name it gives) and the matches. Shared by the
    rename's Finding and the Inbox's (#432)."""
    matches = sorted(
        (_dat_of(m) | {"dat_id": m.get("dat_id")} for m in conflict),
        key=lambda m: (m["dat_name"] or "", m["game_name"] or ""),
    )
    named = " and ".join(f"{m['dat_name']} names it “{m['game_name']}”" for m in matches)
    return named, matches


def _conflict_finding(path: str, conflict: Iterable[dict]) -> findings.Observed:
    named, matches = conflict_text(conflict)
    return findings.Observed(
        kind=findings.RENAME_CONFLICT, image_key=path, path=path,
        reason=f"Not renamed: {named}, and the ranking puts them level; rank the DATs to choose",
        cause={"matches": matches},
    )


def _owner(library) -> tuple[str, Any]:
    """The Findings owner for an Image in *library*: its id and ``current``."""
    if library is None:
        return findings.NO_LIBRARY, None
    return library.id, getattr(library, "is_current", None)


def _note_conflict(path: str, library, conflict) -> None:
    """Record the rename-conflict Finding, or clear it once there is none.
    Bookkeeping: a write that fails is logged, never fails the plan."""
    owner, current = _owner(library)
    try:
        if conflict:
            findings.observe(owner, _conflict_finding(path, conflict), current=current)
        else:
            findings.resolve(owner, path, findings.RENAME_CONFLICT, current=current)
    except Exception:
        logger.warning("dat_rename: could not record the conflict of %s", path, exc_info=True)


Owners = Mapping[tuple[int, int], frozenset[tuple[int, int]]]


def _identity(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


def _member_problem(path: str, tracks: Owners) -> str | None:
    """Why *path* isn't an Image this can rename, or None. *tracks* is every
    sheet's tracks over the Volumes, each with the sheets that name it
    (:func:`sheet_tracks`)."""
    if image_sets.is_playlist(path):
        return "a playlist, not an Image"
    boundary = lock_boundary_root(path)
    if boundary is None:
        return "can't confirm its Volume boundary, so its sheet can't be looked for"
    try:
        sheet = image_sets.owning_sheet(path, stop_at=str(boundary))
    except image_sets.SheetOwnerUnknown as exc:
        return f"can't tell whether a sheet lists it as a track ({exc})"
    if sheet is not None:
        return f"a track of {os.path.basename(sheet)}: it is renamed with its sheet"
    # A sheet anywhere in the Volumes may reach it through a link (Codex
    # 4602560262): the Volume-wide walk the duplicate delete uses decides.
    if _identity(path) in tracks:
        return "a track a .cue or .gdi elsewhere lists: it is renamed with its sheet"
    if not image_rename.is_sheet(path) and image_sets.image_members(path):
        return "an Image of several files that aren't a sheet's tracks; not renamed"
    return None


def _sheet_tracks(path: str, tracks: Owners) -> list[str] | str:
    """The tracks of the sheet at *path*, in its order, or why it can't be
    renamed with them: each must be a file of its own (no link), named by
    this sheet and by no other one."""
    listed = sheet_track_list(path)
    if listed is None:
        return "its tracks can't be read whole from the sheet"
    named = list(dict.fromkeys(str(track) for track in listed))
    if not named:
        return "the sheet names no track"
    extra = {
        os.path.normpath(m) for m in image_sets.image_members(path)
    } - {os.path.normpath(t) for t in named}
    if extra:
        return f"{os.path.basename(min(extra))} belongs to it but isn't a track"
    own = frozenset({_identity(path)})
    for track in named:
        name = os.path.basename(track)
        if os.path.islink(track) or not os.path.isfile(track):
            return f"its track {name} isn't a file of its own"
        if tracks.get(_identity(track)) != own:
            return f"its track {name} is listed by another sheet too"
    return named


def _natural(name: str) -> list:
    """*name* as a person sorts it: ``Track 2`` before ``Track 10``."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.casefold())]


def _track_names(top: dict, tracks: list[str]) -> dict[str, str] | str:
    """Each track's new file name, from the file names the DAT that named
    the sheet lists for its game (#426), or why they can't be told.

    Nothing is hashed: a track pairs with the DAT file of its size, and
    tracks of one size pair in the sheet's order with those files in name
    order (``Track 2`` before ``Track 10``). The DAT's name is taken whole,
    unless its extension differs from the track's, which is kept."""
    if not top.get("dat_id"):
        return "its tracks' names aren't known: only Hasheous names it, not a DAT"
    listed = [
        f for f in dat_store.game_files(top["dat_id"], top.get("game_name") or "")
        if not f["rom_name"].lower().endswith(image_sets.SHEET_EXTENSIONS)
    ]
    if len(listed) != len(tracks):
        return f"it has {len(tracks)} tracks where its DAT lists {len(listed)} files"
    by_size: dict[int, list[str]] = {}
    for rom in sorted(listed, key=lambda f: _natural(f["rom_name"])):
        by_size.setdefault(int(rom["size"]), []).append(rom["rom_name"])
    names: dict[str, str] = {}
    for track in tracks:
        candidates = by_size.get(os.path.getsize(track))
        if not candidates:
            return f"its track {os.path.basename(track)} has no file of its size in the DAT"
        dat_name = re.split(r"[\\/]", candidates.pop(0))[-1]
        own = tools.extension_of(os.path.basename(track))
        theirs = tools.extension_of(dat_name)
        if own.lower() != theirs.lower():
            dat_name = dat_name[: len(dat_name) - len(theirs)] + own
        new_name = safe_file_name(dat_name)
        if new_name is None:
            return f"its DAT's name for {os.path.basename(track)} can't be made a file name"
        names[track] = new_name
    return names


def _folder_for(
    path: str, files: list[str], game_name: str, library,
) -> image_rename.Folder | str | None:
    """The folder renamed with the Image at *path* (#426): the one holding
    exactly its *files* and named as the Image is (its file name without the
    extension), never a Volume root, a Library root or a Platform folder.
    None when there is none; why, when its new name is taken."""
    folder = os.path.dirname(path)
    name = os.path.basename(path)
    stem = name[: len(name) - len(tools.extension_of(name))]
    if os.path.basename(folder) != stem or any(os.path.dirname(f) != folder for f in files):
        return None
    if set(os.listdir(folder)) != {os.path.basename(f) for f in files}:
        return None
    real = os.path.realpath(folder)
    boundary = lock_boundary_root(folder)
    if boundary is None or os.path.realpath(str(boundary)) == real:
        return None
    if library is not None:
        root = getattr(library, "root", None)
        try:
            platform = library.platform_folder(path)
        except Exception:
            # A Platform folder that can't be told may be this one: kept.
            logger.warning(
                "dat_rename: couldn't tell the Platform folder of %s", path, exc_info=True,
            )
            return None
        if any(p and os.path.realpath(p) == real for p in (root, platform)):
            return None
    new_name = safe_file_name(game_name)
    if new_name is None or new_name == os.path.basename(folder):
        return None
    new = os.path.join(os.path.dirname(folder), new_name)
    if volume_containment(new, treat_archives=False) is not True:
        return None
    if os.path.lexists(new):
        return f"a folder {new_name} already exists beside its folder"
    return image_rename.Folder(folder, new)


def _skip(item: dict, reason: str) -> dict:
    item.update(
        status=SKIP, reason=reason, new_path=None, files=[], folder=None, sheet_rewrite=[],
    )
    return item


def plan_blocking(path: str, tracks: Owners | None) -> tuple[dict, Any]:
    """One Image's preview item (without its Referrer changes) and its
    Library. *tracks*: :func:`sheet_tracks`'s answer, None when it couldn't
    be told (then nothing is renamed). Blocking: reads the disk and the
    database.

    A sheet's Image (#426) is renamed whole: ``files`` lists every file of it
    with its new path, ``sheet_rewrite`` each track reference the sheet's
    rewrite changes, and ``folder`` the folder renamed with it, if any."""
    item: dict = {
        "path": path, "new_path": None, "status": SKIP, "reason": None,
        "dat": None, "referrer_changes": [], "files": [], "folder": None,
        "sheet_rewrite": [],
    }
    name = os.path.basename(path)
    if volume_containment(path, treat_archives=False) is not True:
        return _skip(item, "isn't confirmed inside the configured Volumes"), None
    if os.path.islink(path):
        return _skip(item, f"{name} is a symlink"), None
    if not os.path.isfile(path):
        return _skip(item, f"{name} is gone"), None
    if tracks is None:
        return _skip(item, "which files are sheets' tracks couldn't be told"), None
    problem = _member_problem(path, tracks)
    if problem:
        return _skip(item, problem), None
    sheet_tracks_ = _sheet_tracks(path, tracks) if image_rename.is_sheet(path) else []
    if isinstance(sheet_tracks_, str):
        return _skip(item, sheet_tracks_), None
    try:
        library = libraries.library_for(path)
    except libraries.AmbiguousLibrary as exc:
        return _skip(item, str(exc)), None
    except OSError as exc:
        return _skip(item, f"can't tell which Library holds it ({exc})"), None
    ranked = _ranked(path)
    _note_conflict(path, library, ranked.conflict)
    if ranked.conflict:
        dats = " and ".join(sorted({_dat_of(m)["dat_name"] for m in ranked.conflict}))
        return _skip(item, f"{dats} name it differently and are ranked level; rank them"), library
    if ranked.top is None:
        return _skip(item, "not Recognized: no DAT or Hasheous match is known for it"), library
    item["dat"] = _dat_of(ranked.top)
    game_name = ranked.top.get("game_name") or ""
    new_name = dat_file_name(game_name, name)
    if new_name is None:
        return _skip(item, "its DAT name can't be made a file name"), library
    inner = {path: os.path.join(os.path.dirname(path), new_name)}
    if sheet_tracks_:
        names = _track_names(ranked.top, sheet_tracks_)
        if isinstance(names, str):
            return _skip(item, names), library
        inner |= {t: os.path.join(os.path.dirname(t), n) for t, n in names.items()}
    if all(old == new for old, new in inner.items()):
        return _skip(item, "already has its DAT name"), library
    for old, new in inner.items():
        if old != new and os.path.lexists(new):
            if not same_file(new, old):
                return _skip(item, f"{os.path.basename(new)} already exists"), library
            whose = "" if old == path else f"its track {os.path.basename(old)} "
            return _skip(item, f"{whose}differs from its DAT name only in letter case"), library
    rewrite = _sheet_changes(path, inner) if sheet_tracks_ else {}
    if rewrite is None:
        return _skip(item, "its sheet's track references can't be rewritten exactly"), library
    folder = _folder_for(path, list(inner), game_name, library)
    if isinstance(folder, str):
        return _skip(item, folder), library
    try:
        refused = referrers.registry.rename_refused(library)
    except ReferrerError as exc:
        refused = f"{exc.target}: {exc.reason}"
    if refused:
        return _skip(item, refused), library
    final = {
        old: os.path.join(folder.new, os.path.relpath(new, folder.old)) if folder else new
        for old, new in inner.items()
    }
    item.update(
        status=RENAME, new_path=final[path],
        files=[{"path": old, "new_path": new} for old, new in final.items()],
        folder={"path": folder.old, "new_path": folder.new} if folder else None,
        sheet_rewrite=[
            {"old": _shown(old), "new": _shown(new)} for old, new in rewrite.items()
        ],
    )
    return item, library


def _shown(reference: str) -> str:
    """A sheet reference as the preview shows it: a byte that isn't UTF-8
    (a Windows-1252 sheet's ``é``) written as ``\\xe9``."""
    return reference.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _sheet_changes(path: str, inner: Mapping[str, str]) -> dict[str, str] | None:
    """The sheet's track references the rename rewrites (the preview shows
    them), or None when the rewrite can't be done exactly."""
    from utils.sheet_text import MAX_SHEET_BYTES

    with open(path, "rb") as fh:
        raw = fh.read(MAX_SHEET_BYTES + 1)
    changes = image_rename.sheet_reference_changes(path, raw, inner)
    if changes is None or image_rename.sheet_rewrite(path, raw, inner) is None:
        return None
    return changes


def renames_of(item: Mapping) -> tuple[dict[str, str], image_rename.Folder | None]:
    """The renames map and folder a planned *item* asks of
    ``image_rename.rename_image``: every file whose path changes."""
    folder = item.get("folder")
    renames = {
        f["path"]: f["new_path"] for f in item.get("files") or ()
        if f["path"] != f["new_path"]
    }
    return renames, image_rename.Folder(folder["path"], folder["new_path"]) if folder else None


async def sheet_tracks(cancel_event=None) -> tuple[Owners | None, str]:
    """Every sheet's tracks over the Volumes, each with the sheets that name
    it, read once per preview and per run (``image_sets.volume_sheet_owners``,
    the walk the duplicate delete reads them by), or None and why when the
    walk didn't finish: fail closed."""
    from services import content_keys, duplicates

    try:
        tracks, problem = await run_detached_within_stall_bound(
            image_sets.volume_sheet_owners, duplicates.volume_scope(),
            what="reading the Volumes' sheets", cancel_event=cancel_event,
            stall=content_keys.STALL_SECONDS, report_progress=True,
        )
    except (OSError, Stalled, ProgressAbandoned, ReadCancelled) as exc:
        return None, str(exc) or type(exc).__name__
    return (tracks, "") if tracks is not None else (None, str(problem))


async def _plan(path: str, tracks, why: str = "") -> tuple[dict, Any]:
    item = {"path": path, "dat": None, "referrer_changes": []}
    if tracks is None:
        return _skip(item, f"which files are sheets' tracks couldn't be told ({why})"), None
    try:
        return await run_detached_bounded(plan_blocking, path, tracks, timeout=PLAN_SECONDS)
    except DeadlineExpired:
        return _skip(item, "its Volume didn't answer in time"), None
    except Exception as exc:
        # One Image that can't be checked never hides the others (CodeRabbit
        # 4602644812): it is skipped with the reason.
        logger.warning("dat_rename: couldn't check %s", path, exc_info=True)
        return _skip(item, f"couldn't be checked ({exc})"), None


async def _with_referrer_changes(item: dict, library) -> dict:
    """The Referrer updates the rename makes, planned with the rename's own
    placement. One that can't be told skips the Image."""
    if item["status"] != RENAME:
        return item
    # The folder too, as the rename tells the hook (``referrer_renames``).
    renames = image_rename.referrer_renames(*renames_of(item))
    try:
        changes = await referrers.plan_changes_bounded(
            item["path"], renames, library, image_rename.placement_for(renames),
        )
    except Exception as exc:
        # Anything that keeps the changes from being told skips the Image.
        logger.warning("dat_rename: couldn't plan the Referrers of %s", item["path"], exc_info=True)
        reason =f"{exc.target}: {exc.reason}" if isinstance(exc, ReferrerError) else str(exc)
        return _skip(item, f"couldn't tell what points at it ({reason})")
    item["referrer_changes"] = [change.to_dict() for change in changes]
    return item


def _new_paths(item: dict) -> list[str]:
    """Every path a planned *item* takes: each renamed file's and its folder's."""
    folder = item.get("folder")
    return [
        *(f["new_path"] for f in item.get("files") or () if f["new_path"] != f["path"]),
        *((folder["new_path"],) if folder else ()),
    ]


async def preview(paths: list[str]) -> dict:
    """Each Image's rename, or why it is skipped. Changes no file."""
    items = []
    # Each new path to the Image that takes it: the first one asked for wins.
    taken: dict[str, str] = {}
    tracks, why = await sheet_tracks()
    for path in dict.fromkeys(paths):
        item, library = await _plan(path, tracks, why)
        wanted = _new_paths(item)
        other = next((taken[p] for p in wanted if p in taken), None)
        if other is not None:
            name = os.path.basename(other)
            _skip(item, f"{name} takes that name too, so this one is left as it is")
        else:
            taken.update(dict.fromkeys(wanted, path))
        items.append(await _with_referrer_changes(item, library))
    renames = sum(1 for item in items if item["status"] == RENAME)
    return {"items": items, "renames": renames, "skipped": len(items) - renames}


def settle_conflicts() -> int:
    """Clear each rename-conflict Finding the ranking now resolves (or that
    is no longer matched at all). Database only. Returns how many cleared."""
    cleared = 0
    for row in findings.list_findings(include_dismissed=True):
        if row["kind"] != findings.RENAME_CONFLICT:
            continue
        if _ranked(row["path"]).conflict:
            continue
        cleared += findings.resolve(
            row["library_id"], row["image_key"], findings.RENAME_CONFLICT,
            fingerprint=row["fingerprint"],
        )
    return cleared


def _settle_conflicts_once() -> int:
    """One settle of the rename conflicts, unless one is running."""
    if not _conflicts_settling.acquire(blocking=False):
        return 0
    try:
        return settle_conflicts()
    finally:
        _conflicts_settling.release()


def settling_conflicts() -> bool:
    """Whether a rename-conflict settle's worker is running now."""
    return _conflicts_settling.locked()


async def settle_conflicts_bounded() -> None:
    """:func:`settle_conflicts` for a request: the Findings list, and saving
    or resetting the DAT ranking (Codex 4603430802). Never raises (CodeRabbit
    4602644770): one that fails or runs past ``SETTLE_SECONDS`` (it may
    resolve a path on a Volume that stopped answering, CodeRabbit 4602644800)
    leaves the Findings as they are, and the next request tries again.
    Detached, and at most one in flight, held until its worker really ends
    (CodeRabbit 4602978437): requests while one is stuck start none, so they
    can't use up the detached slots.

    A worker that finishes after the request stopped waiting may still clear
    a Finding, which is harmless: it only ever deletes a row whose cause is
    the one it read (``findings.resolve`` with that fingerprint) and whose
    DATs the ranking now orders; it never adds or changes one."""
    if settling_conflicts():
        return
    try:
        await asyncio.wait_for(run_detached(_settle_conflicts_once), SETTLE_SECONDS)
    except Exception:
        logger.warning(
            "dat_rename: settling the rename conflicts did not finish; the next request "
            "tries again", exc_info=True,
        )


# ----------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------


async def start_run(items: list[dict]) -> dict:
    """Start renaming the confirmed preview *items* (``{path, new_path}``) as
    a background job; ``{job_id}``. One at a time."""
    global _running
    if _running is not None:
        raise RenameRefused(409, f"A rename to DAT names is already running ({_running})")
    count = len(items)
    job = job_manager.create_external_job(
        f"Rename {count} Image{'' if count == 1 else 's'} to their DAT names",
        ConversionMode.DAT_RENAME, message="Checking each Image again",
    )
    _running = job.id
    task = asyncio.get_running_loop().create_task(_run(job.id, [dict(i) for i in items]))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"job_id": job.id}


async def drain() -> None:
    """Wait for every running rename job (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _run(job_id: str, items: list[dict]) -> None:
    global _running
    try:
        result = await _rename_all(job_id, items)
    except Exception:
        logger.warning("dat_rename: the rename job %s failed", job_id, exc_info=True)
        await job_manager.finish_external_job(
            job_id, success=False, error_message="The rename stopped on an error",
        )
    else:
        job = job_manager.get_job(job_id)
        if job is not None:
            job.result = result
            job.message = _describe(result)
        notifications.notify(_event(result))
        if result["cancelled"]:
            await job_manager.finish_external_job_cancelled(job_id, message=_describe(result))
        else:
            await job_manager.finish_external_job(job_id, success=True)
    finally:
        if _running == job_id:
            _running = None


async def _rename_all(job_id: str, items: list[dict]) -> dict:
    results: list[dict] = []
    cancelled = False
    total = len(items)
    tracks, why = await sheet_tracks(job_manager.get_cancel_event(job_id))
    for index, confirmed in enumerate(items):
        path = str(confirmed.get("path") or "")
        if job_manager.is_cancelled(job_id):
            cancelled = True
            results.append(_result(path, None, "skipped", "cancelled before it was renamed"))
            continue
        await job_manager.update_external_job(
            job_id, progress=int(100 * index / total) if total else 0,
            message=f"[{index + 1}/{total}] {os.path.basename(path)}",
        )
        new_path = confirmed.get("new_path")
        try:
            results.append(await _rename_one(
                job_id, path, new_path, tracks, why, confirmed.get("files"),
            ))
        except Exception as exc:
            # One Image that fails unexpectedly never hides the others'
            # results (CodeRabbit 4602644812).
            logger.warning("dat_rename: renaming %s failed", path, exc_info=True)
            results.append(_result(path, new_path, "not_renamed", f"stopped on an error ({exc})"))
    # A cancel that came during the last Image ends the job cancelled too
    # (Codex 4603430454).
    cancelled = cancelled or job_manager.is_cancelled(job_id)
    counts = {
        status: sum(1 for r in results if r["status"] == status)
        for status in ("renamed", "skipped", "not_renamed")
    }
    return {"results": results, **counts, "cancelled": cancelled}


def _result(
    path: str, new_path: str | None, status: str, reason: str | None,
    referrer_records: list[dict] | None = None,
) -> dict:
    return {
        "path": path, "new_path": new_path, "status": status, "reason": reason,
        "referrers": list(referrer_records or []),
    }


def _files_of(files: Iterable[Mapping] | None) -> dict[str, str | None] | None:
    if files is None:
        return None
    return {str(f.get("path")): f.get("new_path") for f in files}


async def _rename_one(
    job_id: str, path: str, confirmed: str | None, tracks, why: str,
    confirmed_files: list[Mapping] | None = None,
) -> dict:
    item, library = await _plan(path, tracks, why)
    if item["status"] != RENAME:
        return _result(path, confirmed, "skipped", item["reason"])
    if confirmed != item["new_path"]:
        now = os.path.basename(item["new_path"])
        why = (
            "it wasn't to be renamed when the preview was shown" if confirmed is None
            else f"its DAT name changed since the preview (now {now})"
        )
        return _result(path, confirmed, "skipped", why)
    files = _files_of(confirmed_files)
    if files is None and len(item["files"]) > 1:
        # One path confirmed is never several files renamed (CodeRabbit
        # 4604333160): only a single-file plan may come without ``files``.
        return _result(
            path, confirmed, "skipped",
            "it is renamed with several files, and they weren't confirmed",
        )
    if files is not None and files != _files_of(item["files"]):
        # A sheet's tracks are renamed as the preview showed them, or not at
        # all (#426).
        return _result(path, confirmed, "skipped", "its files' new names changed since the preview")

    async def waiting(what: str) -> None:
        # The rename settles before it decides, however long that takes.
        await job_manager.update_external_job(
            job_id, message=f"{os.path.basename(path)}: {what}",
        )

    renames, folder = renames_of(item)
    outcome = await image_rename.rename_image(
        path, renames, folder=folder, library=library,
        cancel_event=job_manager.get_cancel_event(job_id), on_wait=waiting,
    )
    if outcome.renamed:
        note = "; ".join(outcome.notes) or None
        return _result(path, item["new_path"], "renamed", note, outcome.records)
    return _result(path, item["new_path"], "not_renamed", outcome.reason, outcome.records)


def _describe(result: dict) -> str:
    text = f"Renamed {result['renamed']}"
    if result["skipped"]:
        text += f"; skipped {result['skipped']}"
    if result["not_renamed"]:
        text += f"; {result['not_renamed']} not renamed"
    if result["cancelled"]:
        text += "; cancelled"
    return text


def _event(result: dict) -> notifications.Event:
    title = (
        f"Rename to DAT name finished: {result['renamed']} renamed, "
        f"{result['skipped']} skipped, {result['not_renamed']} not renamed"
    )
    failed = [r for r in result["results"] if r["status"] == "not_renamed"]
    lines = [_describe(result) + "."]
    if failed:
        lines.append("Not renamed:")
        lines += [
            f"- {os.path.basename(r['path'])}: {r['reason']}" for r in failed[:MAX_LISTED]
        ]
        if len(failed) > MAX_LISTED:
            lines.append(f"- and {len(failed) - MAX_LISTED} more")
    return notifications.Event("rename_finished", title, "\n".join(lines), {
        "renamed": result["renamed"], "skipped": result["skipped"],
        "not_renamed": result["not_renamed"], "cancelled": result["cancelled"],
        "failures": [{"file": os.path.basename(r["path"]), "error": r["reason"]} for r in failed],
    })
