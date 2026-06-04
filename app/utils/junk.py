"""Known OS / NAS / filesystem junk names: the single source of truth.

Shared by the file browser (to hide clutter from listings), the Switch key
search (to skip these dirs when walking volumes), and archive readers (to hide
sidecars tucked inside zips/7z). Matched case-insensitively against a single
path component (file or directory name) via ``is_junk_entry``; whole archive
member paths go through ``is_junk_path``.
"""
from __future__ import annotations

from pathlib import PurePosixPath

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
})
# Prefix patterns for families with variable suffixes.
_JUNK_PREFIXES = ("._", ".trash-", ".nfs", ".fuse_hidden", ".smbdelete")


def is_junk_entry(name: str) -> bool:
    """True for known OS/NAS metadata and trash entries that shouldn't show."""
    lower = name.lower()
    if lower in _JUNK_EXACT:
        return True
    return any(lower.startswith(prefix) for prefix in _JUNK_PREFIXES)


def is_junk_path(path: str) -> bool:
    """True when any component of an archive member path is a junk entry.

    Archive members carry directories (``__MACOSX/._Game.gba``), so a basename
    check alone misses junk that lives in a junk *directory*. Splitting on the
    archive-native ``/`` separator and testing every component catches both the
    junk file and the junk folder.
    """
    return any(is_junk_entry(part) for part in PurePosixPath(path).parts)
