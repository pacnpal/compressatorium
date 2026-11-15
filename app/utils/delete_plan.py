import os
import re
from pathlib import Path
from typing import Dict, List, Set

from utils.path_utils import strip_archive_path
from utils.sheet_text import cue_tracks, gdi_tracks, pick_existing, sheet_readings


_WIN_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _is_absolute_reference(ref: str) -> bool:
    ref = ref.strip()
    if not ref:
        return False
    if ref.startswith("~"):
        return True
    if ref.startswith("\\\\"):
        return True
    if _WIN_ABS_RE.match(ref):
        return True
    return Path(ref).is_absolute()


def _resolve_track_path(base_dir: Path, ref: str) -> Path:
    raw = ref.strip().strip('"')
    if not raw:
        raise ValueError("Empty track reference")
    normalized_ref = raw.replace("\\", "/")
    if _is_absolute_reference(normalized_ref):
        raise ValueError(f"Absolute track reference is not allowed: {raw}")

    base_resolved = Path(os.path.normpath(str(base_dir.absolute())))
    candidate = Path(os.path.normpath(os.path.join(str(base_resolved), normalized_ref)))
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"Track reference escapes source directory: {raw}") from exc

    return candidate


def _read_sheet(sheet_path: Path) -> list[str]:
    """The sheet's readings (``utils.sheet_text``); raises when unreadable."""
    readings = sheet_readings(sheet_path)
    if not readings:
        raise OSError(f"Cannot read track sheet: {sheet_path}")
    return readings


def _parse_cue_tracks(cue_path: Path) -> list[str]:
    return cue_tracks(_read_sheet(cue_path)[0])


def _parse_gdi_tracks(gdi_path: Path) -> list[str]:
    return _gdi_tracks_from_text(_read_sheet(gdi_path)[0])


def _gdi_tracks_from_text(text: str) -> list[str]:
    return [track.name for track in gdi_tracks(text)]


def sheet_track_paths(sheet_path: str) -> list[str]:
    """The track files a ``.cue`` or ``.gdi`` sheet names, resolved beside it.

    The same parsing and containment rules the delete plan uses, so a track
    reference that escapes the sheet's folder is left out, and the same
    decoding the disc-ID probe uses (``utils.sheet_text``): the first spelling
    of a name that exists wins. Tracks are listed whether or not they exist
    yet. Empty for any other file, or a sheet that
    cannot be read.
    """
    sheet = Path(sheet_path)
    parse = {".cue": cue_tracks, ".gdi": _gdi_tracks_from_text}.get(
        sheet.suffix.lower(),
    )
    if parse is None:
        return []
    readings = [parse(text) for text in sheet_readings(sheet)]
    tracks = pick_existing(readings, lambda ref: _resolve_track_path(sheet.parent, ref))
    return [str(track) for track in tracks]


def other_images_in_archive(source_path: str) -> List[str]:
    """Other Images held by the archive an archive-member Source lives in.

    Deleting an archive member's Source deletes the whole archive, so the
    archive may only go when this member is the only Image in it (#306). An
    Image is a member some Auto-eligible mode reads from an archive, the same
    test discovery uses, so a ``.cue``'s ``.bin`` tracks and a ``readme.txt``
    are not Images and junk (``__MACOSX/``, ``.DS_Store``) never counts. An
    archive that cannot be listed answers with itself, so the plan refuses.
    Empty for a plain file.
    """
    if "::" not in source_path:
        return []
    archive, member = source_path.split("::", 1)
    from services.archive_members import read_archive_members
    from services.tools import registry
    from utils.junk import is_junk_path
    from utils.path_utils import match_extension

    try:
        members = read_archive_members(archive)
    except Exception:  # noqa: BLE001 - unknown contents: keep the archive
        return [os.path.basename(archive)]
    readers = [
        registry.mode_reads_extensions(spec.mode)
        for spec in registry.mode_specs()
        if spec.auto_target and spec.allows_archive_input
    ]
    others = []
    for entry in members:
        if entry.is_dir or entry.name == member or is_junk_path(entry.name):
            continue
        if any(match_extension(entry.name, reads) is not None for reads in readers):
            others.append(entry.name)
    return others


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
            # Never take another Image with it (#306): the archive goes only
            # when this member is the only Image in it.
            others = other_images_in_archive(original_source)
            if others:
                shown = ", ".join(others[:3]) + (", ..." if len(others) > 3 else "")
                errors.append(
                    f"The archive also holds {shown}; it is kept",
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
        track_refs: List[str] = []
        if ext == ".cue":
            track_refs = _parse_cue_tracks(source)
        elif ext == ".gdi":
            track_refs = _parse_gdi_tracks(source)
        if ext in {".cue", ".gdi"} and not track_refs:
            errors.append("No track references found")

        for ref in track_refs:
            try:
                track_path = _resolve_track_path(base_dir, ref)
            except ValueError as exc:
                unsafe_paths.append(str(exc))
                continue
            _add_path(track_path)

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
