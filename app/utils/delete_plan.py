import os
import re
from pathlib import Path
from typing import Dict, List, Set

from utils.path_utils import is_within_configured_volumes, strip_archive_path
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


# The shared sheet parsers (`utils.sheet_text`): the only FILE-line / GDI-line readers.
_SHEET_TEXT_PARSERS = {".cue": cue_tracks, ".gdi": _gdi_tracks_from_text}


def source_input_is_contained(path: str, within: str | None = None) -> bool:
    """Whether ``path`` resolves inside its containment root.

    ``within`` is an explicit root the caller owns and trusts: the job worker
    passes the folder it extracted an archive member into, which is not a
    Volume. The real path (symlinks followed) must sit under that root's real
    path. Without ``within`` the root is the configured Volumes. There is no
    third case: no root never means anything goes.
    """
    real = os.path.realpath(path)
    if within is None:
        return is_within_configured_volumes(real, treat_archives=False)
    root = os.path.realpath(within)
    try:
        return os.path.commonpath([real, root]) == root
    except ValueError:  # different drives
        return False


def sheet_reference_problems(sheet_path: str, *, within: str | None = None) -> list[str]:
    """Why a ``.cue`` / ``.gdi`` sheet is unsafe to hand to a converter, or ``[]``.

    The converter opens the files a sheet names itself, so each reference has
    to stay inside its containment root or the job would pack a file from
    elsewhere into its output. The root is the configured Volumes, or
    ``within`` when the caller passes one (the job's own extraction folder for
    a sheet taken out of an archive; see :func:`source_input_is_contained`).
    Unsafe: a reference that is absolute, one that leaves the sheet's folder
    (``..``), and one that resolves (through a symlink) outside the root.
    Every decoding of the sheet is checked (``utils.sheet_text``), so a name
    cannot hide in one spelling. A sheet that exists but cannot be read is
    unsafe too: nothing was checked. Empty for any other file. Blocking
    (reads the sheet, resolves each track).
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
                track = _resolve_track_path(sheet.parent, ref)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            if not source_input_is_contained(str(track), within):
                problems.append(f"Track reference resolves outside the volumes: {ref}")
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
