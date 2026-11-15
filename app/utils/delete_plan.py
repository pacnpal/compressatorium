import os
import posixpath
from pathlib import Path
from typing import Dict, List, Sequence, Set

from utils.path_utils import strip_archive_path
from utils.sheet_text import (
    cue_tracks,
    gdi_tracks,
    resolve_track,
    sheet_readings,
    sheet_tracks,
)


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


def sheet_track_paths(sheet_path: str) -> list[str]:
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
    tracks = sheet_tracks(sheet, parse)
    return [str(track) for track in tracks] if tracks else []


def _image_members(archive: str, member: str, names: set[str]) -> set[str]:
    """The members that belong to the Image ``member``: itself, the tracks its
    sheet names, and its split parts. Raises when the sheet can't be read.

    The sheet is extracted to a temp dir and its references resolved there
    through ``resolve_track``, confined to that dir, so a ``..`` or absolute
    reference names nothing in the archive. Split parts come from the
    registry's ``source_companions``, by name, against the same temp layout.
    """
    import shutil
    import tempfile

    from services.archive import archive_service
    from services.tools import registry

    owned = {member}
    parent = posixpath.dirname(member)
    temp_root = tempfile.mkdtemp(prefix="delete-plan-")
    try:
        if posixpath.splitext(member)[1].lower() in {".cue", ".gdi"}:
            extracted, temp_dir = archive_service.extract_file(archive, member)
            try:
                parse = cue_tracks if member.lower().endswith(".cue") else _gdi_tracks_from_text
                # Every spelling of the sheet (chdman opens one of them).
                refs = [ref for text in _read_sheet(Path(extracted)) for ref in parse(text)]
                sheet_dir = os.path.dirname(extracted)
                for ref in refs:
                    try:
                        track = resolve_track(sheet_dir, ref, within=[temp_dir])
                    except ValueError:
                        continue  # names nothing in the archive
                    rel = os.path.relpath(str(track), sheet_dir).replace(os.sep, "/")
                    owned.add(posixpath.normpath(posixpath.join(parent, rel)))
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        # Split parts: lay the member names out as empty files, so the
        # registry's name math sees the same siblings the archive holds.
        for name in names:
            if name.endswith("/"):
                continue
            target = os.path.join(temp_root, *name.split("/"))
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                open(target, "ab").close()
            except OSError:
                continue
        for part in registry.source_companions(os.path.join(temp_root, *member.split("/"))):
            rel = os.path.relpath(part, temp_root).replace(os.sep, "/")
            owned.add(posixpath.normpath(rel))
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    return owned


def describe_extras(extras: List[str], limit: int = 3) -> str:
    """The first few extra members, for a summary line."""
    shown = ", ".join(extras[:limit])
    return shown + (", ..." if len(extras) > limit else "")


def archive_members_beyond_image(source_path: str) -> List[str]:
    """Members of an archive-member Source's archive that are not that Image.

    Deleting an archive member's Source deletes the whole archive, so it may
    go only when every member belongs to the Image that was converted: its own
    file, the tracks its sheet names (``resolve_track``) and its split parts
    (#306, review r9 C1). Anything else keeps the archive, whatever its type:
    another disc, a ROM, a loose ``.bin``, a nested archive, a PS3 folder, a
    ``readme.txt``, junk. There is no list of harmless sidecars. A directory
    entry counts only when nothing of the Image lives under it. An archive or
    sheet that can't be read answers with the archive's own name, so the plan
    refuses. Empty for a plain file.
    """
    if "::" not in source_path:
        return []
    archive, member = source_path.split("::", 1)
    from services.archive_members import read_archive_members

    try:
        members = read_archive_members(archive)
        names = {m.name + ("/" if m.is_dir and not m.name.endswith("/") else "")
                 for m in members}
        owned = _image_members(archive, member, names)
    except Exception:  # noqa: BLE001 - unknown contents: keep the archive
        return [os.path.basename(archive)]
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
        elif posixpath.normpath(name) in owned:
            continue
        extras.append(name)
    return extras


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


def build_delete_plan(source_path: str) -> Dict[str, object]:
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
            others = archive_members_beyond_image(original_source)
            if others:
                errors.append(f"The archive also holds {describe_extras(others)}; it is kept")
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
        readings = [parse(text) for text in _read_sheet(source)] if parse else [[]]
        track_refs: List[str] = readings[0]
        if parse and not track_refs:
            errors.append("No track references found")

        # Every spelling is checked (chdman opens one of them; the raw-byte one
        # for a sheet that is not UTF-8); the paths listed are the first's.
        for spelling in readings[1:]:
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
        unsafe_paths[:] = list(dict.fromkeys(unsafe_paths))

        # Sources that are really a *set* of files report the rest of the set
        # through the registry (jwud: a split Wii U dump's game_part2.wud …
        # game_part12.wud). Without this, deleting the source after a verified
        # conversion would remove the part the job named and orphan ~23 GB of
        # siblings. Imported lazily so this leaf utility keeps no import-time
        # dependency on the tool package that builds the registry.
        from services.tools import registry

        for companion in registry.source_companions(str(source)):
            _add_path(Path(companion))
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
