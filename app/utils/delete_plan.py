import logging
import os
import posixpath
from pathlib import Path
from typing import Dict, List, Sequence, Set

from utils.path_utils import (
    is_within_configured_volumes,
    lock_boundary_root,
    strip_archive_path,
)
from utils.sheet_text import (
    cue_tracks,
    gdi_tracks,
    resolve_track,
    sheet_ambiguity,
    sheet_native_text,
    sheet_readings,
    sheet_tracks,
)


logger = logging.getLogger("compressatorium.delete_plan")

# The one confined track resolver lives in utils.sheet_text; this name stays
# for the callers that import it from here.
_resolve_track_path = resolve_track


def _read_sheet(sheet_path: Path) -> list[str]:
    """The sheet's readings (``utils.sheet_text``); raises when unreadable."""
    readings = sheet_readings(sheet_path)
    if not readings:
        raise OSError(f"Cannot read track sheet: {sheet_path}")
    return readings


def _gdi_tracks_from_text(text: str) -> list[str]:
    return [track.name for track in gdi_tracks(text)]


def sheet_track_paths(
    sheet_path: str, *, within: Sequence[str | Path] | None = None,
) -> list[str]:
    """The track files a ``.cue`` or ``.gdi`` sheet names, resolved beside it.

    The same parsing and containment rules the delete plan uses, so a track
    reference that escapes the sheet's folder is left out, and the same
    decoding the disc-ID probe uses (``utils.sheet_text``): the first spelling
    of a name that exists wins. Tracks are listed whether or not they exist
    yet. Empty for a file that is not a sheet, and for a sheet that is
    unreadable (missing, or over the size cap ``utils.sheet_text`` enforces;
    never a partial list). A caller for whom "unreadable" must not read as "no
    tracks" checks ``utils.sheet_text.sheet_is_readable`` itself.
    """
    sheet = Path(sheet_path)
    parse = {".cue": cue_tracks, ".gdi": _gdi_tracks_from_text}.get(
        sheet.suffix.lower(),
    )
    if parse is None:
        return []
    tracks = sheet_tracks(sheet, parse, within=within)
    return [str(track) for track in tracks] if tracks else []


# The shared sheet parsers (`utils.sheet_text`): the only FILE-line / GDI-line readers.
_SHEET_TEXT_PARSERS = {".cue": cue_tracks, ".gdi": _gdi_tracks_from_text}


def sheet_reference_problems(
    sheet_path: str, *, within: Sequence[str] | None = None,
) -> list[str]:
    """Why a ``.cue`` / ``.gdi`` sheet is unsafe to hand to a converter, or ``[]``.

    The converter opens the files a sheet names itself, so each reference has
    to stay inside the trusted roots or the job would pack a file from
    elsewhere into its output. Each reference goes through the one confined
    resolver, ``utils.sheet_text.resolve_track``: unsafe is a reference that
    is absolute, leaves the sheet's folder (``..``), or resolves (through a
    symlink) outside the roots. ``within`` names those roots and is passed
    straight through: the configured Volumes by default, or the job's own
    extraction folder for a sheet taken out of an archive. Every decoding of
    the sheet is checked, so a name cannot hide in one spelling. A sheet that
    exists but cannot be read (or is over the size cap) is unsafe too: nothing
    was checked. Empty for any other file. Blocking (reads the sheet, resolves
    each track).
    """
    sheet = Path(sheet_path)
    parse = _SHEET_TEXT_PARSERS.get(sheet.suffix.lower())
    if parse is None:
        return []
    readings = sheet_readings(sheet)
    if not readings:
        return ["the track sheet could not be read"] if os.path.exists(sheet) else []
    problems: list[str] = []
    for text in readings:
        for ref in parse(text):
            try:
                resolve_track(sheet.parent, ref, within=within)
            except ValueError as exc:
                problems.append(str(exc))
    return list(dict.fromkeys(problems))


def _unsafe_member_reason(entry) -> str | None:
    """Why an archive member's name or kind is unsafe, or None.

    The shared extraction validator (``ArchiveService._validate_member``:
    absolute, ``..``, empty or ``.`` segments, a drive letter, a backslash, a
    NUL) plus symlink entries. A directory entry is checked without its
    trailing slash.
    """
    from services.archive import archive_service

    if entry.is_symlink:
        return "a symlink"
    name = entry.name.rstrip("/") if entry.is_dir else entry.name
    try:
        archive_service._validate_member(name)
    except ValueError as exc:
        return str(exc)
    return None


class _AmbiguousSheet(Exception):
    """Two spellings of one track name match two different members."""

    def __init__(self, members: list[str]):
        super().__init__(", ".join(members))
        self.members = members


