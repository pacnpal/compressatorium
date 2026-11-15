"""Process resource limits set once at startup (#306)."""
from __future__ import annotations

import logging

try:  # POSIX only; the app runs on Linux (and macOS for development).
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None

logger = logging.getLogger("compressatorium.process_limits")

# Used when the hard limit is "unlimited": setting the soft limit to infinity is
# refused on macOS and pointless on Linux.
_UNLIMITED_TARGET = 1 << 20


def raise_open_file_limit() -> int:
    """Raise the soft open-file limit (RLIMIT_NOFILE) to the hard limit.

    Docker on a systemd host starts processes with a soft limit of 1024 and a
    much higher hard one, and macOS shells start at 256. The server, the CLI,
    their subprocesses and SQLite all draw on that one budget. Called by the
    server's lifespan and by the CLI's `main`. Returns the soft limit in force
    afterwards; a refusal is logged and is not fatal.
    """
    if resource is None:
        return 0
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = _UNLIMITED_TARGET if hard == resource.RLIM_INFINITY else hard
    if soft != resource.RLIM_INFINITY and soft >= target:
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError) as exc:
        logger.warning("Could not raise the open-file limit from %s to %s: %s",
                       soft, target, exc)
        return soft
    return target
