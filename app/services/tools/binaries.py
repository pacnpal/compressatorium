"""Is a tool's program there and runnable? One shared probe (#494).

A plugin that spawns a program declares it (``BaseTool.required_binaries``,
and ``verify_required_binaries`` where a check needs something else), and
``BaseTool.is_ready()`` / ``verify_is_ready()`` ask :func:`binary_problem`
for each. A tool whose program is missing, is not a file, or is not
executable for this process is not ready, with a reason the tool list, the
File info routes, the CLI and the sweeps all show through
``not_ready_message``.

What it checks: present and executable, nothing more. It never runs the
program. A version run would catch a binary built for the wrong architecture,
but several of these tools have no cheap, non-interactive way to say their
version, and a probe that spawns a process on every expiry costs far more than
a stat. rom-converto, a downloaded per-arch release, keeps its own probe that
runs ``--version`` once per file.

How it runs: the stat is :func:`run_detached`, never a shared pool worker. The
configured path may sit on a mount that has stopped answering, and every
readiness caller fans out over the whole registry, so a stuck stat must cost a
disposable thread (see ``ToolPlugin.is_ready``).

The cache rule: one answer per configured path, reused for
:data:`BINARY_PROBE_TTL_SECONDS`. ``GET /api/tools`` and every sweep ask each
tool each time, so without it each call would stat every binary again; with
it a binary that is installed, removed or ``chmod``-ed is noticed within that
window. While a probe of a path is running, every other caller on the same
event loop awaits that one probe rather than starting another, so a path on a
dead mount holds one written-off thread, not one per request. A probe that
raises (the detached-probe ceiling, an abandoned read) is not cached.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from services.subprocess_runner import run_detached

# How long one answer about a path is reused.
BINARY_PROBE_TTL_SECONDS = 30.0

_OK = "ok"
_MISSING = "missing"
_NOT_A_FILE = "not a file"
_NOT_EXECUTABLE = "not executable"
_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class RequiredBinary:
    """A program a tool runs, and where it is configured.

    ``path`` is the configured value: a path (anything with a separator) is
    checked where it is, a bare name is looked up on ``PATH`` as the spawn
    would. ``setting`` is the environment variable that sets it, named in the
    reason so the operator knows what to change; None for a program found only
    on ``PATH`` (z3ds's ``zstd``).
    """

    label: str
    path: str
    setting: str | None = None

    def reason(self, status: str, detail: str) -> str:
        """The user's words for *status* (one of this module's ``_*`` codes)."""
        bare = not _is_path(self.path)
        if status == _MISSING:
            hint = f" (set {self.setting} to where it is)" if self.setting else ""
            if bare:
                return f"{self.label} is not installed (no {self.path} on PATH){hint}"
            return f"{self.label} was not found at {self.path}{hint}"
        place = detail or self.path
        if status == _NOT_A_FILE:
            return f"{self.label} at {place} is not a file"
        if status == _NOT_EXECUTABLE:
            return f"{self.label} at {place} is not executable"
        return f"{self.label} at {self.path} could not be read: {detail}"


def _is_path(value: str) -> bool:
    return os.sep in value or bool(os.altsep and os.altsep in value)


def _now() -> float:
    return time.monotonic()


def _inspect(path: str) -> tuple[str, str]:
    """``(status, detail)`` for *path*. Blocking: run it detached.

    *detail* is the resolved file for a bare name, or the error for an
    unreadable path.
    """
    if not _is_path(path):
        # which() checks the same things for each PATH entry (a file, X_OK),
        # so a bare name that is not runnable is simply not found.
        found = shutil.which(path)
        return (_OK, found) if found else (_MISSING, "")
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return _MISSING, ""
    except OSError as exc:
        return _UNREADABLE, str(exc)
    if not stat.S_ISREG(st.st_mode):
        return _NOT_A_FILE, path
    if not os.access(path, os.X_OK):
        return _NOT_EXECUTABLE, path
    return _OK, path


_lock = threading.Lock()
# path -> (answered at, (status, detail))
_answers: dict[str, tuple[float, tuple[str, str]]] = {}
# path -> (loop, task) of the probe running for it
_running: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task]] = {}


def forget_binary_probes() -> None:
    """Drop every cached answer (tests, or after changing a path at runtime)."""
    with _lock:
        _answers.clear()


async def _probe(path: str) -> tuple[str, str]:
    try:
        answer = await run_detached(_inspect, path)
    finally:
        with _lock:
            running = _running.get(path)
            if running is not None and running[1] is asyncio.current_task():
                del _running[path]
    with _lock:
        _answers[path] = (_now(), answer)
    return answer


def _retrieve(task: asyncio.Task) -> None:
    # Every caller may have given up on a probe that then failed; read its
    # exception so it is not reported as never retrieved.
    if not task.cancelled():
        task.exception()


async def _status(path: str) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    with _lock:
        cached = _answers.get(path)
        if cached is not None and _now() - cached[0] < BINARY_PROBE_TTL_SECONDS:
            return cached[1]
        running = _running.get(path)
        if running is None or running[0] is not loop or running[1].done():
            task = loop.create_task(_probe(path))
            task.add_done_callback(_retrieve)
            _running[path] = (loop, task)
        else:
            task = running[1]
    # Shielded: a caller's bound (``tool_is_ready``) abandons its own wait,
    # never the probe the other callers are sharing.
    return await asyncio.shield(task)


async def binary_problem(binary: RequiredBinary) -> str | None:
    """Why *binary* can't run here, or None when it is there and executable."""
    status, detail = await _status(binary.path)
    return None if status == _OK else binary.reason(status, detail)


async def first_binary_problem(binaries: Iterable[RequiredBinary]) -> str | None:
    """The first of *binaries* (in declared order) that can't run, or None.

    Probed together: they are independent stats.
    """
    needed = tuple(binaries)
    if not needed:
        return None
    problems = await asyncio.gather(*(binary_problem(b) for b in needed))
    return next((p for p in problems if p is not None), None)