def _image_members(archive: str, member: str, names: set[str]) -> set[str]:
    """The members that belong to the Image ``member``: itself, the tracks its
    sheet names, and its split parts. Raises when the sheet can't be read.

    Called only once every member name has passed the extraction validator
    (``archive_members_beyond_image``). Tracks: the sheet alone is extracted
    (``ArchiveService.extract_file``, which validates its destination) and its
    references resolved with ``resolve_track`` confined to that temp dir; a
    track is matched by name, nothing is created for it. Split parts: the
    registry's ``source_companions`` looks for siblings on disk, so the
    validated names are laid out as empty files inside a fresh temp root, each
    through ``ArchiveService._prepare_destination`` (validated, and checked by
    real path to stay inside that root).
    """
    import shutil

    from services.archive import archive_service
    from services.tools import registry

    owned = {member}
    parent = posixpath.dirname(member)
    if posixpath.splitext(member)[1].lower() in {".cue", ".gdi"}:
        extracted, temp_dir = archive_service.extract_file(archive, member)
        try:
            parse = cue_tracks if member.lower().endswith(".cue") else _gdi_tracks_from_text
            # Every spelling of the sheet, in the same track order (chdman
            # opens one of them).
            readings = [parse(text) for text in _read_sheet(Path(extracted))]
            sheet_dir = os.path.dirname(extracted)
            for index in range(len(readings[0]) if readings else 0):
                matched = set()
                for spelling in readings:
                    if index >= len(spelling):
                        continue
                    try:
                        track = resolve_track(sheet_dir, spelling[index], within=[temp_dir])
                    except ValueError:
                        continue  # names nothing in the archive
                    rel = os.path.relpath(str(track), sheet_dir).replace(os.sep, "/")
                    name = posixpath.normpath(posixpath.join(parent, rel))
                    if name in names:
                        matched.add(name)
                if len(matched) > 1:
                    # Which one chdman opens depends on the reading: keep the
                    # archive rather than guess (the rule #294 uses on disk).
                    raise _AmbiguousSheet(sorted(matched))
                owned |= matched
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    # Under the app's temp dir (CHD_TEMP_DIR), as extraction is: a read-only
    # system temp must not refuse every archive delete.
    temp_root = archive_service._create_temp_dir()
    try:
        for name in names:
            if name.endswith("/"):
                continue
            try:
                target = archive_service._prepare_destination(temp_root, name)
                with open(target, "xb"):
                    pass
            except (OSError, ValueError):
                continue
        for part in registry.source_companions(
            os.path.join(temp_root, *member.split("/")), strict=True,
        ):
            rel = os.path.relpath(os.path.realpath(part), os.path.realpath(temp_root))
            owned.add(posixpath.normpath(rel.replace(os.sep, "/")))
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    return owned


def describe_extras(extras: List[str], limit: int = 3) -> str:
    """The first few extra members, for a summary line."""
    shown = ", ".join(extras[:limit])
    return shown + (", ..." if len(extras) > limit else "")


def archive_members_beyond_image(source_path: str) -> List[str]:
    """Members of an archive-member Source's archive that keep it: the names
    ``_archive_verdict`` gives, whatever the reason. Empty when it may go."""
    verdict = _archive_verdict(source_path)
    return verdict[1] if verdict else []


# Why an archive was kept, as the summary says it (review r11 M4).
_KEPT_PHRASES = {
    "extra": "also holds {}",
    "unsafe": "has an unsafe member name {}",
    "duplicate": "has a duplicate member {}",
    "ambiguous": "has an ambiguous sheet spelling {}",
}


def archive_kept_summary(source_path: str) -> str | None:
    """Why an archive-member Source's archive must be kept, for a summary line
    ("also holds readme.txt", "has an unsafe member name ../x", "has a
    duplicate member Game.iso", "has an ambiguous sheet spelling Café.bin,
    CafÃ©.bin", "can't be read"), or None when it may go."""
    verdict = _archive_verdict(source_path)
    if verdict is None:
        return None
    why, names = verdict
    if why == "unreadable":
        return "can't be read"
    return _KEPT_PHRASES[why].format(describe_extras(names))


