import os
import re
from pathlib import Path
from typing import Dict, List, Set

from utils.path_utils import is_within_configured_volumes, strip_archive_path
from utils.sheet_text import (
    cue_tracks,
    gdi_tracks,
    resolve_track,
    sheet_ambiguity,
    sheet_native_text,
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
