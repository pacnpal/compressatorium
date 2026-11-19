"""Known OS / NAS / filesystem junk names: the single source of truth.

Shared by the file browser (to hide clutter from listings), every tree walk
(``walk_tree``, the folder Library walk, the Switch key search), and archive readers (to hide
sidecars tucked inside zips/7z). Matched case-insensitively against a single
path component (file or directory name) via ``is_junk_entry``; whole archive
member paths go through ``is_junk_path``.
"""
from __future__ import annotations

from pathlib import PurePosixPath

# The hidden folder a conversion stages its output in beside the Source until
# its checks pass (``services.staging``, #398). Never a library entry: RomM
# skips dot-prefixed folders, and every listing and walk here hides it.
STAGE_DIR_NAME = ".compressatorium-stage"

# Exact names (lowercased).
_JUNK_EXACT = frozenset({
    # macOS
    ".ds_store", "__macosx", ".appledouble", ".lsoverride", "icon\r",
    ".documentrevisions-v100", ".fseventsd", ".spotlight-v100",
    ".temporaryitems", ".trashes", ".volumeicon.icns",
    ".com.apple.timemachine.donotpresent", ".appledb", ".appledesktop",
    "network trash folder", "temporary items", ".apdisk",
    # Windows
    "thumbs.db", "thumbs.db:encryptable", "ehthumbs.db", "ehthumbs_vista.db",
    "desktop.ini", "$recycle.bin", "recycler", "system volume information",
    # Synology / QNAP / NAS
    "@eadir", "@tmp", "#recycle", "@recycle", ".@__thumb", "#snapshot",
    # Linux / *nix
    "lost+found", ".directory", ".trash",
    # This app's own staging folder (#398)
    STAGE_DIR_NAME,
})
# Prefix patterns for families with variable suffixes.
# ``.romm_tmp_`` is RomM's own converter staging (``.romm_tmp_convert_<...>``):
# half-written work that is never a library entry. Only the prefix counts, so
# ``romm_tmp_x`` or ``x.romm_tmp_y`` stay visible.
_JUNK_PREFIXES = ("._", ".trash-", ".nfs", ".fuse_hidden", ".smbdelete", ".romm_tmp_")


def is_junk_entry(name: str) -> bool:
    """True for known OS/NAS metadata and trash entries that shouldn't show."""
    lower = name.lower()
    if lower in _JUNK_EXACT:
        return True
    return any(lower.startswith(prefix) for prefix in _JUNK_PREFIXES)


# Files a release ships beside its payload: the scene's .nfo, the .sfv of
# checksums, a readme .txt, a download .url (#476). Real files a user may want
# to see, so never hidden from a listing (``is_junk_entry``), but never the
# thing an archive was made to carry either.
RELEASE_SIDECAR_SUFFIXES = frozenset({".nfo", ".sfv", ".txt", ".url"})


def is_archive_sidecar(path: str) -> bool:
    """True for an archive member that is never its payload: junk
    (:func:`is_junk_path`) or a release sidecar (``.nfo``, ``.sfv``, ``.txt``,
    ``.url``). What a single-file extract counts its payload without."""
    if is_junk_path(path):
        return True
    return PurePosixPath(path).suffix.lower() in RELEASE_SIDECAR_SUFFIXES


def is_junk_path(path: str) -> bool:
    """True when any component of an archive member path is a junk entry.

    Archive members carry directories (``__MACOSX/._Game.gba``), so a basename
    check alone misses junk that lives in a junk *directory*. Splitting on the
    archive-native ``/`` separator and testing every component catches both the
    junk file and the junk folder.
    """
    return any(is_junk_entry(part) for part in PurePosixPath(path).parts)