def _archive_verdict(source_path: str) -> tuple[str, List[str]] | None:
    """Whether an archive-member Source's archive must be kept, and why.

    ``(why, members)`` with ``why`` one of ``extra``, ``unsafe``,
    ``duplicate``, ``ambiguous`` or ``unreadable``; None when it may go.

    Deleting an archive member's Source deletes the whole archive, so it may
    go only when every member belongs to the Image that was converted: its own
    file, the tracks its sheet names (``resolve_track``) and its split parts
    (#306, review r9 C1). Anything else keeps the archive, whatever its type:
    another disc, a ROM, a loose ``.bin``, a nested archive, a PS3 folder, a
    ``readme.txt``, junk. There is no list of harmless sidecars. A directory
    entry counts only when nothing of the Image lives under it. A member whose
    name or kind is unsafe (``_unsafe_member_reason``) keeps the archive too,
    and nothing is created from any name until every one has passed. An
    archive or sheet that can't be read answers with the archive's own name,
    so the plan refuses. Empty for a plain file.
    """
    if "::" not in source_path:
        return None
    archive, member = source_path.split("::", 1)
    from services.archive_members import read_archive_members

    try:
        members = read_archive_members(archive)
    except Exception:
        # Unknown contents: keep the archive.
        logger.debug("Could not list %s", archive, exc_info=True)
        return ("unreadable", [os.path.basename(archive)])
    # Fail closed on a name or kind the extraction validator refuses, before
    # anything is created from any name (codex 4577029535).
    unsafe = [m.name for m in members if _unsafe_member_reason(m)]
    if unsafe:
        return ("unsafe", unsafe)
    # A name listed twice is two different files; only one can be the Image.
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in members:
        if entry.name in seen and entry.name not in duplicates:
            duplicates.append(entry.name)
        seen.add(entry.name)
    if duplicates:
        return ("duplicate", duplicates)
    names = {m.name + ("/" if m.is_dir and not m.name.endswith("/") else "")
             for m in members}
    try:
        owned = _image_members(archive, member, names)
    except _AmbiguousSheet as exc:
        return ("ambiguous", exc.members)
    except Exception:
        # An unreadable sheet: keep the archive.
        logger.debug("Could not read the sheet %s in %s", member, archive, exc_info=True)
        return ("unreadable", [os.path.basename(archive)])
    owned_dirs = set()
    for name in owned:
        folder = posixpath.dirname(name)
        while folder:
            owned_dirs.add(folder)
            folder = posixpath.dirname(folder)
    extras = []
    for name in sorted(names):
        if name.endswith("/"):
            if name.rstrip("/") in owned_dirs:
                continue
        elif name in owned:  # names are canonical: the validator passed them
            continue
        extras.append(name)
    return ("extra", extras) if extras else None


def _other_owner(part: str, source: Path) -> str | None:
    """Why *part* of *source* stays: another file names it too (a sheet, a
    playlist, a set; ``image_sets.owners_naming``), or that could not be told
    (fail closed). None when only *source* does."""
    from services import image_sets

    if not os.path.exists(part):
        return None  # reported as missing already
    boundary = lock_boundary_root(part)
    if boundary is None:
        # Outside every Volume: never walked (Codex 4655058131). The plan
        # already refuses such a path (unsafe_paths), and the job's delete
        # step checks the Volumes again.
        return None
    stop = str(boundary)
    name = os.path.basename(part)
    try:
        owners = image_sets.owners_naming(part, stop_at=stop)
    except (image_sets.SheetOwnerUnknown, OSError, ValueError) as exc:
        return f"{exc}, so whether another game needs {name} is unknown; it is kept"
    own = os.path.normcase(os.path.realpath(source))
    others = [o for o in owners if os.path.normcase(os.path.realpath(o)) != own]
    if not others:
        return None
    return f"{os.path.basename(others[0])} needs {name} too; it is kept"


