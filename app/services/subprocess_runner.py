"""Shared subprocess orchestration for conversion tools.

The implementation lives in this top-level ``services`` module (rather than
inside the ``services.tools`` package) so the service singletons can import it
during their own module initialization without triggering ``services.tools``'s
eager registry build, which imports the tool wrappers, which import back into
the still-initializing service modules.  ``services.tools.runner`` re-exports
these names so the design's documented path keeps working.

``SubprocessRunner.run`` collapses the line-buffered subprocess loop that was
duplicated, almost verbatim, in ``chdman``'s and ``dolphin_tool``'s
``convert()`` (spawn, ``nice`` wrap, PID tracking, ``\\r``/``\\n`` buffering,
stall timeout, cancel watcher, non-zero-exit error tail, final 100% emit).
dolphin's only addition over chdman is a periodic heartbeat, which is an opt-in
flag here.

``SubprocessRunner.run_capture`` is the one-shot counterpart for tools that
need a buffered ``(returncode, stdout, stderr)`` rather than streamed lines
(info / header / embedded-hash extraction). It shares the same PID tracking
and adds cancel-event + timeout handling (terminate -> kill), so an expensive
capture such as ``dolphin-tool verify --algorithm sha1`` aborts promptly when
a background scan/match job is cancelled instead of running to completion.

``ConversionCancelled`` is defined here (rather than in ``services.chdman``) so
the runner can raise it without importing back into the service that uses the
runner.  ``services.chdman`` re-exports it from this module, so its identity and
every existing import path (``from services.chdman import ConversionCancelled``)
are preserved.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import threading
import time
from collections.abc import AsyncGenerator, Callable, Mapping

from config import settings
from fastapi.concurrency import run_in_threadpool
from services.timeout_policy import compute_progress_stall_timeout


class ConversionCancelled(Exception):
    """Raised when a conversion is cancelled before completion."""


# --- Shared process-priority / timeout policy -----------------------------
#
# These knobs (nice level, I/O priority, info/verify timeouts) govern *every*
# conversion tool's subprocess, not just chdman. They live here -- read once in
# one place -- rather than being re-read with a chdman-flavoured name in each
# service. The shared default comes from the tool-neutral ``tool_*`` settings;
# an optional per-tool override (``<owner>_*``, e.g. ``dolphin_tool_nice``)
# takes precedence when set. ``owner`` matches the ``SubprocessRunner`` owner
# string each service constructs (``chdman``, ``dolphin_tool``, ``nsz``,
# ``z3ds``, ``maxcso``).


def _resolve_policy(key: str, owner: str | None):
    """Return the per-owner override for ``key`` if set, else the shared default.

    ``key`` is the bare setting suffix (``nice``, ``ioprio_class``,
    ``ioprio_level``, ``info_timeout``, ``verify_timeout``); the shared value is
    ``settings.tool_<key>`` and the optional override ``settings.<owner>_<key>``.
    """
    if owner is not None:
        override = getattr(settings, f"{owner}_{key}", None)
        if override is not None:
            return override
    return getattr(settings, f"tool_{key}")


def nice_value(owner: str | None = None) -> int | None:
    """Effective ``nice`` increment for ``owner`` (None disables renicing)."""
    return _resolve_policy("nice", owner)


def ioprio_prefix(owner: str | None = None) -> list[str]:
    """Return the ``ionice`` command prefix per the shared priority policy.

    Empty when I/O priority is unset for ``owner`` or ``ionice`` is unavailable.
    """
    ioprio_class = _resolve_policy("ioprio_class", owner)
    ioprio_level = _resolve_policy("ioprio_level", owner)
    if ioprio_class is None or ioprio_level is None:
        return []
    ionice = shutil.which("ionice")
    if not ionice:
        return []
    return [ionice, "-c", str(ioprio_class), "-n", str(ioprio_level)]


def nice_prefix(owner: str | None = None) -> list[str]:
    """Return the ``nice`` command prefix for ``owner``.

    Used by services that apply nice via a command wrapper instead of a
    ``preexec_fn`` (e.g. nsz, where forking a Python callable in a
    multithreaded process can deadlock the child before exec).
    """
    value = nice_value(owner)
    if value is None:
        return []
    nice = shutil.which("nice")
    if not nice:
        return []
    return [nice, "-n", str(value)]


def apply_nice(owner: str | None = None) -> None:
    """Renice the current process per the shared policy (for ``preexec_fn``)."""
    value = nice_value(owner)
    if value is None:
        return
    try:
        os.nice(value)
    except OSError:
        pass


def info_timeout(owner: str | None = None) -> int:
    """Effective ``info`` subprocess timeout in seconds (0 disables)."""
    return max(0, int(_resolve_policy("info_timeout", owner) or 0))


def verify_timeout(owner: str | None = None) -> int:
    """Effective ``verify`` subprocess timeout in seconds (0 disables)."""
    return max(0, int(_resolve_policy("verify_timeout", owner) or 0))


def output_size_progress(current: int, expected_size: int) -> int:
    """Estimate a 5-95% progress value from output-file growth.

    The shared progress estimate for tools whose subprocess draws a TTY
    progress bar that falls silent on a pipe (``maxcso``, ``nsz``, ``z3ds``):
    with no parseable percent in stdout, the growing output file *is* the
    progress signal, scored against an expected size (the input size times a
    per-direction ratio — roughly 0.5 for compress, 2.0 for decompress). The
    ratio only smooths the bar, so an inexact guess affects perceived
    smoothness, not correctness. Clamped to ``[5, 95]``; :meth:`SubprocessRunner.run`
    emits the terminal 100% on a clean exit. ``expected_size`` is floored at 1
    to avoid a divide-by-zero on a zero-byte input.
    """
    expected = expected_size if expected_size > 0 else 1
    return min(95, max(1, int(current / expected * 90) + 5))


class SubprocessRunner:
    """Spawns a conversion subprocess and streams progress updates.

    One instance per tool owns that tool's in-flight PID set, so both
    ``run()`` and the tool's separate ``verify_stream`` loop can register
    their subprocesses through the same store (``track_pid``/``untrack_pid``).
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()
        self._logger = logging.getLogger(f"chd.{owner}")

    @property
    def owner(self) -> str:
        """Tool identifier used to resolve per-tool priority/timeout overrides."""
        return self._owner

    def track_pid(self, pid: int) -> None:
        with self._pid_lock:
            self._active_pids.add(pid)

    def untrack_pid(self, pid: int) -> None:
        with self._pid_lock:
            self._active_pids.discard(pid)

    def active_pids(self) -> list[int]:
        with self._pid_lock:
            return list(self._active_pids)

    async def run_capture(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        cancel_event: asyncio.Event | None = None,
        stderr_to_stdout: bool = False,
    ) -> tuple[int | None, bytes, bytes]:
        """Run ``cmd`` to completion and capture ``(returncode, stdout, stderr)``.

        Shared one-shot counterpart to :meth:`run` for tools that need a
        buffered result rather than the streaming line loop (info / header /
        hash extraction). PID-tracked like :meth:`run`, so ``active_pids``
        and cancellation see these subprocesses too.

        The call races ``communicate()`` against an optional ``cancel_event``
        and ``timeout`` (seconds). If either fires before the process exits,
        the subprocess is terminated (TERM, then KILL after 5s) and the
        returncode is reported as ``None`` to signal the abort. ``stderr`` is
        folded into ``stdout`` when ``stderr_to_stdout`` is set (and the
        returned ``stderr`` is then empty).
        """
        # Honour the shared process-priority policy, same as the streaming
        # run(): renice via preexec and wrap with ionice. A captured command
        # (e.g. dolphin-tool verify reconstructing a full disc for DAT hashing)
        # is just as heavy as a conversion, so it must respect TOOL_NICE /
        # TOOL_IOPRIO_* instead of running at normal priority.
        cmd = ioprio_prefix(self._owner) + cmd

        def _preexec():
            apply_nice(self._owner)

        process = await asyncio.create_subprocess_exec(  # nosemgrep
            cmd[0], *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=(
                asyncio.subprocess.STDOUT
                if stderr_to_stdout
                else asyncio.subprocess.PIPE
            ),
            preexec_fn=_preexec if os.name == "posix" else None,
        )
        self.track_pid(process.pid)
        comm = asyncio.ensure_future(process.communicate())
        cancel_wait = (
            asyncio.ensure_future(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        try:
            waiters = [comm] + ([cancel_wait] if cancel_wait is not None else [])
            done, _pending = await asyncio.wait(
                waiters,
                timeout=timeout or None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if comm in done:
                stdout, stderr = comm.result()
                return process.returncode, stdout or b"", stderr or b""
            # Cancelled or timed out before the process exited; the finally
            # block below terminates it and drains ``comm``.
            return None, b"", b""
        finally:
            if cancel_wait is not None and not cancel_wait.done():
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
            if not comm.done():
                comm.cancel()
            # CancelledError is a BaseException, so it is NOT covered by
            # suppress(Exception); list it explicitly so a cancelled/timed-out
            # capture still returns (None, b"", b"") instead of propagating the
            # cancellation out of run_capture.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await comm
            self.untrack_pid(process.pid)

    async def run(
        self,
        cmd: list[str],
        *,
        input_path: str,
        output_path: str,
        parse_progress: Callable[[str], int | None],
        initial_progress: int = 0,
        cancel_event: asyncio.Event | None = None,
        heartbeat: bool = False,
        fail_label: str = "process",
        complete_message: str = "Conversion complete",
        cwd: str | None = None,
        output_growth_paths: Callable[[], list[str]] | None = None,
        size_progress: Callable[[int], dict | None] | None = None,
        nice_via_wrapper: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Spawn ``cmd``, stream stdout, and yield ``{"progress", "message"}``.

        ``parse_progress(line) -> int | None`` is the only per-tool knob in the
        common path; ``heartbeat`` enables dolphin's 2-second "Converting..."
        keep-alive.  Handles nice wrap, PID tracking, ``\\r``/``\\n`` line
        buffering, stall timeout via ``compute_progress_stall_timeout``, a
        cancel watcher (terminate -> kill), ``ConversionCancelled`` on request,
        a non-zero-exit ``RuntimeError`` carrying the output tail, and the final
        100% emit.

        ``output_growth_paths`` is an optional callable returning the set of
        files whose **summed** size is the growth signal for stall detection,
        replacing the single ``output_path`` probe. A tool whose output filename
        changes mid-run uses it so the probe keeps following the write — e.g.
        makeps3iso ``-s`` renames the base ``.iso`` to ``.iso.0`` and then writes
        ``.iso.1``/…, which the bare ``output_path`` probe would stop seeing.

        ``size_progress(output_size) -> dict | None`` turns the measured output
        size into a progress update for tools whose CLI prints no parseable
        percent (its TTY bar goes silent on a pipe): on each output-growth tick
        the runner calls it and yields its ``{"progress", "message"}``, clamped
        to never drop below the current floor. ``parse_progress`` still wins when
        it returns a percent. See :func:`output_size_progress` for the shared
        estimate. ``initial_progress`` seeds that floor with the caller's preamble
        (e.g. a service's "Starting..." yield at 1/5%) so an early non-parseable
        line cannot drop the bar below it.

        ``nice_via_wrapper`` skips the ``preexec_fn`` renice when the caller has
        already prefixed ``cmd`` with ``nice``/``ionice`` command wrappers
        (maxcso/nsz avoid ``preexec_fn``: forking a Python callable in this
        multithreaded process can deadlock the child before ``exec``).  ``env``
        is forwarded to the subprocess (nsz runs with a private keys-home env).
        """
        output_dir = os.path.dirname(output_path)
        if output_dir:
            await run_in_threadpool(os.makedirs, output_dir, exist_ok=True)

        def _preexec():
            apply_nice(self._owner)

        # nice/ionice: by default renice the forked child via ``preexec_fn``
        # (ionice, when used, is folded into ``cmd`` by the caller's
        # ``_build_command``). ``nice_via_wrapper`` callers have instead prefixed
        # ``cmd`` with ``nice``/``ionice`` command wrappers and must NOT also be
        # reniced via preexec — forking a Python callable in this multithreaded
        # process can deadlock the child before ``exec``.
        use_preexec = os.name == "posix" and not nice_via_wrapper

        # cmd is built from validated settings paths (no shell interpretation);
        # create_subprocess_exec passes the arg list directly (shell=False), so
        # there is no shell expansion / injection surface. Same call shape that
        # already lives unsuppressed in chdman/dolphin/z3ds; flagged here only
        # because the line is new in this extracted module.
        process = await asyncio.create_subprocess_exec(  # nosemgrep
            cmd[0], *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_preexec if use_preexec else None,
            cwd=cwd,
            env=env,
        )
        self.track_pid(process.pid)
        # Everything below runs under try/finally so that if the caller stops
        # iterating (generator aclose / task cancellation) or an unexpected
        # error fires, the subprocess, the cancel-watcher task and the PID entry
        # are always cleaned up rather than leaked.
        cancel_task = None
        try:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "Starting %s pid=%s cmd=%s",
                    self._owner, process.pid, " ".join(cmd),
                )

            stall_timeout = compute_progress_stall_timeout(
                input_path=input_path,
                base_timeout=getattr(settings, "progress_timeout", 0),
                timeout_per_gib=getattr(settings, "progress_timeout_per_gib", 0),
                timeout_cap=getattr(settings, "progress_timeout_cap", 0),
            )
            # Seed the progress floor with the caller's preamble (e.g. the
            # service's "Starting..." yield at 1/5%) so an early non-parseable
            # stdout line — which emits last_progress_value — can't drop the bar
            # below it before the first size-growth tick.
            last_progress_value = initial_progress
            last_output_size: int | None = None
            last_activity_at = time.monotonic()
            start = last_activity_at
            last_heartbeat_at = start

            cancelled_by_request = False
            if cancel_event:

                async def _cancel_watcher():
                    nonlocal cancelled_by_request
                    await cancel_event.wait()
                    if process.returncode is not None:
                        return
                    cancelled_by_request = True
                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            "Cancelling %s pid=%s", self._owner, process.pid,
                        )
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()

                cancel_task = asyncio.create_task(_cancel_watcher())

            buffer = ""
            output_lines: list[str] = []
            stall_error: str | None = None

            def _record_line(line: str) -> None:
                if not output_lines or output_lines[-1] != line:
                    output_lines.append(line)
                    if len(output_lines) > 30:
                        output_lines.pop(0)

            def _measure_output() -> int | None:
                # Summed size of the growth-probe target(s). Default is the
                # single output_path; output_growth_paths widens it to a set
                # whose total grows monotonically even as filenames change
                # mid-run (makeps3iso split). Returns None when nothing exists.
                paths = (
                    output_growth_paths() if output_growth_paths
                    else ([output_path] if output_path else [])
                )
                total = 0
                found = False
                for probe in paths:
                    try:
                        total += os.path.getsize(probe)
                        found = True
                    except OSError:
                        continue
                return total if found else None

            def _update_output_activity(now: float):
                nonlocal last_output_size, last_activity_at
                size = _measure_output()
                if size is None:
                    return
                if last_output_size is None or size > last_output_size:
                    last_output_size = size
                    last_activity_at = now

            def _size_update(now: float) -> dict | None:
                # Size-based progress for tools whose CLI prints no parseable
                # percent (maxcso/nsz/z3ds): on each output-growth tick, refresh
                # the stall clock, advance the progress floor, and return the
                # tool's {"progress","message"} update to yield. The emitted
                # progress is clamped to the floor so a size estimate can never
                # drop the bar below a higher parsed/seeded value. No-op (returns
                # None) for every existing caller, which leaves size_progress unset.
                nonlocal last_output_size, last_activity_at, last_progress_value
                if size_progress is None:
                    return None
                size = _measure_output()
                if size is None:
                    return None
                if last_output_size is not None and size <= last_output_size:
                    return None
                last_output_size = size
                last_activity_at = now
                update = size_progress(size)
                if update is not None:
                    pct = update.get("progress")
                    if isinstance(pct, int):
                        if pct < last_progress_value:
                            update = {**update, "progress": last_progress_value}
                        elif pct > last_progress_value:
                            last_progress_value = pct
                return update

            async def _check_stall(now: float) -> bool:
                nonlocal stall_error
                if stall_timeout <= 0:
                    return False
                _update_output_activity(now)
                if now - last_activity_at < stall_timeout:
                    return False
                stall_error = (
                    "Conversion stalled: no progress increase or output growth "
                    f"for {stall_timeout}s (progress={last_progress_value}%,"
                    f" output_size={last_output_size})"
                )
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                return True

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(100), timeout=2,
                    )
                except asyncio.TimeoutError:
                    if cancel_event and cancel_event.is_set():
                        break
                    now = time.monotonic()
                    update = _size_update(now)
                    if update is not None:
                        yield update
                    if await _check_stall(now):
                        break
                    if heartbeat and now - last_heartbeat_at >= 2:
                        elapsed = int(now - start)
                        yield {
                            "progress": last_progress_value,
                            "message": f"Converting... ({elapsed}s)",
                        }
                        last_heartbeat_at = now
                    continue
                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")

                while "\r" in buffer or "\n" in buffer:
                    sep = "\r" if "\r" in buffer else "\n"
                    parts = buffer.split(sep)
                    for part in parts[:-1]:
                        line = part.strip()
                        if not line:
                            continue
                        _record_line(line)
                        now = time.monotonic()
                        progress = parse_progress(line)
                        if progress is not None and progress > last_progress_value:
                            last_progress_value = progress
                            last_activity_at = now
                        yield {
                            "progress": (
                                progress
                                if progress is not None
                                else last_progress_value
                            ),
                            "message": line,
                        }
                    buffer = parts[-1]
                now = time.monotonic()
                update = _size_update(now)
                if update is not None:
                    yield update
                if await _check_stall(now):
                    break

            if buffer.strip():
                line = buffer.strip()
                _record_line(line)
                now = time.monotonic()
                progress = parse_progress(line)
                if progress is not None and progress > last_progress_value:
                    last_progress_value = progress
                    last_activity_at = now
                yield {
                    "progress": (
                        progress if progress is not None else last_progress_value
                    ),
                    "message": line,
                }
                update = _size_update(time.monotonic())
                if update is not None:
                    yield update
                await _check_stall(time.monotonic())

            await process.wait()
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "%s pid=%s exit=%s", self._owner, process.pid, process.returncode,
                )

            if stall_error:
                raise RuntimeError(stall_error)

            # Treat a set cancel_event as cancellation even when the child
            # already exited 0 before the watcher marked the request: the
            # watcher returns early once ``returncode`` is set, so on that race
            # ``cancelled_by_request`` can stay False. Without this, a cancelled
            # job whose process happened to finish first would be reported
            # complete — publishing the output and skipping the caller's cancel
            # cleanup — instead of raising ConversionCancelled.
            if cancelled_by_request or (
                cancel_event is not None and cancel_event.is_set()
            ):
                raise ConversionCancelled("Conversion cancelled")

            if process.returncode != 0:
                tail = "\n".join(output_lines[-6:])
                if tail:
                    raise RuntimeError(
                        f"{fail_label} failed with return code {process.returncode}."
                        f"\nLast output:\n{tail}",
                    )
                raise RuntimeError(
                    f"{fail_label} failed with return code {process.returncode}",
                )

            yield {"progress": 100, "message": complete_message}
        finally:
            self.untrack_pid(process.pid)
            if cancel_task:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