def build_delete_plan(
    source_path: str, *, confine_to_volumes: bool = False,
) -> Dict[str, object]:
    """The files a delete-on-verify would remove for *source_path*.

    With *confine_to_volumes* (the delete-plan route), a resolved path outside
    every configured Volume goes to ``unsafe_paths`` instead of
    ``delete_paths``, so the plan is blocked and the Referrer scan never sees
    it. The job's own delete step checks each path against the Volumes too.
    """
    original_source = source_path
    is_archive_member = "::" in source_path
    plan_source = strip_archive_path(source_path) if is_archive_member else source_path
    source = Path(plan_source)
    base_dir = source.parent
    resolved: Set[str] = set()
    delete_paths: List[str] = []
    missing_paths: List[str] = []
    unsafe_paths: List[str] = []
    errors: List[str] = []
    warnings: List[str] = []

    def _add_path(path: Path) -> None:
        path_str = str(path)
        if os.path.islink(path_str):
            errors.append(f"Delete path is a symlink: {path_str}")
        real = os.path.realpath(path)
        if real in resolved:
            return
        resolved.add(real)
        # A track reached through a symlinked folder can resolve outside every
        # Volume. It is never deleted, and the Referrer scan never sees it.
        if confine_to_volumes and not is_within_configured_volumes(
            real, treat_archives=False,
        ):
            unsafe_paths.append(f"Delete path outside configured volumes: {real}")
            return
        delete_paths.append(real)
        if not os.path.exists(real):
            missing_paths.append(real)
        elif not os.path.isfile(real):
            errors.append(f"Delete path is not a file: {real}")

    try:
        _add_path(source)
        if is_archive_member:
            warnings.append(
                "Archive input detected; delete-on-verify will remove the entire archive"
            )
            # Never take anything else with it (#306): the archive goes only
            # when every member belongs to this Image.
            kept = archive_kept_summary(original_source)
            if kept:
                errors.append(f"The archive {kept}; it is kept")
            return {
                "source_path": original_source,
                "delete_paths": delete_paths,
                "missing_paths": missing_paths,
                "unsafe_paths": unsafe_paths,
                "errors": errors,
                "warnings": warnings,
            }
        ext = source.suffix.lower()
        parse = {".cue": cue_tracks, ".gdi": _gdi_tracks_from_text}.get(ext)
        readings: list[list[str]] = [[]]
        track_refs: list[str] = []
        if parse:
            # A decoded spelling naming another existing file than the bytes
            # chdman opens: refuse rather than delete a file it never read.
            ambiguity = sheet_ambiguity(source)
            if ambiguity:
                raise OSError(ambiguity)
            readings = [parse(text) for text in _read_sheet(source)]
            # Deletion targets come from the native spelling (the raw bytes as
            # the OS spells a file name), which is what chdman opened; never
            # from a decoded reading, which can name a different file.
            native = sheet_native_text(source)
            if native is None:
                raise OSError(f"Cannot read track sheet: {source}")
            track_refs = parse(native)
            if not track_refs:
                errors.append("No track references found")

        # Every spelling is checked too, so none can name a file outside.
        for spelling in readings:
            for ref in spelling:
                try:
                    _resolve_track_path(base_dir, ref)
                except ValueError as exc:
                    unsafe_paths.append(str(exc))
        for ref in track_refs:
            try:
                track_path = _resolve_track_path(base_dir, ref)
            except ValueError as exc:
                unsafe_paths.append(str(exc))
                continue
            _add_path(track_path)
            # A track another game's sheet names too is never deleted with
            # this one (#549 review): the plan is blocked instead.
            shared = _other_owner(str(track_path), source)
            if shared:
                errors.append(shared)
        unsafe_paths[:] = list(dict.fromkeys(unsafe_paths))

        # Sources that are really a *set* of files report the rest of the set
        # through the registry (jwud: a split Wii U dump's game_part2.wud …
        # game_part12.wud). Without this, deleting the source after a verified
        # conversion would remove the part the job named and orphan ~23 GB of
        # siblings. Imported lazily so this leaf utility keeps no import-time
        # dependency on the tool package that builds the registry.
        from services.tools import registry

        # Strict: a set whose members can't be told now (a RAR set whose
        # headers can't be read) blocks the plan rather than deleting the
        # named file alone.
        # A split output's later parts too (Game.iso.0 leads .1, .2): the set
        # goes whole or not at all.
        companions = [
            *registry.source_companions(str(source), strict=True),
            *registry.split_members(str(source)),
        ]
        for companion in dict.fromkeys(companions):
            _add_path(Path(companion))
            shared = _other_owner(companion, source)
            if shared:
                errors.append(shared)
    except Exception as exc:
        errors.append(str(exc))

    return {
        "source_path": original_source,
        "delete_paths": delete_paths,
        "missing_paths": missing_paths,
        "unsafe_paths": unsafe_paths,
        "errors": errors,
        "warnings": warnings,
    }


def collect_delete_paths(source_path: str) -> List[str]:
    plan = build_delete_plan(source_path)
    errors = list(plan.get("errors", []))
    unsafe = list(plan.get("unsafe_paths", []))
    missing = list(plan.get("missing_paths", []))

    if errors or unsafe:
        details = "; ".join(errors + unsafe)
        raise ValueError(details or "Unsafe delete plan")
    if missing:
        raise ValueError("Missing companion files; refusing to delete")

    return list(plan.get("delete_paths", []))


def build_delete_snapshot(source_path: str) -> Dict[str, object]:
    plan = build_delete_plan(source_path)
    errors = list(plan.get("errors", []))
    unsafe = list(plan.get("unsafe_paths", []))
    missing = list(plan.get("missing_paths", []))

    if errors or unsafe:
        details = "; ".join(errors + unsafe)
        raise ValueError(details or "Unsafe delete plan")
    if missing:
        raise ValueError("Missing companion files; refusing to delete")

    fingerprints: Dict[str, Dict[str, int]] = {}
    for path in plan.get("delete_paths", []):
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError("Missing companion files; refusing to delete") from exc
        if os.path.islink(path):
            raise ValueError(f"Delete path is a symlink: {path}")
        if not os.path.isfile(path):
            raise ValueError(f"Delete path is not a file: {path}")
        fingerprints[path] = {
            "size": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
            "inode": int(getattr(st, "st_ino", 0)),
            "device": int(getattr(st, "st_dev", 0)),
        }

    return {
        "paths": list(plan.get("delete_paths", [])),
        "fingerprints": fingerprints,
    }
