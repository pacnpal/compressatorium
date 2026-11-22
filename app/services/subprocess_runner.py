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
import contextvars
import functools
import logging
import os
import re
import shutil
import signal
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence

from config import settings
from logging_setup import get_logger
from services.timeout_policy import (
    compute_progress_stall_timeout,
    compute_size_scaled_timeout,
)

# A CSI escape sequence (colour, cursor movement), then any other control
# character but the newline and the tab: what a progress bar or a coloured
# message leaves in captured output (#455).
_ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_RUN_OF_BLANKS = re.compile(r"[ \t]+")


def clean_tool_output(text: str) -> str:
    """*text*, a tool's output or an error carrying it, fit for a job's error.

    Escape sequences and control characters go (a progress bar's backspaces,
    a coloured ``ERROR:``), a carriage return ends a line as a newline does,
    runs of blanks become one space, and blank lines are dropped (#455). The
    one place tool output is cleaned: the runner's own messages and every job
    error sink (``JobManager``) go through it, so every tool gets it.
    """
    text = _ESCAPE_SEQUENCE.sub("", text).replace("\r", "\n")
    lines = (
        _RUN_OF_BLANKS.sub(" ", _CONTROL_CHARACTERS.sub("", line)).strip()
        for line in text.split("\n")
    )
    return "\n".join(line for line in lines if line)


def killed_by_signal_message(label: str, returncode: int) -> str:
    """A tool ended by a signal, in plain words (#455).

    *returncode* is the negative return code ``asyncio`` reports. SIGKILL that
    the app didn't send (a cancel or a stall is reported as itself) is almost
    always the kernel's OOM killer, so it says so; any other signal is named.
    """
    number = -returncode
    if number == signal.SIGKILL:
        return f"{label} was killed (signal {number}), most likely out of memory"
    try:
        name = signal.Signals(number).name
    except ValueError:
        return f"{label} was killed (signal {number})"
    return f"{label} was killed (signal {number}, {name})"


class ConversionCancelled(Exception):
    """Raised when a conversion is cancelled before completion."""


# Hard bound on a filesystem probe taken on the spawn path, where there is no
# stall loop yet to rescue a wait that never returns.
_STAT_TIMEOUT = 10.0

# Ceiling on how many abandoned probes may be outstanding at once. Writing off a
# thread per wedged syscall is the deliberate trade below, but it has to have a
# floor under it: a request-driven probe (a verify route's path guard) can be
# retried indefinitely against a dead volume, and one leaked thread per attempt
# eventually exhausts memory or the process thread limit -- turning a broken
# mount into a dead application, which is the outcome all of this exists to
# prevent. In healthy operation these probes finish in microseconds and the
# count sits at zero, so reaching the ceiling is itself the diagnosis.
_MAX_DETACHED_PROBES = 64
_probe_slots = threading.Semaphore(_MAX_DETACHED_PROBES)

# Whatever the current operation has had to abandon: a child that outlived
# SIGKILL, or a detached read whose awaiter gave up while it was still blocked
# (one written-off thread each, holding a probe slot until the kernel unblocks
# it). Neither is an error on its own -- both are the deliberate trade for never
# hanging -- but a caller working through a list has to know, because the next
# file is on the same storage and will cost another one.
#
# A context variable rather than a global tally, because "did *this* verify
# abandon something" is the actual question. Globals cannot answer it once
# MAX_VERIFY_CONCURRENCY > 1: one request's wedged mount would mark another
# request's ordinary timeout as abandoned and stop its batch, possibly on
# perfectly healthy storage. ``asyncio.create_task`` copies the current context,
# so a producer task started inside ``collect_abandonment()`` reports into that
# sink and no other.
_abandon_sink: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "verify_abandon_sink", default=None,
)


@contextlib.contextmanager
def collect_abandonment():
    """Collect what the work started inside this block had to abandon.

    Yields the list it collects into; truthy afterwards means this operation
    left something stuck behind. Nests and interleaves safely: each caller gets
    its own sink, and work outside any block simply reports nowhere.
    """
    sink: list[str] = []
    token = _abandon_sink.set(sink)
    try:
        yield sink
    finally:
        _abandon_sink.reset(token)


class ToolUnavailable(RuntimeError):
    """A program, library or prerequisite a check needs is not available here.

    Raised where a check finds out it cannot run (zstd missing, py7zr missing),
    so ``verify_exception_event`` reports it as no verdict on the file.
    """


class StorageAbandoned(RuntimeError):
    """A child for this file outlived SIGKILL and is still running.

    Raised by a *caller* at the point it would otherwise act on a value that
    cannot distinguish "the storage stopped answering" from an ordinary result --
    a metadata read that came back empty, a tag that appears absent. The runner
    itself never raises it: an exception thrown from the ``finally`` an outer
    deadline unwinds through would mask whatever is already propagating, which is
    the case ``collect_abandonment()`` exists for. Deciding helpers raise it after
    an ``abandonment_checkpoint()`` tells them what just happened (issue #268).
    """


@contextlib.contextmanager
def abandonment_checkpoint():
    """Observe what *this step* abandoned, without hiding it from the caller.

    ``collect_abandonment()`` shadows an enclosing sink, so a compound operation
    cannot simply open one to make a mid-flight decision: the outer loop would
    stop seeing the abandonment and its stop-walking policy would never fire.
    This yields a list for the immediate decision and then forwards whatever it
    caught to the enclosing sink, so both fire.

    It exists because checking the sink only once, after a whole per-file
    operation returns, is too late whenever that operation *acts* on an
    intermediate result. Two cases in this codebase (issue #268):

    * A size probe for the verify bound is abandoned, the resolver falls back to
      the flat baseline, and the caller spawns the verifier anyway -- into
      storage already proven unresponsive. The design rule "re-check the cancel
      after resolving the bound, before spawning" applies to abandonment for the
      same reason, and more so: the reward is a child that blocks immediately
      and may outlive SIGKILL.
    * ``chdman dumpmeta`` is abandoned and reports "no GAME tag", which is
      indistinguishable from a genuinely untagged CHD -- so the caller writes a
      tag to a file the abandoned reader still holds, or falls through to a
      whole-disc sector read of the same dead mount.
    """
    outer = _abandon_sink.get()
    sink: list[str] = []
    token = _abandon_sink.set(sink)
    try:
        yield sink
    finally:
        _abandon_sink.reset(token)
        if outer is not None:
            outer.extend(sink)


def _note_abandoned(detail: str) -> None:
    sink = _abandon_sink.get()
    if sink is not None:
        sink.append(detail)


class ProbeCapacityExceeded(asyncio.TimeoutError):
    """Too many filesystem probes are already stuck to start another.

    Deliberately an ``asyncio.TimeoutError``: to every caller this means exactly
    what a timeout means -- "the filesystem did not answer" -- so each existing
    bounded-probe handler treats it correctly with no new branch. Only the
    message differs, because the cause an operator has to act on is different:
    the volume has been unresponsive for a while and probes have been piling up
    against it.

    ``asyncio.TimeoutError`` and not the builtin, because they are only the same
    class from 3.11 on; on 3.10 (which ``pyproject.toml`` still targets) the
    builtin is an ``OSError`` subclass that ``except asyncio.TimeoutError`` does
    not catch -- and every handler here catches the asyncio one. Inheriting both
    is not an option either: their C layouts conflict.
    """


def _probe_in_daemon_thread(func: Callable[[], object]) -> asyncio.Future:
    """Run a blocking filesystem call on a throwaway daemon thread.

    Deliberately not a ``ThreadPoolExecutor``. Its workers are joined during
    interpreter shutdown, so a probe wedged on a dead mount would stop the
    container from restarting cleanly -- and a pooled worker that never returns
    starves every probe queued behind it. A daemon thread does neither: it holds
    up nothing at exit and occupies no shared capacity. A call that never
    returns simply costs one written-off thread that dies with the process,
    which is the unavoidable price of a syscall Python cannot cancel.

    Bounded by :data:`_MAX_DETACHED_PROBES` outstanding at a time: the price is
    per-probe, so an unbounded number of attempts against a dead volume would
    otherwise be an unbounded number of threads. Raises
    :class:`ProbeCapacityExceeded` instead of starting the next one.
    """
    if not _probe_slots.acquire(blocking=False):
        raise ProbeCapacityExceeded(
            f"{_MAX_DETACHED_PROBES} filesystem probes are already blocked; "
            "the volume is not responding",
        )
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _settle(setter: Callable, value: object) -> None:
        if not future.done():
            setter(value)

    def _worker() -> None:
        try:
            try:
                result = func()
            except BaseException as exc:  # noqa: BLE001 - relayed to the awaiter
                setter, value = future.set_exception, exc
            else:
                setter, value = future.set_result, result
            with contextlib.suppress(RuntimeError):
                # RuntimeError: the loop closed while this thread was blocked.
                loop.call_soon_threadsafe(_settle, setter, value)
        finally:
            # Released only when the call actually returns, so the count
            # reflects probes that are *stuck*, not probes that were started.
            _probe_slots.release()

    try:
        threading.Thread(target=_worker, daemon=True, name="fs-probe").start()
    except RuntimeError as exc:
        # The OS thread limit: nothing started, so the slot is given back and
        # the caller hears what it hears when no slot is free (CodeRabbit
        # 4594855525).
        _probe_slots.release()
        raise ProbeCapacityExceeded(f"no filesystem probe thread could start: {exc}") from exc
    return future


async def _bounded_probe(func: Callable, *args: object, **kwargs: object) -> object:
    """Run a one-shot blocking filesystem call with a hard bound.

    Raises :class:`asyncio.TimeoutError` if it does not finish within
    :data:`_STAT_TIMEOUT`; every other exception propagates untouched. The
    timeout is signalled rather than folded into a ``None`` return because some
    of these calls (``os.makedirs``) return ``None`` on success, and each caller
    wants a different fallback anyway.

    A ``stat`` on an unresponsive mount blocks in uninterruptible I/O and cannot
    be cancelled -- Python can abandon the future but never the OS thread. Each
    call therefore gets its **own** disposable executor, shut down without
    joining: a wedged probe writes off exactly one thread and cannot occupy a
    worker that anything else depends on. Sharing one would let a hung
    input-side stat starve the continuous output-growth probe, leaving a job
    whose output is writing perfectly well with no status and no watchdog
    activity until the stall timeout killed it.
    """
    return await asyncio.wait_for(
        _probe_in_daemon_thread(functools.partial(func, *args, **kwargs)),
        timeout=_STAT_TIMEOUT,
    )


# --- Bounded cleanup of partial output ------------------------------------
#
# Every tool that writes its output in place sweeps the partial away when a run
# ends abnormally. That sweep runs *after* the runner has already given up, on
# the same storage the run just failed on -- so on a dead mount the unlink or
# rmtree blocks in uninterruptible I/O and the job never finalises. With
# MAX_CONCURRENT_JOBS defaulting to 1 and jobs running inline, that freezes the
# queue exactly the way the bounded reap (#263/#265) was written to prevent.
# These helpers are the one bounded seam every tool's cleanup goes through.

_cleanup_logger = get_logger("cleanup")
_probe_logger = get_logger("detached_probe")

# Hard bound on that sweep. Larger than _STAT_TIMEOUT because a sweep may unlink
# several files in a row (a split makeps3iso build) or walk a work dir, and each
# step is a separate round trip to storage that is, by assumption, already
# unhappy. Still a bound: a leftover partial file is a far smaller problem than
# a frozen queue, so cleanup gives up and the job finishes as failed.
_CLEANUP_TIMEOUT = 30.0


def _unlink_all(
    paths: tuple[str, ...],
    discover: Callable[[], Sequence[str]] | None,
    abandoned: threading.Event,
) -> None:
    """Unlink each path, reporting what could not be removed.

    Runs on the cleanup thread, which is also where ``discover`` is called:
    enumerating a set (makeps3iso's numbered split parts) *stats the same
    storage the sweep is about to unlink from*, so doing it on the event loop
    would leave the exact unbounded block this seam exists to remove.

    An absent path is the goal, not a failure, so ``FileNotFoundError`` is
    skipped; every other ``OSError`` (a directory shadowing the output, a
    permission problem) is collected and raised at the end so one bad path is
    logged without stopping the sweep of the rest.

    ``abandoned`` is checked before the discovery and before each unlink, and
    stops the sweep once the waiter has given up (timed out, or was cancelled).
    A sweep that outlives its bound must not keep deleting: the job has
    finalised and released those paths, the mount can recover minutes later, and
    by then a retry may own the names -- this thread would be deleting the *new*
    job's good output. The one unlink already issued when the bound expired
    cannot be recalled (an in-flight syscall is not cancellable), so that single
    path stays at risk; every path after it does not. On a responsive filesystem
    the whole sweep finishes long before either giving-up path is reached, so
    this costs the normal case nothing.
    """
    if abandoned.is_set():
        # Given up on before this thread even ran. Return without touching the
        # storage: the paths are no longer this job's, and the discovery is
        # itself a round trip to the volume in question -- one that would also
        # hold a probe slot the next job's cleanup may need.
        return
    targets = list(paths)
    if discover is not None:
        targets.extend(discover())
    failures: list[str] = []
    for path in targets:
        if abandoned.is_set():
            _cleanup_logger.warning(
                "Abandoned cleanup stopping before %s; it is no longer this "
                "job's to delete.", path,
            )
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{path} ({exc})")
    if failures:
        raise OSError("; ".join(failures))


def _rmtree_quietly(path: str, _abandoned: threading.Event) -> None:
    """``rmtree`` ignoring errors. Takes the flag for the ``work`` signature.

    Deliberately does *not* bail out when the flag is set, unlike
    :func:`_unlink_all`. The two guard different risks. A sweep of output paths
    stops because a retry can take those names and would lose real data; a work
    dir comes from ``mkdtemp``, so its name is unique to this run and no later
    job can ever own it -- there is nothing to protect, and skipping would only
    orphan the temp dir. Which is the likely case, not the rare one: the flag is
    set either by the full timeout (by which point ``rmtree`` has certainly
    started, making a check a no-op) or by a cancel that can land microseconds
    after dispatch -- and a cancelled job's work dir is exactly one that should
    still be swept.
    """
    shutil.rmtree(path, ignore_errors=True)


async def _bounded_cleanup(
    work: Callable[[threading.Event], None],
    what: str,
    timeout: float,
) -> bool:
    """Run a blocking cleanup call with a hard bound, raising only to cancel.

    Returns ``True`` when the removal finished, ``False`` when it did not --
    timed out, or failed. A cleanup problem is logged rather than raised: on an
    error path it must not replace the exception that actually explains the
    job's outcome, and in a ``finally`` after a *successful* conversion it must
    not fail a job whose output is already published. It must also never hold up
    finalising the job.

    ``CancelledError`` is the one exception that does propagate, like from any
    other ``await``. Swallowing it would defeat cancellation outright for the
    callers that have no pending exception to re-raise -- nsz, JWUDTool, romz's
    extract and chains all clean up in a ``finally`` that runs on the success
    path too, where consuming the cancel lets the job go on to be marked
    complete.

    The blocking call is *dispatched* before the wait, so a cancellation
    arriving afterwards cannot take back work the sweep already did -- the
    useful half of what the per-tool blocks bought by unlinking synchronously on
    the event loop (awaiting during a cancellation could be re-cancelled and
    skip the cleanup), without the unbounded block. What giving up *does* stop
    is the rest of the sweep: once this returns, the job finalises and stops
    owning those paths, so ``abandoned`` tells the thread not to unlink anything
    it has not already started. See :func:`_unlink_all`.
    """
    abandoned = threading.Event()

    def _run_work() -> None:
        try:
            work(abandoned)
        finally:
            if abandoned.is_set():
                _cleanup_logger.warning(
                    "Cleanup of %s finished after it was abandoned", what,
                )

    try:
        future = _probe_in_daemon_thread(_run_work)
    except (RuntimeError, ProbeCapacityExceeded) as exc:
        # Two ways the dispatch itself fails, both plausible precisely here
        # because a run of dead-mount sweeps deliberately writes threads off:
        # RuntimeError, the process cannot start another thread; and
        # ProbeCapacityExceeded, _MAX_DETACHED_PROBES already wedged against the
        # volume. (The latter is an asyncio.TimeoutError, not a RuntimeError, so
        # it needs naming here rather than riding along.) Neither dispatched anything,
        # so both are reported like any other cleanup failure rather than
        # escaping to stand in for the conversion's own outcome -- which on the
        # `finally` callers would fail a job whose output is already published.
        _cleanup_logger.warning(
            "Could not start the cleanup of %s: %s", what, exc,
        )
        return False
    try:
        await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        abandoned.set()
        _cleanup_logger.warning(
            "Giving up after %.0fs on removing %s; the job will finish and the "
            "file is left on disk. The storage it lives on is most likely "
            "unresponsive.",
            timeout, what,
        )
        return False
    except asyncio.CancelledError:
        # Stop the sweep before re-raising: the job is going away, so anything
        # it has not already started to unlink is no longer its to delete.
        abandoned.set()
        _cleanup_logger.warning("Interrupted while removing %s", what)
        raise
    except Exception as exc:  # noqa: BLE001 - cleanup never raises at its caller
        _cleanup_logger.warning("Could not remove %s: %s", what, exc)
        return False
    return True


async def remove_partial_output(
    *paths: str,
    discover: Callable[[], Sequence[str]] | None = None,
    label: str = "partial output",
    timeout: float | None = None,
) -> bool:
    """Bounded unlink of the file(s) a failed run may have left behind.

    Pass every path the run could have written. When the set is only knowable
    by probing the disk (a split build leaves numbered parts alongside the
    base), pass ``discover`` instead of enumerating first: it is called *inside*
    the bounded worker, so the probing is bounded along with the unlinking.
    ``label`` names the sweep in the log, and should carry the path when
    ``discover`` means there is nothing static to print.

    Returns ``True`` if the sweep finished, ``False`` if it did not; only
    ``CancelledError`` propagates. ``timeout`` defaults to
    :data:`_CLEANUP_TIMEOUT`, read at call time so the bound stays one knob
    rather than a value frozen into each signature.
    """
    if discover is None and not paths:
        return True
    what = f"{label} {', '.join(paths)}" if paths else label
    return await _bounded_cleanup(
        functools.partial(_unlink_all, paths, discover),
        what,
        _CLEANUP_TIMEOUT if timeout is None else timeout,
    )


async def remove_partial_tree(
    path: str,
    *,
    label: str = "work directory",
    timeout: float | None = None,
) -> bool:
    """Bounded ``rmtree`` of a private work/scratch dir, errors ignored.

    The file-level counterpart of :func:`remove_partial_output` for the tools
    that name their own output inside a temp dir (nsz, JWUDTool, romz extract,
    chains). ``ignore_errors`` matches what these call sites always wanted: the
    dir is private to the run and a stray file in it must not fail the job.
    """
    what = f"{label} {path}"
    return await _bounded_cleanup(
        functools.partial(_rmtree_quietly, path),
        what,
        _CLEANUP_TIMEOUT if timeout is None else timeout,
    )


# Bounds on reaping a subprocess: how long to let it exit on its own once its
# output stream closes, then the SIGTERM and SIGKILL grace periods.
# How long a stall check will wait for the first growth measurement to land
# before judging without one. Spans a few read-loop ticks: long enough that
# probe lag cannot fake a stall, short enough that a probe which never returns
# cannot disable the watchdog.
_FIRST_SAMPLE_GRACE = 6.0

# Minimum spacing between output-size samples. The read loop turns over once per
# stdout chunk, so without this a chatty converter would set the probe rate --
# hundreds of threads and metadata round-trips per second against the very mount
# whose responsiveness is in question.
_PROBE_INTERVAL = 2.0

# Floor for the stall timeout. Growth is sampled every _PROBE_INTERVAL and read
# a tick later, so the detector cannot resolve a window shorter than a few
# sample periods: below that, "no growth observed" only means "not looked at
# recently", and a converter writing steadily gets killed for it. Clamped rather
# than trusted, so no configuration can ask the watchdog for a precision it does
# not have.
_MIN_STALL_TIMEOUT = 3 * _PROBE_INTERVAL

_EXIT_GRACE = 60.0
# How finely the post-EOF exit grace is sliced. Each slice re-checks the cancel
# event and both verify deadlines, so this is the worst-case lag between a
# Cancel and the streaming verifier acting on it once its output has stopped.
_EXIT_POLL = 1.0
_TERM_GRACE = 5.0
_KILL_GRACE = 10.0


# --- Shared process-priority / timeout policy -----------------------------
#
# These knobs (nice level, I/O priority, info/verify timeouts) govern *every*
# conversion tool's subprocess, not just chdman. They live here -- read once in
# one place -- from the tool-neutral ``tool_*`` settings. There are no per-tool
# overrides (#542): the old ``COMPRESSATORIUM_<TOOL>_*`` names feed these.


def _resolve_policy(key: str):
    """The shared ``settings.tool_<key>`` value (``nice``, ``ioprio_class``, ...)."""
    return getattr(settings, f"tool_{key}")


def nice_value() -> int | None:
    """Effective ``nice`` increment (None disables renicing)."""
    return _resolve_policy("nice")


def ioprio_prefix() -> list[str]:
    """Return the ``ionice`` command prefix per the shared priority policy.

    Empty when I/O priority is unset or ``ionice`` is unavailable.
    """
    ioprio_class = _resolve_policy("ioprio_class")
    ioprio_level = _resolve_policy("ioprio_level")
    if ioprio_class is None or ioprio_level is None:
        return []
    ionice = _WRAPPERS.get("ionice")  # cache only: see _resolve_wrappers
    if not ionice:
        return []
    # -t: best effort, like nice's renice (CodeRabbit 4615955439). A class the
    # kernel denies (realtime without CAP_SYS_ADMIN) no longer stops ionice
    # before it runs the tool with ionice's own exit 1, read as the tool's.
    return [ionice, "-t", "-c", str(ioprio_class), "-n", str(ioprio_level)]


def nice_prefix() -> list[str]:
    """Return the ``nice`` command prefix.

    Empty when renicing is off or ``nice`` is unavailable, and
    for a level this process can't apply (:func:`_can_renice`): ``nice`` would
    only warn and run the tool anyway, and its warning would land in output a
    caller parses (rom-converto's ``--version`` readiness probe). The old
    ``preexec_fn`` renice dropped that failure silently too.
    """
    value = nice_value()
    if value is None or not _can_renice(value):
        return []
    nice = _WRAPPERS.get("nice")  # cache only: see _resolve_wrappers
    if not nice:
        return []
    return [nice, "-n", str(value)]


def _has_cap_sys_nice() -> bool:
    """Whether this process holds CAP_SYS_NICE (bit 23 of CapEff), read once.
    Being root is not the test (codex 4615398086): a container's root often
    lacks it. Where /proc can't say, root is assumed to hold it."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) >> 23 & 1)
    except (OSError, ValueError):
        pass
    return os.geteuid() == 0


_CAP_SYS_NICE = _has_cap_sys_nice()


def _can_renice(increment: int) -> bool:
    """Whether ``nice -n increment`` would apply, by the kernel's rule:
    raising niceness always may; lowering it to N needs CAP_SYS_NICE or
    ``20 - N`` within RLIMIT_NICE. A couple of syscalls, no filesystem."""
    if increment >= 0 or _CAP_SYS_NICE:
        return True
    try:
        import resource

        target = max(-20, os.getpriority(os.PRIO_PROCESS, 0) + increment)
        soft = resource.getrlimit(resource.RLIMIT_NICE)[0]
    except (AttributeError, ImportError, OSError):
        return False
    return soft == resource.RLIM_INFINITY or 20 - target <= soft


_WRAPPERS: dict[str, str | None] = {}


def _wrapper_path(name: str) -> str | None:
    """Where ``nice``/``ionice`` is, looked up once per process and only once
    its setting asks for it (a PATH scan stats directories, so it isn't
    repeated per spawn, nor made at import when no priority is enabled).
    Absolute, so a tool started in another folder (romz's ``cwd``) still
    finds it when PATH held a relative entry. Blocking: only
    :func:`_resolve_wrappers` calls it, off the event loop. ``nice_prefix`` and
    ``ioprio_prefix`` read the cache alone, a wrapper not yet looked up being
    no wrapper (CodeRabbit 4616275683), so building a prefix never scans."""
    if name not in _WRAPPERS:
        found = shutil.which(name)
        _WRAPPERS[name] = os.path.abspath(found) if found else None
    return _WRAPPERS[name]


async def _resolve_wrappers(owner: str | None) -> bool:
    """Look up, off the event loop and bounded (``_bounded_probe``), each
    wrapper *owner*'s settings ask for that isn't cached yet (CodeRabbit
    4616166783). False when a lookup stalled on a PATH entry that doesn't
    answer: that spawn runs with no wrapper, at normal priority, and nothing
    is cached, so a later spawn tries again."""
    wanted = []
    if nice_value() is not None:
        wanted.append("nice")
    if None not in (_resolve_policy("ioprio_class"), _resolve_policy("ioprio_level")):
        wanted.append("ionice")
    for name in wanted:
        if name in _WRAPPERS:
            continue
        if name in _LOOKING_UP:
            # One lookup per wrapper at a time, even after its caller gave up
            # on it (CodeRabbit 4616898109): a PATH entry that never answers
            # costs one probe thread, not one per spawn until the probe slots
            # run out. This spawn runs at normal priority meanwhile.
            return False
        _LOOKING_UP.add(name)

        def lookup(name: str = name) -> str | None:
            try:
                return _wrapper_path(name)
            finally:
                _LOOKING_UP.discard(name)  # the thread finished, answer or not

        try:
            await _bounded_probe(lookup)
        except asyncio.TimeoutError as exc:
            if isinstance(exc, ProbeCapacityExceeded):
                _LOOKING_UP.discard(name)  # never started
            logging.getLogger(f"chd.{owner}").warning(
                "Looking up %s on PATH did not answer; running at normal priority", name,
            )
            return False
    return True


_LOOKING_UP: set[str] = set()


async def _wrapped_command(
    owner: str | None, cmd: list[str], *, wrap: bool = True,
) -> tuple[list[str], bool]:
    """The argv every spawn method runs, built here for all of them: *cmd*
    behind the priority wrappers *owner*'s settings ask for, and whether it
    got them. The wrappers come from the cache, looked up off the loop first
    (:func:`_resolve_wrappers`); a stalled lookup means no wrappers. With
    ``wrap=False`` (a spawn whose exit code is a verdict) it is *cmd* alone, a
    new list all the same: a verdict never comes from a process started
    through them, see :func:`_prioritize_child`."""
    resolved = await _resolve_wrappers(owner)
    prefix = priority_prefix() if wrap and resolved else []
    return prefix + cmd, bool(prefix)


def _prioritize_child(pid: int) -> None:
    """Apply the priority policy to a child spawned bare, best effort.

    The verify paths (``capture_verify``, ``run_verify``, ``hash_stdout``)
    spawn the tool directly: ``nice``/``ionice`` exec through ``execvp``,
    which hands a file the kernel refuses (empty, a bare ``#!``) to
    ``/bin/sh``, and its exit 0 would read as a pass (CodeRabbit 4616898119).
    A direct exec raises ENOEXEC instead. The nice level is then set on the
    child by ``setpriority`` (the same increment over this process that
    ``nice -n`` gives; a level :func:`_can_renice` refuses is left off), and
    the I/O class by ``ionice -p <pid>`` through the bounded probe. A failure
    of either leaves the child at normal priority."""
    value = nice_value()
    if value is not None and _can_renice(value):
        try:
            own = os.getpriority(os.PRIO_PROCESS, 0)
            os.setpriority(os.PRIO_PROCESS, pid, max(-20, min(19, own + value)))
        except OSError:
            pass
    # Never awaited by the spawn (CodeRabbit 4617379646): the nice level is a
    # syscall, and `ionice -p` runs as a task of its own, so a stalled one
    # can't hold back the verify's deadline or cancel, which start at once.
    # Its cache was filled by the argv build before the spawn (no lookup
    # here). Kept out of the shared probe pool (CodeRabbit 4617447724): an
    # asyncio child of its own, bounded and killed on a stall, and at most
    # _MAX_IONICE_TASKS at once; past that the child gets no I/O priority.
    prefix = ioprio_prefix()
    if prefix and not _IONICE_STUCK and len(_IONICE_TASKS) < _MAX_IONICE_TASKS:
        task = asyncio.ensure_future(_ionice_child([*prefix, "-p", str(pid)]))
        _IONICE_TASKS.add(task)
        task.add_done_callback(_ionice_done)


# At most this many `ionice -p` steps in flight, each given this long before
# it is killed; I/O priority is best effort, never a cost to anything else.
_MAX_IONICE_TASKS = 4
_IONICE_SECONDS = 5.0
_IONICE_TASKS: set[asyncio.Future] = set()
# Set for good once an `ionice` child outlives SIGKILL: its slot can't be
# counted back, and I/O priority isn't worth pursuing on that storage
# (CodeRabbit 4617587026).
_IONICE_STUCK = False


async def _ionice_child(argv: list[str]) -> None:
    """Run ``ionice ... -p <pid>`` as an asyncio child, bounded: one that
    doesn't finish in :data:`_IONICE_SECONDS` is killed and reaped (bounded
    too). Every failure is dropped."""
    try:
        process = await asyncio.create_subprocess_exec(  # nosemgrep
            argv[0], *argv[1:],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return
    try:
        await asyncio.wait_for(process.wait(), _IONICE_SECONDS)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), _IONICE_SECONDS)
        except asyncio.TimeoutError:
            global _IONICE_STUCK
            _IONICE_STUCK = True
            logging.getLogger("chd.ionice").warning(
                "ionice pid %s outlived SIGKILL; I/O priority is off for "
                "verifiers until restart", process.pid,
            )
    finally:
        if process.returncode is None:  # cancelled (the loop is closing)
            with contextlib.suppress(ProcessLookupError):
                process.kill()


def _ionice_done(task: asyncio.Future) -> None:
    _IONICE_TASKS.discard(task)
    if not task.cancelled():
        task.exception()  # best effort: retrieved, never raised


def _forget_wrappers() -> None:
    """A wrapped spawn failed to start: the cached ``nice``/``ionice`` may be
    gone (CodeRabbit, review 5960207187), so the next spawn looks them up
    again rather than failing until the process restarts."""
    _WRAPPERS.clear()


def _note_exit(returncode: int | None, wrapped: bool) -> None:
    """A wrapped run that exited 126/127 may mean a cached wrapper is gone:
    the outer ``nice`` starts and its inner ``ionice`` doesn't (codex
    4617171876). Forget the cache, so the next spawn looks them up again; a
    tool's own 126/127 costs one extra lookup."""
    if wrapped and returncode in (126, 127):
        _forget_wrappers()


def priority_prefix() -> list[str]:
    """``nice`` + ``ionice`` command wrappers (issue #444).

    The only way the runner applies the priority policy to a child, never a
    ``preexec_fn``: this process is multithreaded (threadpools, the SSE routes),
    and forking a Python callable from a multithreaded parent can deadlock the
    child on a lock it inherited before ``exec``. That hangs inside
    ``create_subprocess_exec``, before the PID is tracked or any bound is
    installed, so nothing could rescue it. ``nice``/``ionice`` only exec; either
    absent, its prefix is empty and the child runs at normal priority.
    """
    return nice_prefix() + ioprio_prefix()


def info_timeout() -> int:
    """Effective ``info`` subprocess timeout in seconds (0 disables)."""
    return max(0, int(_resolve_policy("info_timeout") or 0))


def verify_timeout() -> int:
    """Baseline ``verify`` timeout in seconds (0 disables).

    The *baseline* only. Callers that know which file is being verified should
    use :func:`resolve_verify_timeout`, which adds the per-GiB allowance that
    makes one bound workable across a 400 MB CIA and a 90 GB PS3 ISO.
    """
    return max(0, int(_resolve_policy("verify_timeout") or 0))


def verify_stall_timeout() -> int:
    """Effective no-output stall bound for a streaming verify (0 disables)."""
    return max(0, int(_resolve_policy("verify_progress_timeout") or 0))


def _verify_timeout_sync(path: str, size: int | None = None) -> int:
    return compute_size_scaled_timeout(
        path=path,
        base_timeout=verify_timeout(),
        timeout_per_gib=_resolve_policy("verify_timeout_per_gib"),
        timeout_cap=_resolve_policy("verify_timeout_cap"),
        size=size,
    )


async def resolve_verify_timeout(
    path: str,
    *,
    cancel_event: asyncio.Event | None = None,
    size: int | None = None,
) -> int:
    """Effective wall-clock bound for verifying ``path`` (0 disables).

    The single source of truth for "how long may a verify run": the configured
    baseline plus a per-GiB allowance for the file actually being read, capped
    (see :func:`services.timeout_policy.compute_size_scaled_timeout`). Every
    tool's verify resolves its own bound through this, and ``job_manager``
    resolves the same value to bound the whole ``verify()`` call as a backstop
    for tools whose verify never spawns a subprocess.

    Sizing ``path`` stats it, which on a dead mount blocks in uninterruptible
    I/O -- exactly the failure this bound exists to survive -- so the stat is
    taken through the bounded detached seam and a probe that does not answer
    falls back to the flat baseline. A bound that cannot be sized still applies.

    ``cancel_event`` is raced against the stat for the same reason
    :func:`verify_preflight` races it: this runs *before* the verify that would
    observe the cancel, so without it a Cancel pressed while sizing an output on
    a dead mount left the job -- and the single-slot dispatcher behind it -- on
    "Cancelling..." for the full probe bound. A cancelled sizing falls back to
    the flat baseline too: the caller is about to be told the run was cancelled,
    so the number only has to exist.

    *size*, when given, is what the verify reads instead of the file's own
    size: an archive's declared uncompressed size (romz's ``7z t``, #431).
    """
    try:
        return await asyncio.wait_for(
            run_detached(
                _verify_timeout_sync, path,
                *(() if size is None else (size,)), cancel_event=cancel_event,
            ),
            timeout=_STAT_TIMEOUT,
        )
    except (asyncio.TimeoutError, ReadCancelled):
        return verify_timeout()


class ReadCancelled(Exception):
    """A detached read was abandoned because its ``cancel_event`` fired.

    Distinct from :class:`ConversionCancelled` (which belongs to the conversion
    pipeline) so a verify can translate it into its own terminal
    ``{"cancelled": True}`` event rather than letting it read as a failure.
    """


# How long a cancelled awaitable gets to unwind (stop its child, leave a lane
# queue) before :func:`until_cancelled` stops waiting for it.
CANCEL_SETTLE_SECONDS = 5.0


async def until_cancelled(
    awaitable: Awaitable,
    cancel_event: asyncio.Event | None,
    *,
    settle: float = CANCEL_SETTLE_SECONDS,
    discard: Callable[[object], None] | None = None,
) -> object:
    """Await *awaitable*, or raise :class:`ReadCancelled` as soon as
    *cancel_event* fires (#388).

    For a call that does not watch the event all the way down (a lane wait, a
    tool's hash under its own bound): the caller's Cancel ends the wait now,
    not when the call's own timeout runs out. On a cancel the pending task is
    cancelled and awaited for at most *settle* seconds, so whatever it holds
    (a lane, a child) is let go before this returns. With no event, this is a
    plain await of *awaitable*.

    Every way out that does not hand the result to the caller gives it to
    *discard* (a lane token to give back): the event's Cancel, and the
    caller's own task being cancelled, even in the window where the call had
    already finished but the caller had not yet resumed (Codex 4590926157). A
    call still running then is cancelled, and its result, should it arrive
    after all, is discarded as it lands.
    """
    if cancel_event is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(cancel_event.wait())
    delivered = False
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            delivered = True
            return task.result()
        task.cancel()
        await asyncio.wait({task}, timeout=max(0.0, settle))
        raise ReadCancelled("Cancelled while waiting")
    finally:
        waiter.cancel()
        if not delivered:
            _discard_when_done(task, discard)


def _discard_when_done(task: asyncio.Future, discard: Callable[[object], None] | None) -> None:
    """Give *task*'s result to *discard*, now or when it lands: nobody will
    receive it. A task still running is cancelled first."""
    def give_back(done: asyncio.Future) -> None:
        if discard is None or done.cancelled() or done.exception() is not None:
            return
        discard(done.result())

    if task.done():
        give_back(task)
        return
    task.cancel()
    task.add_done_callback(give_back)


async def bounded_path_check(func: Callable, *args: object, **kwargs: object) -> object:
    """A one-shot path predicate (``isfile``, within-volumes) under a hard bound.

    The verify *routes* validate a path before they start anything, and those
    checks are ordinary blocking stats: on an unresponsive volume they block in
    uninterruptible I/O, ahead of every bound the verify itself carries, and in
    a shared pool they take a worker with them. Running them here bounds the
    wait and keeps the abandoned thread off any pool that matters.

    Raises :class:`asyncio.TimeoutError` when the volume does not answer.
    """
    return await _bounded_probe(func, *args, **kwargs)


def start_detached(func: Callable, *args: object, **kwargs: object) -> asyncio.Future:
    """Start a blocking call on a detached daemon thread *now*, before the
    caller's next await, and return the future that settles with it.

    For work that must not be lost to a cancel: ``run_detached`` starts its
    thread only once awaited, so a cancel that lands first means it never
    starts. Once this returns, the thread owns the call. Raises
    :class:`ProbeCapacityExceeded` like ``run_detached`` when no slot is free.
    Must be called on the event loop. An exception nobody awaits is consumed,
    not reported as never retrieved.
    """
    future = _probe_in_daemon_thread(functools.partial(func, *args, **kwargs))
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    return future


def start_detached_or_own_thread(
    func: Callable, *args: object, **kwargs: object,
) -> asyncio.Future:
    """:func:`start_detached`, or, when no detached slot is free, a daemon
    thread of its own (the fallback ``_close_stage`` and
    ``_let_go_in_background`` take): either way the call starts now, before
    the caller's next await, and the future settles with it. For work that
    must start whatever happens next, such as keeping the only copy of a
    checked output out of every sweep (#391). Must be called on the event
    loop."""
    try:
        return start_detached(func, *args, **kwargs)
    except ProbeCapacityExceeded:
        pass
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def settle(result: object, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def run() -> None:
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            # Relayed to whoever awaits the future, which acts on it.
            _cleanup_logger.debug("A call on its own thread raised; relaying it", exc_info=True)
            answer: tuple[object, BaseException | None] = (None, exc)
        else:
            answer = (result, None)
        try:
            loop.call_soon_threadsafe(settle, *answer)
        except RuntimeError:
            pass  # the loop is closed: nobody is waiting any more

    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError as exc:
        # The OS thread limit: nothing started, so nothing will settle the
        # future. Said plainly, for the caller to fall back on.
        raise NoThreadAvailable(f"no thread could start: {exc}") from exc
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    return future


class NoThreadAvailable(RuntimeError):
    """No thread could start for :func:`start_detached_or_own_thread`: every
    detached slot is in use and the OS refused a thread of its own. The call
    never ran."""


async def run_detached_to_end(func: Callable, *args: object) -> object:
    """A step that changes something, run to its end and never abandoned.

    On a detached thread, or one of its own when none is free
    (:func:`start_detached_or_own_thread`: the step always starts), in the
    caller's context. A cancel waits for the step to finish before it
    propagates, every further cancel too, so what the step did is known
    before anything acts on the cancel (the RomM swap's moves, #391; an Image
    rename's moves, #425). *func* should record its own outcome on its thread
    (a flag, a journal row)."""
    future = start_detached_or_own_thread(contextvars.copy_context().run, func, *args)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({future})
        raise


# How long :func:`start_detached_eventually` waits between attempts while no
# thread can start at all.
NO_THREAD_RETRY_SECONDS = 1.0


def start_detached_eventually(
    func: Callable, *args: object, **kwargs: object,
) -> asyncio.Future | None:
    """:func:`start_detached_or_own_thread`, or, when no thread can start at
    all (the OS thread limit), the same again on the loop every
    :data:`NO_THREAD_RETRY_SECONDS` until one does. For cleanup that must
    happen and that the caller no longer owns, such as letting go of locks
    (CodeRabbit 4594855525). Returns the started call's future, or None when
    it was left to the retries. Never raises :class:`NoThreadAvailable`.
    Must be called on the event loop."""
    try:
        return start_detached_or_own_thread(func, *args, **kwargs)
    except NoThreadAvailable:
        _cleanup_logger.warning(
            "No thread could start for %s; trying again every %gs",
            getattr(func, "__name__", func), NO_THREAD_RETRY_SECONDS,
        )
    loop = asyncio.get_running_loop()

    def again() -> None:
        try:
            start_detached_or_own_thread(func, *args, **kwargs)
        except NoThreadAvailable:
            loop.call_later(NO_THREAD_RETRY_SECONDS, again)

    loop.call_later(NO_THREAD_RETRY_SECONDS, again)
    return None


async def run_detached(
    func: Callable,
    *args: object,
    cancel_event: asyncio.Event | None = None,
    **kwargs: object,
) -> object:
    """Run a blocking call off the event loop **without** a shared pool worker.

    The un-pooled counterpart of ``run_in_threadpool`` / ``asyncio.to_thread``
    for work that may never return: a verify's whole-file reads (the WUX index
    scan, an archive listing, a header probe, a PARAM.SFO readback). Cancelling
    a thread is impossible — Python can abandon the future, never the OS thread
    — so the only question is *whose* thread is abandoned. In a shared pool it
    is one of a small fixed set: now that verify is genuinely cancellable and
    bounded, a client disconnecting repeatedly against an unresponsive mount
    would strand one pooled worker per attempt and eventually starve every
    unrelated offload in the process. Here it is a throwaway daemon thread,
    which holds up nothing at interpreter exit and occupies no shared capacity.

    Unbounded by design: the caller supplies the bound (a route deadline, the
    job's ``verify_timeout``), because the right limit depends on the file.

    ``cancel_event`` is raced against the read and raises :class:`ReadCancelled`
    the moment it fires. The read itself cannot be stopped — it is abandoned, on
    its own disposable thread — but the *awaiter* returns immediately, which is
    the part that matters: without it, pressing Cancel during a long index scan
    or archive listing left the job (and the single-slot dispatcher behind it)
    on "Cancelling..." until the outer bound expired, possibly hours later.
    """
    if cancel_event is not None and cancel_event.is_set():
        # Before taking a probe slot, not after: starting the call would write
        # off a thread (and one of the process-wide slots) on a dead mount for a
        # result nobody wants, and enough cancelled attempts that way would
        # exhaust the ceiling and start failing path checks on healthy volumes.
        raise ReadCancelled("Read cancelled")
    future = _probe_in_daemon_thread(functools.partial(func, *args, **kwargs))
    waiter = (
        asyncio.ensure_future(cancel_event.wait())
        if cancel_event is not None
        else None
    )
    try:
        if waiter is None:
            # No event to race, but still inside the try: an *outer* deadline
            # cancelling this await abandons the thread just the same, and that
            # has to be noticed and counted like any other abandonment.
            return await future
        done, _pending = await asyncio.wait(
            [future, waiter], return_when=asyncio.FIRST_COMPLETED,
        )
        if future in done:
            return future.result()
        raise ReadCancelled("Read cancelled")
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        if not future.done():
            # Abandons the thread, not the syscall: nothing can stop the latter,
            # and the thread dies with the process. Counted, because for a
            # verify that spawns no child this is the only trace left when an
            # outer deadline cancels it.
            future.cancel()
            _note_abandoned("detached read")
        elif future.cancelled():
            # Cancelling the awaiting task propagates straight into the future
            # it is awaiting, so it is already "done" here -- cancelled, not
            # answered. The thread is every bit as stuck; count it the same.
            _note_abandoned("detached read")


async def run_detached_undoable(
    func: Callable,
    *args: object,
    timeout: float,
    undo: Callable[[object], object],
    **kwargs: object,
) -> object:
    """:func:`run_detached` under *timeout*, for a call that takes something
    its caller must give back (a lock).

    A caller that stops waiting (the timeout, a cancel) abandons the call on
    its thread, as ``run_detached`` does; whatever the call returns after that
    is handed to *undo*, so nothing an abandoned call took stays held. *undo*
    runs on that thread when the answer comes after the caller gave up, or on
    the caller's side when the answer came but was lost to the timeout. Raises
    :class:`DeadlineExpired` on the timeout; an exception from *func*
    propagates as itself, its own ``TimeoutError`` included (it took nothing,
    or let go of it itself). Told apart by who finished, not by type.
    """
    guard = threading.Lock()
    state: dict = {"abandoned": False, "delivered": False, "result": None, "error": None}

    def call() -> object:
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            with guard:
                state["error"] = exc
            raise
        with guard:
            if not state["abandoned"]:
                state["delivered"] = True
                state["result"] = result
                return result
        undo(result)  # nobody is waiting for it any more
        return result

    try:
        return await asyncio.wait_for(run_detached(call), timeout)
    except BaseException as exc:
        with guard:
            state["abandoned"] = True
            delivered, result, error = state["delivered"], state["result"], state["error"]
        if isinstance(exc, ProbeCapacityExceeded):
            raise  # it never started: nothing taken
        if error is not None:
            # It took nothing, or let go of it itself. Its own failure is that
            # failure, also when it landed just as the wait ran out; a caller's
            # cancel still propagates as the cancel.
            if error is exc or not isinstance(exc, asyncio.TimeoutError):
                raise
            raise error from None
        if delivered and isinstance(exc, asyncio.TimeoutError):
            return result  # answered just as the wait ran out: the caller's now
        if delivered:
            # Answered, but the answer never reached the caller. Undone on a
            # thread of its own: *undo* may do I/O, never on the event loop.
            threading.Thread(target=undo, args=(result,), daemon=True).start()
        if isinstance(exc, asyncio.TimeoutError):
            raise DeadlineExpired(f"no answer in {timeout:g}s") from exc
        raise


class MutationNotStarted(RuntimeError):
    """An irreversible mutation never started (no detached thread could be
    launched: :class:`ProbeCapacityExceeded`, or the caller gave up first).
    Nothing changed, and the caller still holds its locks."""


class MutationInFlight(RuntimeError):
    """An irreversible mutation was still running when its caller stopped
    waiting (:func:`run_detached_mutation`). Whether it took effect is
    unknown; the locks it runs under went with it and are released by its
    worker once it has finished."""


async def run_detached_mutation(
    func: Callable,
    *args: object,
    timeout: float | None,
    hand_off: Callable[[], Callable[[], object]],
    **kwargs: object,
) -> object:
    """Run an irreversible mutation (an unlink) on a detached thread, never
    abandoning it with the locks that make it safe.

    A read-only probe may be abandoned (:func:`run_detached`,
    :func:`run_detached_undoable`): nothing it does outlives its caller's
    interest. A mutation may not: if its caller let go of its locks while it
    still ran, it could take effect under another job's newly granted lock.
    So when the caller stops waiting (*timeout*, None for no bound, or a
    cancel) while *func* still runs, *hand_off()* is called once, on the
    caller's side: it takes the locks from the caller and returns the
    callable that releases them, which the worker calls after *func* has
    returned or raised. The caller must not release them itself after that.

    Raises :class:`MutationInFlight` on the timeout (also when *func* finished
    just as the wait ran out, its answer lost: the outcome is unknown either
    way, but then the locks stay the caller's). A cancel propagates as usual,
    after the hand-off. An exception from *func* within the wait propagates.

    Only a worker that has started can own the locks (codex 4579801887): when
    none could be launched (:class:`ProbeCapacityExceeded`), or the caller
    gave up before it began, nothing is handed off, *func* never runs, the
    caller keeps its locks, and :class:`MutationNotStarted` says so.
    """
    guard = threading.Lock()
    state: dict = {
        "started": False, "settled": False, "gave_up": False, "release": None,
        "error": None, "result": None,
    }

    def call() -> object:
        with guard:
            if state["gave_up"]:
                # The caller stopped waiting before this began: it still holds
                # the locks, so the mutation must not run without them.
                raise MutationNotStarted("the caller stopped waiting before it began")
            state["started"] = True
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            with guard:
                state["error"] = exc
            raise
        else:
            with guard:
                state["result"] = result
            return result
        finally:
            with guard:
                state["settled"] = True
                release = state["release"]
            if release is not None:
                release()  # the caller stopped waiting: these are ours to let go

    try:
        return await asyncio.wait_for(run_detached(call), timeout)
    except BaseException as exc:
        with guard:
            started, settled = state["started"], state["settled"]
            error, result = state["error"], state["result"]
            if not started:
                state["gave_up"] = True
            elif not settled:
                state["release"] = hand_off()
        if settled:
            # The mutation finished; the locks are still the caller's. Its own
            # failure (an ETIMEDOUT from a network filesystem included) is
            # that failure, not the wait running out (codex 4579904147).
            if error is exc or not isinstance(exc, asyncio.TimeoutError):
                raise  # its own failure as raised, or the caller's cancel
            if error is not None:
                # The wait ran out just as it failed: the timeout did not
                # cause the failure, so it is not chained on.
                raise error from None
            if isinstance(exc, asyncio.TimeoutError):
                return result  # answered just as the wait ran out
            raise
        if not started:
            if isinstance(exc, (asyncio.CancelledError, MutationNotStarted)):
                raise
            raise MutationNotStarted(
                f"{getattr(func, '__name__', 'the change')} could not start: {exc}",
            ) from exc
        if isinstance(exc, asyncio.TimeoutError):
            raise MutationInFlight(
                f"{getattr(func, '__name__', 'the change')} was still running "
                f"after {timeout:g}s",
            ) from exc
        raise


def conversion_stall_bound() -> int:
    """Seconds a conversion may go without progress before it is given up on.

    ``CHD_PROGRESS_TIMEOUT``, floored at :data:`_MIN_STALL_TIMEOUT`; ``0`` means
    no bound. The one reading of that rule for work that runs outside
    ``SubprocessRunner.run`` (detached reads, an in-process repack), so it
    cannot drift from the subprocess phase's.
    """
    bound = max(0, int(getattr(settings, "progress_timeout", 0) or 0))
    return max(bound, _MIN_STALL_TIMEOUT) if bound > 0 else 0


def open_input_at(path: str, offset: int = 0):
    """A buffered read handle on ``path``, positioned at ``offset``.

    For a reader that outlives any one block: the stdin feeder reads it chunk
    by chunk on detached threads and must be able to abandon it with a stuck
    read, so no ``with`` block can own it. The caller closes it.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    if offset:
        handle.seek(offset)
    return handle


async def feed_stdin_from_file(
    process,
    path: str,
    *,
    offset: int = 0,
    cancel_event: asyncio.Event | None = None,
    chunk_size: int = 1 << 20,
) -> None:
    """Copy ``path`` from ``offset`` into ``process.stdin``, then close stdin.

    The shared feeder for a child that reads a file on stdin (``zstd -t`` in
    z3ds verify, ``zstd -dc`` in the Round-trip). Reads go through
    :func:`run_detached`, so a read on a mount that stopped answering never
    blocks the event loop. Such a read can only be abandoned, never closed:
    closing the handle waits on the lock the stuck read holds, which would turn
    the cleanup into another unbounded wait holding a probe slot. So the handle
    is closed only when no read is in flight; when this is cancelled or
    ``cancel_event`` fires during a read, it is abandoned with its thread.
    A child that stops reading (broken pipe) ends the feed quietly; its exit
    code says why.
    """
    handle = await run_detached(
        open_input_at, path, offset, cancel_event=cancel_event,
    )
    abandon = False
    try:
        while not (cancel_event is not None and cancel_event.is_set()):
            abandon = True  # a read is in flight from here
            try:
                chunk = await run_detached(
                    handle.read, chunk_size, cancel_event=cancel_event,
                )
            except ReadCancelled:
                break
            abandon = False
            if not chunk:
                break
            try:
                process.stdin.write(chunk)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                break
    except (asyncio.CancelledError, GeneratorExit):
        abandon = True
        raise
    finally:
        with contextlib.suppress(Exception):
            process.stdin.close()
        if not abandon:
            with contextlib.suppress(Exception):
                await run_detached(handle.close)


class DeadlineExpired(asyncio.TimeoutError):
    """A bounded detached call's wait ran out while the call was still running.

    Never raised for the call's own ``TimeoutError`` (a network filesystem's
    ``ETIMEDOUT``): ``asyncio.TimeoutError`` is the built-in ``TimeoutError``,
    so the bounded helpers tell the two apart by who finished (``_Finished``),
    not by type, and re-raise the call's own error as itself (codex 4579904147).
    """


class _Finished:
    """Who finished: whether a detached call settled, and how. Recorded by the
    call itself, on its thread, before it returns or raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.settled = False
        self.error: BaseException | None = None
        self.result: object = None

    def wrap(self, func: Callable, args: tuple, kwargs: dict) -> Callable[[], object]:
        def call() -> object:
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                with self._lock:
                    self.settled, self.error = True, exc
                raise
            with self._lock:
                self.settled, self.result = True, result
            return result
        return call

    def snapshot(self) -> tuple[bool, BaseException | None, object]:
        with self._lock:
            return self.settled, self.error, self.result


async def run_detached_bounded(
    func: Callable,
    *args: object,
    timeout: float,
    cancel_event: asyncio.Event | None = None,
    **kwargs: object,
) -> object:
    """:func:`run_detached` under *timeout*, for a read-only probe.

    Raises :class:`DeadlineExpired` only when the wait ran out while the call
    still ran. The call's own exceptions propagate as themselves, its own
    ``TimeoutError`` included; an answer that came just as the wait ran out
    is returned; :class:`ProbeCapacityExceeded` (no thread could start)
    propagates unchanged.
    """
    finished = _Finished()
    try:
        return await asyncio.wait_for(
            run_detached(finished.wrap(func, args, kwargs), cancel_event=cancel_event),
            timeout,
        )
    except asyncio.TimeoutError as exc:
        settled, error, result = finished.snapshot()
        if isinstance(exc, ProbeCapacityExceeded) or error is exc:
            raise
        if error is not None:
            # It failed just as the wait ran out: that failure, not a timeout.
            raise error from None
        if settled:
            return result
        raise DeadlineExpired(f"no answer in {timeout:g}s") from exc



def run_detached_blocking(
    func: Callable, *args: object, timeout: float, **kwargs: object,
) -> object:
    """:func:`run_detached_bounded` for blocking code already off the event
    loop (a worker thread, a Library walk): *func* on a throwaway daemon
    thread, under the same process-wide ceiling every detached probe shares
    (``_MAX_DETACHED_PROBES``), so a mount that hangs reads can't pile up
    threads past it.

    Raises :class:`ProbeCapacityExceeded` when no slot is free (nothing is
    started) and :class:`DeadlineExpired` when *timeout* runs out while the
    call still runs (it is abandoned; its slot frees when it returns). The
    call's own exceptions propagate as themselves.
    """
    if not _probe_slots.acquire(blocking=False):
        raise ProbeCapacityExceeded(
            f"{_MAX_DETACHED_PROBES} filesystem probes are already blocked; "
            "the volume is not responding",
        )
    box: dict = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            box["value"] = func(*args, **kwargs)
        except Exception as exc:
            # Relayed to the caller, who raises it as itself.
            _probe_logger.debug("Detached call failed", exc_info=True)
            box["error"] = exc
        finally:
            _probe_slots.release()
            done.set()

    try:
        threading.Thread(target=_worker, daemon=True, name="fs-probe").start()
    except RuntimeError as exc:
        _probe_slots.release()
        raise ProbeCapacityExceeded(f"no filesystem probe thread could start: {exc}") from exc
    if not done.wait(timeout):
        raise DeadlineExpired(f"no answer in {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]

class Stalled(RuntimeError):
    """A detached call made no progress for its stall bound.

    ``where`` is the last place it reported (the folder a walk was listing),
    so the message can name what stopped answering.
    """

    def __init__(self, what: str, where: str | None, seconds: float) -> None:
        self.what, self.where, self.seconds = what, where, seconds
        at = f" at {where}" if where else ""
        super().__init__(f"{what} stopped responding{at} (no answer in {seconds:g}s)")


class ProgressAbandoned(RuntimeError):
    """Raised inside a detached call by :class:`Progress` once its awaiter has
    given up (a cancel, the stall bound): the call stops at its next step
    instead of walking on for a result nobody reads."""


class Progress:
    """What a call run by :func:`run_detached_within_stall_bound` reports as it moves.

    Call it after every step (each entry a walk reads): ``progress()`` notes
    the time, ``progress(where)`` also notes where it is. Thread-safe. Raises
    :class:`ProgressAbandoned` once the awaiter has given up.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = time.monotonic()
        self._where: str | None = None
        self._abandoned = False

    def __call__(self, where: str | None = None) -> None:
        with self._lock:
            if self._abandoned:
                raise ProgressAbandoned("the caller stopped waiting")
            self._at = time.monotonic()
            if where is not None:
                self._where = where

    def snapshot(self) -> tuple[float, str | None]:
        with self._lock:
            return self._at, self._where

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True


async def run_detached_within_stall_bound(
    func: Callable,
    *args: object,
    what: str,
    cancel_event: asyncio.Event | None = None,
    stall: float | None = None,
    report_progress: bool = False,
    **kwargs: object,
) -> object:
    """:func:`run_detached`, given up on after a stretch with no progress.

    The one stall-bounded detached seam. Two kinds of caller:

    * A conversion's blocking reads *before* its subprocess starts (a header
      probe, a filesystem walk, workspace setup). ``SubprocessRunner.run``'s
      stall watchdog only starts with the child, and the job has no outer
      deadline, so without this a source mount that stops answering during
      those reads holds the job, and the single-slot queue behind it,
      indefinitely. They report no progress, so the bound is simply how long
      the call may take.
    * A walk over a tree that may be large and healthy, or frozen
      (``report_progress=True``, #361): ``func`` is called with a
      ``progress=<Progress>`` keyword and reports every step
      (``utils.path_utils.walk_tree`` does it). The clock restarts on each
      report, so a big Library that keeps moving is never cut off, and a
      listing blocked on a frozen mount ends at the bound.

    The bound is ``stall`` seconds, or by default ``CHD_PROGRESS_TIMEOUT``,
    the conversion's own "no progress for this long" limit, floored like it;
    ``0`` (or less) disables it. On expiry the thread is abandoned (as
    ``run_detached`` does for a cancel; counted the same way) and
    :class:`Stalled`, a ``RuntimeError`` naming ``what`` and the last place
    reported, is raised. ``ReadCancelled`` still propagates for a cancel. A
    call that finished just as the bound ran out is answered, or raises its
    own error, and an instant ``TimeoutError`` (``ProbeCapacityExceeded``, an
    OS ``ETIMEDOUT``) keeps its own type and message (codex 4579904147).
    Whatever ends the wait, the call is told to stop at its next report.
    """
    bound = conversion_stall_bound() if stall is None else stall
    progress = Progress()
    finished = _Finished()
    call_kwargs = {**kwargs, "progress": progress} if report_progress else kwargs
    task = asyncio.ensure_future(
        run_detached(finished.wrap(func, args, call_kwargs), cancel_event=cancel_event),
    )
    try:
        if bound <= 0:
            return await task
        while True:
            at, where = progress.snapshot()
            remaining = at + bound - time.monotonic()
            if remaining > 0:
                done, _pending = await asyncio.wait({task}, timeout=remaining)
                if task in done:
                    return task.result()
                continue
            settled, error, result = finished.snapshot()
            if error is not None:
                raise error  # it failed just as the bound ran out
            if settled:
                return result
            raise Stalled(what, where, bound)
    finally:
        # Tell the call to stop at its next step, whatever ended the wait (a
        # finished call never reads the flag again).
        progress.abandon()
        if not task.done():
            task.cancel()  # run_detached notes the thread as abandoned
            with contextlib.suppress(asyncio.CancelledError, ReadCancelled):
                await task


def run_blocking_within_stall_bound(
    func: Callable, *args: object, what: str, stall: float, **kwargs: object,
) -> object:
    """:func:`run_detached_within_stall_bound` (``report_progress=True``) for
    blocking code already off the event loop (a job's worker thread).

    *func* runs on a throwaway daemon thread with a ``progress=<Progress>``
    keyword it reports every step to; the clock restarts on each report. After
    *stall* seconds with no report the thread is abandoned (told to stop at its
    next report) and :class:`Stalled` is raised. A call that finished just as
    the bound ran out is answered. Its own exceptions propagate as themselves.
    """
    progress = Progress()
    box: dict = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            box["value"] = func(*args, progress=progress, **kwargs)
        except Exception as exc:
            # Relayed to the caller, who raises it as itself.
            _probe_logger.debug("Stall-bounded call failed", exc_info=True)
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True, name="fs-walk").start()
    try:
        while not done.is_set():
            at, where = progress.snapshot()
            remaining = at + stall - time.monotonic()
            if remaining > 0:
                done.wait(remaining)
            elif not done.is_set():
                raise Stalled(what, where, stall)
    finally:
        progress.abandon()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _open_for_verify(path: str) -> int:
    """Size *path*, then open it for reading as its verifier will. Blocking:
    run detached (:func:`_probe_for_verify`). ``O_NONBLOCK`` changes nothing
    for a regular file, but keeps a FIFO with no writer from holding the
    probe's thread for good."""
    size = os.path.getsize(path)
    os.close(os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)))
    return size


async def _probe_for_verify(
    path: str, cancel_event: asyncio.Event | None, *, missing: dict | None,
) -> tuple[dict | None, int]:
    """The bounded "can the verifier open it" probe (#359): ``(None, size)``
    when *path* opens for reading, else the event that says why not.

    Opening is decided here, before any verifier runs, rather than by reading
    each tool's error text: a file that exists but can't be opened (EACCES,
    EPERM, EIO) is no verdict on the file (``inconclusive``), so a stored check
    stays. Bounded and detached, and raced against *cancel_event*, like every
    probe of storage that may have stopped answering: an open that stalls is
    abandoned, as before. A missing file is not "could not read": it is gone,
    and each caller keeps the answer it always gave (*missing*: the event to
    return, or None to let the verifier run and judge it).
    """
    try:
        size = await asyncio.wait_for(
            run_detached(_open_for_verify, path, cancel_event=cancel_event),
            timeout=_STAT_TIMEOUT,
        )
    except ReadCancelled:
        return {
            "type": "error",
            "valid": False,
            "cancelled": True,
            "message": "Verification cancelled",
        }, 0
    except asyncio.TimeoutError:
        return {
            "type": "error",
            "valid": False,
            # The probe is still running on its own thread -- giving up on it
            # is abandonment, and a caller walking a list has to stop rather
            # than spend one more thread per remaining path on the same storage.
            "abandoned": True,
            # It ran out of time: nothing was judged, so no verdict, marked
            # here for every consumer (#374).
            "inconclusive": True,
            "message": (
                f"File stopped responding (no answer in {_STAT_TIMEOUT:.0f}s); "
                "the volume may be offline"
            ),
        }, 0
    except FileNotFoundError:
        return missing, 0
    except OSError as e:
        return {
            "type": "error", "valid": False, "inconclusive": True,
            "message": (
                f"Could not open {os.path.basename(path)} to check it: "
                f"[Errno {e.errno}] {e.strerror or e}"
            ),
        }, 0
    return None, size


async def verify_readable(
    path: str, *, cancel_event: asyncio.Event | None = None,
) -> dict | None:
    """The shared verify entry's open probe (:func:`_probe_for_verify`): the
    event to yield and return on, or None when the verifier can open *path*.
    Every verify goes through it, the Python checks via
    :func:`verify_preflight` and the subprocess ones via
    ``SubprocessRunner.run_verify`` / ``capture_verify``, so a verifier's
    non-zero exit after a successful open is always its verdict. A missing
    file passes through to the verifier, whose answer it always was."""
    problem, _size = await _probe_for_verify(path, cancel_event, missing=None)
    return problem


async def verify_preflight(
    path: str,
    extensions: frozenset[str] | set[str],
    *,
    cancel_event: asyncio.Event | None = None,
) -> tuple[dict | None, int]:
    """The missing / empty / wrong-extension gate every ``verify_stream`` opens with.

    Returns ``(error_event_or_None, size_in_bytes)``: an event to yield and
    return on, or ``None`` plus the size when the file is worth verifying.

    Five services opened with the identical three checks, so they are written
    once here — and, more to the point, the stat is **bounded**. These checks run
    on the event loop before the verify's first real await, and ``getsize`` on an
    unresponsive mount blocks in uninterruptible I/O: inline, that freezes every
    task in the process, including the ``asyncio.wait_for`` that is supposed to
    bound this very verify (the issue #263 failure mode, reached through the
    verify path). Off the loop, an unresponsive mount fails this one verify.

    ``cancel_event`` is raced against the stat as well: on a volume that has
    stopped answering, a cancel pressed during the probe must produce a
    *cancelled* verdict, not the "stopped responding" failure the bound would
    otherwise report ten seconds later.

    The size comes from opening the file for reading (:func:`_probe_for_verify`,
    #359): one the verifier could not open is no verdict.
    """
    # A missing file stays what it always was here: no verdict.
    problem, size = await _probe_for_verify(path, cancel_event, missing={
        "type": "error", "valid": False, "message": "File not found",
        "inconclusive": True,
    })
    if problem is not None:
        return problem, 0
    if size == 0:
        return {"type": "error", "valid": False, "message": "File is empty"}, 0
    ext = os.path.splitext(path)[1].lower()
    if ext not in extensions:
        return {
            "type": "error", "valid": False, "message": f"Invalid extension: {ext}",
        }, size
    return None, size


BATCH_STOPPED_MESSAGE = (
    "Batch stopped: a verifier could not be stopped and is still holding the storage."
)


def killed_verify_error(returncode: int) -> dict:
    """The verify event for a verifier ended by a signal (#306).

    A negative return code is a signal (SIGKILL, the OOM killer), not the
    verifier's judgement of the file: no verdict, so a stored pass is kept.
    """
    return SubprocessRunner._verify_error(
        f"The verifier was killed by signal {-returncode} before it finished",
        inconclusive=True,
    )


def verify_exit_event(returncode: int | None, failure_message: str) -> dict:
    """The terminal event for a verifier that did not exit 0 (#306).

    The one place an exit status becomes a result, for the shared runner and
    for any service that drives its own subprocess (z3ds's zstd): a signal
    (negative return code) is ``killed_verify_error``, no verdict; any other
    non-zero exit is the verifier's verdict, ``failure_message``.
    """
    if returncode is not None and returncode < 0:
        return killed_verify_error(returncode)
    return SubprocessRunner._verify_error(failure_message)


def stops_verify_batch(result: dict, abandoned: Sequence[str] = ()) -> bool:
    """Whether a verify must end a walk over a list of files.

    True when the verifier outlived SIGKILL (the result's ``abandoned``), or
    when anything the check started was written off on the way, as reported
    to its ``collect_abandonment()`` sink, passed as ``abandoned``: a deadline
    that cancelled a check stuck on dead storage leaves the detached read
    behind without saying so in any result. Either way something is still
    reading the storage the list lives on, so opening the next file would
    strand one more. The web UI's batch verify and the CLI's ``verify`` both
    stop on it, with ``BATCH_STOPPED_MESSAGE``.
    """
    return bool(result.get("abandoned")) or bool(abandoned)


def verify_exception_event(exc: BaseException, *, prefix: str = "Verification error") -> dict:
    """The verify event for an exception a check raised, for every tool (#306).

    ``inconclusive`` is an allowlist. Only "the check could not run" counts:
    the tool, a library or a prerequisite is missing (``ToolUnavailable``), the
    verifier could not be launched or the input could not be read (any
    ``OSError``), or a timeout. Everything else is a verdict on the file: a
    header or format error (``ValueError``, ``struct.error``, a bad archive),
    and a broken pipe or reset from a decoder that quit early on bad data,
    which is an ``OSError`` but says the data was rejected.
    """
    message = f"{prefix}: {exc}" if str(exc) else f"{prefix}: {type(exc).__name__}"
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return SubprocessRunner._verify_error(message)
    if isinstance(exc, (ToolUnavailable, OSError, asyncio.TimeoutError)):
        return SubprocessRunner._verify_error(message, inconclusive=True)
    return SubprocessRunner._verify_error(message)


def abandoned_verify_error(pid: int) -> dict:
    """The one terminal event for a verifier that outlived ``SIGKILL``.

    Written once because every verify path can reach it -- the streaming loop's
    cancel/timeout/stall ladder, its post-EOF grace, the captured one-shot
    verifiers through ``run_capture``'s ``on_abandoned`` hook, and z3ds's
    hand-rolled zstd loop -- and because it has to read the same wherever it
    comes from. The ``abandoned`` flag is the part that matters: it outranks
    "cancelled" and "timed out", because the child is still running and still
    holding the storage, and a caller working through a list has to stop rather
    than open the next file against it. Issue #268 considered replacing this
    flag with an exception and rejected it: the sink is the mechanism, and a
    deciding helper raises ``StorageAbandoned`` only where the value it would
    otherwise return cannot express the difference.
    """
    return {
        "type": "error",
        "valid": False,
        "abandoned": True,
        # Nothing was judged: no verdict, marked at the source (#374).
        "inconclusive": True,
        "message": (
            f"Verification did not exit and could not be killed (pid {pid}); "
            "it is likely blocked on unresponsive storage."
        ),
    }


async def collect_verify(
    stream: AsyncGenerator[dict, None], *, fallback_message: str,
) -> dict:
    """Reduce a ``verify_stream`` to the one-shot ``verify()`` result.

    Every tool's ``verify()`` is this same drain-and-keep-the-terminal-event
    wrapper, so it is written once here rather than eight times. The result is
    ``{"valid", "message"}`` plus ``"cancelled": True`` when the stream ended
    because the operator cancelled -- a distinction callers must keep, since a
    cancelled verify proved nothing and must not be recorded as a failure --
    and ``"abandoned": True`` when it ended with a child that outlived SIGKILL,
    which tells a caller working through a list to stop.
    """
    final: dict = {"valid": False, "message": fallback_message}
    async for update in stream:
        if update.get("type") in ("complete", "error"):
            final = update
    result = {
        "valid": bool(final.get("valid", False)),
        "message": final.get("message") or fallback_message,
    }
    if final.get("cancelled"):
        result["cancelled"] = True
    if final.get("abandoned"):
        result["abandoned"] = True
    if final.get("inconclusive"):
        result["inconclusive"] = True
    if final.get("uncheckable"):
        result["uncheckable"] = True
    return result


def _split_stream_lines(buffer: str) -> tuple[list[str], str]:
    """Segment accumulated subprocess output into complete lines + a remainder.

    Segmentation is a pure function of the byte stream, independent of where
    ``read()`` chunk boundaries fall: CRLF and bare CR (progress redraws) are
    normalized to LF, then the buffer is split on LF. The text after the final
    LF is returned as the remainder to prepend to the next chunk. Lines are
    stripped and blanks dropped, matching the prior per-separator loop's output
    minus its chunk-boundary sensitivity (issue #183, site 5).
    """
    normalized = buffer.replace("\r\n", "\n").replace("\r", "\n")
    segments = normalized.split("\n")
    remainder = segments[-1]
    return [s.strip() for s in segments[:-1] if s.strip()], remainder


# Expected output size as a multiple of the input, per conversion mode. The
# single source of truth for the size-growth progress estimate: one table here
# rather than a `_COMPRESS_RATIO`/`_DECOMPRESS_RATIO` pair and an
# `expected_size` closure re-derived inside every tool service. A mode absent
# from the table still reports status -- it falls back to the bytes-written
# message, which needs no ratio -- so a new tool is never *required* to add a
# row, and adding one only sharpens its percentage estimate. The ratio only
# smooths the bar (see `output_size_progress`), so an approximate value is fine.
SIZE_RATIOS: dict[str, float] = {
    # chdman: CD/DVD image -> CHD, and back. chdman prints its own percentage,
    # so these rows never drive the progress bar (native parsing wins); they
    # exist so a *savings estimate* has a figure for the most common conversion
    # in the app. Approximate by nature -- a data-heavy DVD compresses far less
    # than a mostly-audio CD.
    "createcd": 0.6, "createdvd": 0.65, "createraw": 0.7, "createhd": 0.7,
    "createld": 0.7,
    "extractcd": 1.7, "extractdvd": 1.5, "extractraw": 1.4,
    "extracthd": 1.4, "extractld": 1.4,
    # dolphin-tool: RVZ/WIA/GCZ compression, and decompression back to ISO.
    "dolphin_rvz": 0.5, "dolphin_wia": 0.5, "dolphin_gcz": 0.7,
    "dolphin_iso": 2.0,
    # maxcso: PSP/PS2 CSO family.
    "cso_compress": 0.5, "cso2_compress": 0.5, "zso_compress": 0.5,
    "dax_compress": 0.5, "cso_decompress": 2.0,
    # nsz: Switch NSP/XCI <-> NSZ/XCZ.
    "nsz_compress": 0.6, "nsz_decompress": 1.7,
    # z3ds: 3DS CIA/3DS <-> Z3DS.
    "z3ds_compress": 0.5, "z3ds_decompress": 2.0,
    # extract-xiso: full Xbox / 360 disc image -> XISO. Prints its own per-file
    # progress, so this only feeds savings estimates; real trims keep 60-80%.
    "xiso_rewrite": 0.75,
    # rom-converto: Xbox 360 disc image -> ZAR. Only the game's files, each
    # zstd-compressed, so the disc padding goes too. Prints no progress on a
    # pipe, so this row is what gives its bar a percentage.
    "zar_compress": 0.5,
}


def size_ratio_for(mode: str | None) -> float | None:
    """Expected output/input size ratio for ``mode`` (None when unknown)."""
    return SIZE_RATIOS.get(mode) if mode else None


def output_size_message(size: int, delta_bytes: int, delta_seconds: float) -> str:
    """Status line for a size-growth tick: bytes written and the current rate.

    The shared human-readable half of the size-growth progress signal (the
    numeric half is :func:`output_size_progress`). A tool that is merely *slow*
    -- a conversion crawling against a saturated array or a stalled share --
    must read as slow rather than as indistinguishable from a hang, so the
    message carries an absolute (MB written) and a derivative (MB/min): the
    first keeps climbing, the second collapses toward zero.

    The rate is measured **between consecutive samples**, not as total bytes
    over total elapsed time. A cumulative average is dominated by whatever the
    conversion did first: a job that writes gigabytes quickly and then crawls
    would keep advertising hundreds of MB/min long after it slowed to nothing,
    destroying the one distinction this line exists to make. Rate is per minute
    because the case worth diagnosing is the pathological one, where a
    per-second figure rounds to ``0.0`` and says nothing.
    """
    written_mb = size / (1024 * 1024)
    per_min = (delta_bytes / (1024 * 1024)) / (max(delta_seconds, 1e-9) / 60.0)
    return f"Working... ({written_mb:,.0f} MB written, {per_min:,.1f} MB/min)"


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


def files_in_folder(folder: str) -> Callable[[], list[str]]:
    """An ``output_growth_paths`` probe over every file directly in *folder*.

    For a tool that writes its product under a name of its own choosing and
    renames it into place only when it is done (rom-converto writes
    ``.Game.zar.<random>.tmp``). The output path does not exist until the very
    end, so the default probe sees no growth for the whole run, the stall
    watchdog kills a healthy job, and the bar never moves. Run such a tool into
    a private work folder and pass this: the summed size of whatever is in the
    folder is the growth signal, whatever the tool calls its files, and the
    size-growth fallback reports bytes and rate from it as usual.

    Regular files only, not followed into subfolders. A folder that does not
    exist (yet) is no files. Called from the runner's probe thread, never on
    the event loop.
    """

    def paths() -> list[str]:
        try:
            with os.scandir(folder) as entries:
                return [
                    entry.path for entry in entries
                    if entry.is_file(follow_symlinks=False)
                ]
        except OSError:
            return []

    return paths


class SubprocessRunner:
    """Spawns a conversion subprocess and streams progress updates.

    One instance per tool owns that tool's in-flight PID set, so both
    ``run()`` and the tool's separate ``verify_stream`` loop can register
    their subprocesses through the same store (``track_pid``/``untrack_pid``).
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._active_pids: set[int] = set()
        self._abandoned_pids: set[int] = set()
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

    def abandoned_pids(self) -> list[int]:
        """Children of this runner that outlived ``SIGKILL`` and are still alive.

        Recorded in :meth:`reap`, the one place that discovers the fact, so it
        survives paths that cannot report it in their own return value -- above
        all a verify generator cancelled by an *outer* deadline, which unwinds
        through its ``finally`` with no event left to carry the flag. A caller
        working through a list (the batch verify route) asks here before opening
        the next file, since a wedged mount would otherwise cost it one
        unkillable process per file.

        Entries are dropped once the pid is gone: the kernel does eventually
        unblock most of these, and a permanent entry would stop every later
        batch. A pid the OS has since reused reads as still-abandoned, which
        errs toward stopping early -- the safe direction here.
        """
        with self._pid_lock:
            candidates = list(self._abandoned_pids)
        gone = []
        for pid in candidates:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone.append(pid)
            except PermissionError:  # pragma: no cover - not ours any more
                gone.append(pid)
        if gone:
            with self._pid_lock:
                self._abandoned_pids.difference_update(gone)
        return [pid for pid in candidates if pid not in gone]

    async def reap(self, process, *, exit_timeout: float = _EXIT_GRACE) -> bool:
        """Wait for ``process`` to exit, bounded at every step. True if reaped.

        The shared teardown for every subprocess this module spawns. A child
        blocked in uninterruptible I/O (``D`` state -- a stalled mount, a drive
        that stopped answering) does not die on ``SIGTERM`` *or* ``SIGKILL``:
        the signal is only delivered once the kernel I/O returns, which may be
        never. A bare ``await process.wait()`` on such a child therefore blocks
        its job forever, and because ``MAX_CONCURRENT_JOBS=1`` runs jobs inline
        in the dispatcher, every job queued behind it too (issue #263).

        So each step is bounded and the ladder terminates: wait ``exit_timeout``
        for a voluntary exit (skipped when 0 -- teardown paths have already
        stopped reading), then ``SIGTERM`` + grace, then ``SIGKILL`` + grace.
        Returning ``False`` means the child outlived ``SIGKILL`` and has been
        abandoned -- it keeps its pipes and one OS process until the kernel
        unblocks it, which is the unavoidable cost of not hanging the queue.
        Callers turn that into a failed job.
        """
        if process.returncode is not None:
            return True

        async def _wait(timeout: float) -> bool:
            if timeout <= 0:
                return process.returncode is not None
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return False
            return True

        if await _wait(exit_timeout):
            return True

        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        if await _wait(_TERM_GRACE):
            return True

        with contextlib.suppress(ProcessLookupError):
            process.kill()
        if await _wait(_KILL_GRACE):
            return True

        self._logger.error(
            "%s pid=%s survived SIGKILL (likely blocked in uninterruptible I/O); "
            "abandoning it so the job fails instead of stalling the queue",
            self._owner, process.pid,
        )
        with self._pid_lock:
            self._abandoned_pids.add(process.pid)
        _note_abandoned(f"pid {process.pid}")
        return False

    @staticmethod
    async def _spawn_piped(
        cmd: list[str],
        *,
        stdin=None,
        stderr=asyncio.subprocess.PIPE,
        env: Mapping[str, str] | None = None,
        wrapped: bool = False,
    ):
        """Spawn ``cmd`` (a list argv, never a shell) with stdout on a pipe.

        The one spawn site :meth:`run_capture` and :meth:`hash_stdout` share,
        so the argv-from-the-tool's-config spawn exists once in this module.
        """
        try:
            return await asyncio.create_subprocess_exec(  # nosemgrep
                cmd[0], *cmd[1:],
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=env,
            )
        except OSError:
            if wrapped:
                _forget_wrappers()
            raise

    async def run_capture(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        cancel_event: asyncio.Event | None = None,
        stderr_to_stdout: bool = False,
        env: Mapping[str, str] | None = None,
        on_abandoned: Callable[[int], None] | None = None,
        verdict: bool = False,
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

        ``cmd`` is wrapped with :func:`priority_prefix` here, so a caller passes
        the bare argv. ``env`` mirrors :meth:`run`: it forwards a private
        environment (nsz runs with its own keys home).

        ``on_abandoned`` is called with the pid when the TERM/KILL ladder gives
        up -- the child outlived ``SIGKILL`` and is still holding whatever it
        was reading. Without it that fact is invisible here: an abandoned child
        and a clean cancellation both come back as a ``None`` returncode, and a
        caller working through a list would open the next file against the same
        storage. Retained for a caller that must name the pid in its own
        terminal event (``capture_verify``); every other caller reads the same
        fact from an open ``collect_abandonment()`` sink, which ``reap`` always
        fills. Raising from ``run_capture`` itself was tried for issue #268 and
        rejected: it cannot be raised from the ``finally`` an outer deadline
        unwinds through without masking what already propagates.
        """
        # Don't spawn into an already-cancelled operation. The race is real
        # rather than theoretical: the child is created *before* the
        # cancel_event becomes a waiter below, so a caller that resolved a
        # size-scaled bound (itself a probe that can take its full time on
        # storage that stopped answering) and only then reached here would start
        # a verifier nobody wants -- and on that storage it is exactly the child
        # that blocks immediately and may outlive SIGKILL. Checked in the shared
        # seam so every capture caller gets it, not just the ones that remember.
        # Reported as the ordinary abort, which is what a cancel is.
        # Honour the shared process-priority policy, same as the streaming
        # run(): a captured command (e.g. dolphin-tool verify reconstructing a
        # full disc for DAT hashing) is just as heavy as a conversion. Command
        # wrappers only, never a preexec_fn (see priority_prefix). Resolved
        # before the cancel check, which then also covers its (bounded) wait.
        # *verdict* (capture_verify): its exit code is a check's verdict, so
        # it is spawned bare and reniced after (_prioritize_child).
        cmd, wrapped = await _wrapped_command(self._owner, cmd, wrap=not verdict)
        if cancel_event is not None and cancel_event.is_set():
            return None, b"", b""

        process = await self._spawn_piped(
            cmd,
            # Never the caller's terminal: a tool that asks for input (7z
            # wanting a password) fails at once instead of waiting (#306).
            stdin=asyncio.subprocess.DEVNULL,
            stderr=(
                asyncio.subprocess.STDOUT
                if stderr_to_stdout
                else asyncio.subprocess.PIPE
            ),
            env=env,
            wrapped=wrapped,
        )
        self.track_pid(process.pid)
        comm = asyncio.ensure_future(process.communicate())
        cancel_wait = (
            asyncio.ensure_future(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        try:
            if verdict:  # inside the try: a cancel here still reaps the child
                _prioritize_child(process.pid)
            waiters = [comm] + ([cancel_wait] if cancel_wait is not None else [])
            done, _pending = await asyncio.wait(
                waiters,
                timeout=timeout or None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if comm in done:
                stdout, stderr = comm.result()
                _note_exit(process.returncode, wrapped)
                return process.returncode, stdout or b"", stderr or b""
            # Cancelled or timed out before the process exited; the finally
            # block below terminates it and drains ``comm``.
            return None, b"", b""
        finally:
            if cancel_wait is not None and not cancel_wait.done():
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait
            # exit_timeout=0: nothing is reading the child's output any more, so
            # go straight to signalling instead of waiting out a voluntary exit.
            if not await self.reap(process, exit_timeout=0) and on_abandoned:
                on_abandoned(process.pid)
            if not comm.done():
                comm.cancel()
            # CancelledError is a BaseException, so it is NOT covered by
            # suppress(Exception); list it explicitly so a cancelled/timed-out
            # capture still returns (None, b"", b"") instead of propagating the
            # cancellation out of run_capture.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await comm
            self.untrack_pid(process.pid)

    async def hash_stdout(
        self,
        cmd: list[str],
        *,
        algorithm: str = "sha1",
        stdin_path: str | None = None,
        stdin_offset: int = 0,
        timeout: float | None = None,
        cancel_event: asyncio.Event | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Run a decoder that writes the decoded image to stdout; hash the stream.

        The streaming counterpart to :meth:`run_capture` for the Round-trip
        check (#301): stdout is read in chunks and hashed as it arrives, never
        buffered and never written to a temp file. ``stdin_path`` feeds the
        child that file from ``stdin_offset`` on (a container whose payload
        follows a header, piped to ``zstd -dc``), read on detached threads.

        Returns the hex digest (``services.stream_digest.new_hasher`` names).
        Raises :class:`ReadCancelled` when ``cancel_event`` fires,
        :class:`asyncio.TimeoutError` past ``timeout`` seconds, and
        ``RuntimeError`` (with the child's stderr tail) on a non-zero exit. The
        child is PID-tracked and stopped through the bounded :meth:`reap`
        ladder on every exit, so a cancel or a deadline stops the decoder.
        """
        from services.stream_digest import new_hasher  # import cycle

        # Command wrappers, never a preexec_fn: see priority_prefix. Resolved
        # before the cancel check, which then also covers its (bounded) wait.
        label = os.path.basename(cmd[0])
        # Spawned bare, its priority applied after (_prioritize_child): the
        # digest it streams is a Round-trip verdict, so no execvp wrapper may
        # start it.
        cmd, wrapped = await _wrapped_command(self._owner, cmd, wrap=False)
        if cancel_event is not None and cancel_event.is_set():
            raise ReadCancelled("Read cancelled")
        process = await self._spawn_piped(
            cmd,
            stdin=(
                asyncio.subprocess.PIPE
                if stdin_path is not None
                else asyncio.subprocess.DEVNULL
            ),
            env=env,
            wrapped=wrapped,
        )
        self.track_pid(process.pid)
        hasher = new_hasher(algorithm)
        stderr_tail = bytearray()

        async def _feed() -> None:
            await feed_stdin_from_file(process, stdin_path, offset=stdin_offset)

        async def _drain_stdout() -> None:
            while True:
                chunk = await process.stdout.read(1 << 20)
                if not chunk:
                    return
                hasher.update(chunk)

        async def _drain_stderr() -> None:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                stderr_tail.extend(chunk)
                del stderr_tail[:-4096]

        async def _run() -> None:
            parts = [_drain_stdout(), _drain_stderr()]
            if stdin_path is not None:
                parts.append(_feed())
            await asyncio.gather(*parts)
            await process.wait()

        work = asyncio.ensure_future(_run())
        cancel_wait = (
            asyncio.ensure_future(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        try:
            _prioritize_child(process.pid)
            done, _pending = await asyncio.wait(
                [work] + ([cancel_wait] if cancel_wait is not None else []),
                timeout=timeout or None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work not in done:
                if cancel_event is not None and cancel_event.is_set():
                    raise ReadCancelled("Read cancelled")
                raise asyncio.TimeoutError(f"decoder did not finish in {timeout}s")
            work.result()
        finally:
            for helper in (cancel_wait, work):
                if helper is not None and not helper.done():
                    helper.cancel()
            await self.reap(process, exit_timeout=0)
            for helper in (cancel_wait, work):
                if helper is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await helper
            self.untrack_pid(process.pid)
        _note_exit(process.returncode, wrapped)
        if process.returncode is not None and process.returncode < 0:
            raise RuntimeError(killed_by_signal_message(label, process.returncode))
        if process.returncode != 0:
            tail = clean_tool_output(stderr_tail.decode("utf-8", errors="replace"))
            raise RuntimeError(
                f"{label} exited with {process.returncode}"
                + (f": {tail.splitlines()[-1]}" if tail else ""),
            )
        return hasher.hexdigest()

    # --- verify ----------------------------------------------------------
    #
    # Two shared shapes cover every tool's verify subprocess, so the bound and
    # the cancel handling are written once instead of per tool (issue #266):
    # :meth:`run_verify` for a verifier that streams progress (chdman,
    # dolphin-tool) and :meth:`capture_verify` for one that says nothing until
    # it exits (maxcso ``--crc``, nsz ``-V``, ``7z t``). Both emit the same
    # ``{"type": "progress"|"complete"|"error"}`` events the verify SSE routes
    # and ``ToolPlugin.verify()`` already consume, and both honour the same
    # ``cancel_event`` so pressing Cancel actually stops the verifier.

    @staticmethod
    def _verify_error(
        message: str, *, cancelled: bool = False, abandoned: bool = False,
        inconclusive: bool = False, uncheckable: bool = False,
    ) -> dict:
        event = {"type": "error", "valid": False, "message": message}
        if uncheckable:
            # The tool won't check this file here (an archive safety rule
            # refused it): no verdict, and unlike ``inconclusive`` nothing to
            # retry. The CLI reports it as skipped, never failed (#306).
            event["uncheckable"] = True
        if inconclusive:
            # The check ended without a verdict on the file: its own bound
            # expired, it stalled, or the file could not be read. "valid":
            # False still stops a delete, but a caller holding a stored result
            # (the CLI's verify, #306) must not drop it on this.
            event["inconclusive"] = True
        if cancelled:
            # Distinguishes "the operator stopped this" from "this file is
            # bad": job_manager turns the flag into a CANCELLED job rather than
            # a failed one, and no caller may record a verification result from
            # a run that never finished.
            event["cancelled"] = True
        if abandoned:
            # Distinguishes "this file failed" from "cleanup itself failed":
            # a verifier that outlived SIGKILL is still holding the storage,
            # so a caller working through a list must stop rather than open the
            # next file against it.
            event["abandoned"] = True
        return event


    async def capture_verify(
        self,
        cmd: list[str],
        *,
        path: str,
        success_message: str,
        cancel_event: asyncio.Event | None = None,
        env: Mapping[str, str] | None = None,
        start_message: str = "Verifying integrity...",
        on_success_output: Callable[[bytes], None] | None = None,
        size: int | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run a one-shot verifier, yielding this tool's verify events.

        For a verifier that prints nothing useful until it exits: the whole run
        is one :meth:`run_capture` bounded by :func:`resolve_verify_timeout` for
        ``path`` and racing ``cancel_event`` -- which the bound resolution takes
        too, since sizing the file is another stat of the same storage.
        The whole is wrapped in the 0%/100% progress
        events the SSE routes expect. ``run_capture`` reports both an abort and
        a timeout as a ``None`` return code, so the two are told apart here by
        asking the cancel event which one happened.

        ``on_success_output`` gets the verifier's output after a passing run,
        for a tool whose verifier also prints a hash the Round-trip can reuse
        (nsz's ``[NSP SHA256]``), so that verifier runs once per job.
        """
        yield {"type": "progress", "progress": 0, "message": start_message}
        # One the verifier can't open is no verdict, decided before it runs
        # (#359): its exit would read as one.
        problem = await verify_readable(path, cancel_event=cancel_event)
        if problem is not None:
            yield problem
            return
        # *size*: what the verifier reads, when it isn't the file's own size
        # (an archive's declared contents, #431).
        sized = {"size": size} if size is not None else {}
        timeout = await resolve_verify_timeout(
            path, cancel_event=cancel_event, **sized,
        )
        if cancel_event is not None and cancel_event.is_set():
            # Same re-check as run_verify: resolving the bound above can take
            # the full probe bound on unresponsive storage.
            yield self._verify_error("Verification cancelled", cancelled=True)
            return
        abandoned_pid: list[int] = []
        try:
            returncode, stdout, _ = await self.run_capture(
                cmd,
                timeout=timeout or None,
                cancel_event=cancel_event,
                stderr_to_stdout=True,
                env=env,
                on_abandoned=abandoned_pid.append,
                verdict=True,
            )
        except OSError as exc:
            # The verifier couldn't be started: no verdict, so no stored check
            # is overwritten.
            yield verify_exception_event(exc, prefix="Verification could not run")
            return
        if abandoned_pid:
            # Outranks both branches below: the child outlived SIGKILL, so it is
            # still holding the storage whether the operator cancelled or the
            # bound expired, and a caller walking a list has to stop.
            yield abandoned_verify_error(abandoned_pid[0])
            return
        if returncode == 0:
            if on_success_output is not None:
                on_success_output(stdout or b"")
            yield {"type": "progress", "progress": 100, "message": "Integrity check passed"}
            yield {"type": "complete", "valid": True, "message": success_message}
            return
        if returncode is None:
            if cancel_event is not None and cancel_event.is_set():
                yield self._verify_error("Verification cancelled", cancelled=True)
            else:
                yield self._verify_error(
                    f"Verification timed out after {timeout}s", inconclusive=True,
                )
            return
        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        tail = "\n".join(output.splitlines()[-5:]) if output else "verification failed"
        yield verify_exit_event(returncode, f"Integrity check failed: {tail}")

    async def run_verify(
        self,
        cmd: list[str],
        *,
        path: str,
        parse_progress: Callable[[str], int | None],
        success_message: str,
        failure_message: str,
        cancel_event: asyncio.Event | None = None,
        judge_output: Callable[[list[str]], str | None] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream a verifier's output, yielding this tool's verify events.

        ``judge_output`` is for a verifier whose exit code is not the whole
        verdict: it gets every output line (stderr folded in) after an exit 0
        and returns ``None`` to pass or a reason to fail. Without it, exit 0
        passes.

        The streaming counterpart to :meth:`capture_verify`, and the shared
        replacement for the two near-identical read loops chdman and
        dolphin-tool each carried. Every bound the conversion path already has
        applies here too (issue #266):

        * an **overall** bound from :func:`resolve_verify_timeout` for ``path``
          (baseline + per-GiB allowance), so a verify that never returns ends;
        * a **stall** bound (``COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT``)
          on a verifier that streams but goes silent, which catches a wedge long
          before the overall bound would;
        * ``cancel_event``, so Cancel terminates the verifier instead of leaving
          the UI on *Cancelling...* until the read loop happens to end;
        * the bounded :meth:`reap` ladder rather than a bare ``process.wait()``,
          which on a child stuck in uninterruptible I/O never returns and
          freezes the queue behind it (the issue #263 failure, on this path).

        ``parse_progress`` is the only per-tool knob; line segmentation is the
        shared :func:`_split_stream_lines`, so a percentage redraw split across
        read chunks still parses.
        """
        # Spawned bare, its priority applied after (_prioritize_child): its
        # exit code is the verdict, so no execvp wrapper may start it.
        cmd, wrapped = await _wrapped_command(self._owner, cmd, wrap=False)

        # One the verifier can't open is no verdict, decided before it runs
        # (#359): chdman's exit 1 on "Permission denied" would read as one.
        problem = await verify_readable(path, cancel_event=cancel_event)
        if problem is not None:
            yield problem
            return

        # Resolve the bounds *before* spawning. They stat the file, and an await
        # between the spawn and the try/finally below is a window where a
        # cancellation (the verify SSE route cancels its task when the client
        # disconnects) unwinds this coroutine with the child already running and
        # tracked, but with nothing to reap or untrack it.
        overall_timeout = await resolve_verify_timeout(
            path, cancel_event=cancel_event,
        )
        stall_timeout = verify_stall_timeout()
        if cancel_event is not None and cancel_event.is_set():
            # Re-checked after the bound is resolved, because resolving it can
            # take the full probe bound on storage that has stopped answering
            # -- and spawning into that would mean a child that immediately
            # blocks and may outlive SIGKILL, turning a prompt cancellation
            # into an abandoned process for no work gained.
            yield self._verify_error("Verification cancelled", cancelled=True)
            return

        try:
            process = await asyncio.create_subprocess_exec(  # nosemgrep
                cmd[0], *cmd[1:],
                # Never the caller's terminal: a tool that asks for input (7z
                # wanting a password) fails at once instead of waiting (#306).
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            if wrapped:
                _forget_wrappers()
            # The verifier couldn't be started (ENOEXEC, missing): no verdict,
            # the same event capture_verify gives for it.
            yield verify_exception_event(exc, prefix="Verification could not run")
            return
        self.track_pid(process.pid)
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                "Starting %s verify pid=%s path=%s", self._owner, process.pid, path,
            )

        output_lines: list[str] = []
        buffer = ""
        start = time.monotonic()
        last_output_at = start
        terminal: dict | None = None
        # Set once the ladder has run and given up, so teardown does not repeat
        # TERM/KILL on a child known to be unkillable -- that second pass is
        # another 15s holding the verify lane (and, at MAX_CONCURRENT_JOBS=1,
        # the queue) for no possible gain. Same guard run() carries.
        reap_failed = False

        async def _stop() -> None:
            # TERM -> KILL with grace, then give up rather than waiting forever:
            # the point of every bound here is that this coroutine ends.
            nonlocal reap_failed
            if not await self.reap(process, exit_timeout=0):
                reap_failed = True

        async def _check_limits(now: float) -> bool:
            nonlocal terminal
            if cancel_event is not None and cancel_event.is_set():
                terminal = self._verify_error("Verification cancelled", cancelled=True)
            elif overall_timeout > 0 and now - start >= overall_timeout:
                terminal = self._verify_error(
                    f"Verification timed out after {overall_timeout}s",
                    inconclusive=True,
                )
            elif stall_timeout > 0 and now - last_output_at >= stall_timeout:
                terminal = self._verify_error(
                    f"Verification stalled: no output for {stall_timeout}s",
                    inconclusive=True,
                )
            else:
                return False
            await _stop()
            if reap_failed:
                # Abandonment outranks whatever prompted the stop. The child is
                # still running and still holding the storage, which is a
                # bigger fact than "the operator cancelled" -- and the caller
                # walking a list has to know to stop. Same precedence run()
                # gives it.
                terminal = abandoned_verify_error(process.pid)
            return True

        try:
            _prioritize_child(process.pid)
            while True:
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(100), timeout=2)
                except asyncio.TimeoutError:
                    if await _check_limits(time.monotonic()):
                        break
                    continue
                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")
                last_output_at = time.monotonic()
                lines, buffer = _split_stream_lines(buffer)
                for line in lines:
                    output_lines.append(line)
                    yield {
                        "type": "progress",
                        "progress": parse_progress(line),
                        "message": line,
                    }
                if await _check_limits(time.monotonic()):
                    break

            if terminal is None:
                if buffer.strip():
                    line = buffer.strip()
                    output_lines.append(line)
                    yield {
                        "type": "progress",
                        "progress": parse_progress(line),
                        "message": line,
                    }
                # The voluntary-exit grace runs under the same live checks the
                # read loop ran, rather than as one blocking wait: a verifier
                # that closed stdout without exiting is still subject to the
                # cancel and to both deadlines. Deciding the grace once, up
                # front, would cover neither a cancel pressed a second later
                # nor a stall-only configuration -- so it is sliced, and each
                # slice re-asks the same question the read loop asked.
                grace_until = time.monotonic() + _EXIT_GRACE
                stopped = False
                while process.returncode is None:
                    now = time.monotonic()
                    if await _check_limits(now):
                        stopped = True
                        break
                    if now >= grace_until:
                        break
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            process.wait(),
                            timeout=min(_EXIT_POLL, grace_until - now),
                        )
                if not stopped and not await self.reap(process, exit_timeout=0):
                    reap_failed = True
                    terminal = abandoned_verify_error(process.pid)

            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "%s verify pid=%s exit=%s",
                    self._owner, process.pid, process.returncode,
                )

            if terminal is not None:
                yield terminal
                return

            # A verifier that reports a mismatch but still exits 0 (chdman)
            # names the reason in its output; the tool's judge reads it.
            rejected = (
                judge_output(output_lines)
                if process.returncode == 0 and judge_output is not None
                else None
            )
            if rejected:
                yield self._verify_error(rejected)
            elif process.returncode == 0:
                yield {"type": "complete", "valid": True, "message": success_message}
            else:
                output = "\n".join(output_lines[-20:]).strip()
                yield verify_exit_event(process.returncode, output or failure_message)
        finally:
            # Skipped when the ladder already exhausted itself: repeating it on a
            # child that survived SIGKILL only burns another 15s.
            if not reap_failed:
                await self.reap(process, exit_timeout=0)
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
        mode: str | None = None,
        env: Mapping[str, str] | None = None,
        require_output: bool = False,
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

        **Every run reports status, with no per-tool wiring.** ``parse_progress``
        is the preferred signal and always wins: as soon as it returns a real
        percent, that tool is reporting for itself. When it never does — the
        common case, since most of these CLIs draw a TTY bar that falls silent
        on a pipe — the runner falls back to the growing output file, emitting
        bytes-written and a MB/min rate (:func:`output_size_message`) so a slow
        job is visibly slow rather than indistinguishable from a hung one. Pass
        ``mode`` to additionally get a percentage from :data:`SIZE_RATIOS`; a
        mode absent from that table still gets the message, so a new tool needs
        no wiring to be observable and a ratio only sharpens the bar.
        Updates carry ``"activity": True`` when this tick represented real
        forward movement -- a parsed percentage that advanced, or the output
        file growing -- and omit it for keep-alives. It is the same signal the
        stall watchdog runs on, published so callers judging liveness do not
        have to re-derive it from a proxy: percentage alone stops moving at the
        fallback's 95% cap and never moves for a mode with no size ratio, and
        update arrivals alone count heartbeats as progress (issue #263).

        ``initial_progress`` seeds the floor with the caller's preamble (e.g. a
        service's "Starting..." yield at 1/5%) so an early non-parseable line
        cannot drop the bar below it.

        ``cmd`` is the bare argv: the runner wraps it with :func:`priority_prefix`
        (``nice``/``ionice``), never a ``preexec_fn``. ``env`` is forwarded to
        the subprocess (nsz runs with a private keys-home env).

        ``require_output`` makes a clean exit (return code 0) that left no file
        at ``output_path`` a failure, raised as a ``RuntimeError`` carrying the
        same stdout tail as the non-zero-exit path — so a tool that exits 0
        without producing output still reports the reason it printed first,
        rather than a bare "no output" message. Used by nsz, whose ``output_path``
        is the temp file the runner already watches.
        """
        output_dir = os.path.dirname(output_path)
        if output_dir:
            # Bounded like every other filesystem call here: an unresponsive
            # output mount must fail this job, not hang it -- and hanging here,
            # before the child exists, would freeze the whole queue behind it
            # (issue #263).
            try:
                await _bounded_probe(os.makedirs, output_dir, exist_ok=True)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"{fail_label}: output directory {output_dir} stopped responding"
                ) from None

        # nice/ionice as command wrappers, never a preexec_fn: see
        # priority_prefix. No cancel check before the spawn, deliberately:
        # ConversionCancelled from run() means the child ran, which callers
        # rely on before removing a partial output (see
        # test_cancel_event_preset_terminates_and_raises); a cancel that landed
        # meanwhile is honoured by the watcher as soon as it spawns.
        cmd, wrapped = await _wrapped_command(self._owner, cmd)

        # cmd is built from validated settings paths (no shell interpretation);
        # create_subprocess_exec passes the arg list directly (shell=False), so
        # there is no shell expansion / injection surface. Same call shape that
        # already lives unsuppressed in chdman/dolphin/z3ds; flagged here only
        # because the line is new in this extracted module.
        try:
            process = await asyncio.create_subprocess_exec(  # nosemgrep
                cmd[0], *cmd[1:],
                # Never the caller's terminal: a tool that asks for input (7z
                # wanting a password) fails at once instead of waiting (#306).
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=env,
            )
        except OSError:
            if wrapped:
                _forget_wrappers()
            raise
        self.track_pid(process.pid)
        # Everything below runs under try/finally so that if the caller stops
        # iterating (generator aclose / task cancellation) or an unexpected
        # error fires, the subprocess, the cancel-watcher task and the PID entry
        # are always cleaned up rather than leaked.
        cancel_task = None
        # Bound before the try: the finally tears these down, so an early failure
        # must not hit an unbound name and mask the real error.
        size_probe: asyncio.Task | None = None
        # Set once the ladder has already run and given up, so teardown does not
        # repeat TERM/KILL on a child known to be unkillable -- that second pass
        # is another 15s holding the queue's only slot for no possible gain.
        reap_failed = False
        try:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "Starting %s pid=%s cmd=%s",
                    self._owner, process.pid, " ".join(cmd),
                )

            # Sizing the adaptive stall timeout stats the input. On a dead
            # mount that must not hang the spawn path, so fall back to the
            # non-adaptive baseline rather than waiting -- a watchdog with a
            # rough bound beats no watchdog.
            try:
                stall_timeout = await _bounded_probe(
                    compute_progress_stall_timeout,
                    input_path=input_path,
                    base_timeout=getattr(settings, "progress_timeout", 0),
                    timeout_per_gib=getattr(settings, "progress_timeout_per_gib", 0),
                    timeout_cap=getattr(settings, "progress_timeout_cap", 0),
                )
            except asyncio.TimeoutError:
                stall_timeout = max(0, int(getattr(settings, "progress_timeout", 0) or 0))
            if 0 < stall_timeout < _MIN_STALL_TIMEOUT:
                self._logger.warning(
                    "Stall timeout of %ss is below the %ss sampling floor; using %ss. "
                    "A shorter window cannot tell a stalled converter from one that "
                    "simply has not been sampled yet.",
                    stall_timeout, _MIN_STALL_TIMEOUT, _MIN_STALL_TIMEOUT,
                )
                stall_timeout = _MIN_STALL_TIMEOUT
            # Seed the progress floor with the caller's preamble (e.g. the
            # service's "Starting..." yield at 1/5%) so an early non-parseable
            # stdout line — which emits last_progress_value — can't drop the bar
            # below it before the first size-growth tick.
            last_progress_value = initial_progress
            last_output_size: int | None = None
            last_activity_at = time.monotonic()
            # Start of the current rate window (last observed growth).
            last_growth_at = last_activity_at
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
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    except ProcessLookupError:
                        # The child exited between the returncode check and
                        # terminate(); the cancel is already recorded, so swallow
                        # it. Otherwise the watcher task ends in an exception that
                        # the finally re-raises, masking the run's real result.
                        pass

                cancel_task = asyncio.create_task(_cancel_watcher())

            buffer = ""
            output_lines: list[str] = []
            stall_error: str | None = None
            last_message = ""
            abandoned_error: str | None = None
            # Native stdout parsing is the preferred signal; the size-growth
            # fallback below only speaks while this stays False.
            saw_native_progress = False
            ratio = size_ratio_for(mode)
            expected_size = 0
            if ratio:
                # Unbounded here would hang the job after the child is already
                # running, with no stall loop yet to end it. No sample just means
                # no percentage; the bytes/rate message needs no ratio.
                try:
                    input_size = await _bounded_probe(os.path.getsize, input_path)
                except (asyncio.TimeoutError, OSError):
                    input_size = None
                if input_size is not None:
                    expected_size = max(1, int(input_size * ratio))

            def _record_line(line: str) -> None:
                if not output_lines or output_lines[-1] != line:
                    output_lines.append(line)
                    if len(output_lines) > 30:
                        output_lines.pop(0)

            probed_size: int | None = None
            last_probe_at = 0.0
            # Whether any probe has ever finished. Distinguishes "no measurement
            # yet" from "measured, and there is no output file".
            probe_completed = False

            def _measure_output_sync() -> int | None:
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

            def _measure_output() -> int | None:
                """Most recent output size, measured off the event loop.

                ``getsize`` on an unresponsive mount blocks in uninterruptible
                I/O, and inline that would freeze the entire event loop --
                including the stall watchdog and the ``reap()`` ladder that exist
                to rescue exactly this situation (issue #263). So the probe runs
                in a worker thread and is *never awaited*: each tick reads the
                last completed measurement and kicks off the next. Single-flight,
                so a wedged mount costs one blocked thread for the life of the
                job rather than one per tick. The cost is that the size is one
                tick (~2s) stale, which no consumer here cares about.
                """
                nonlocal size_probe, probed_size, probe_completed, last_probe_at
                if size_probe is not None and size_probe.done():
                    if not size_probe.cancelled():
                        probe_completed = True
                        with contextlib.suppress(Exception):
                            probed_size = size_probe.result()
                    size_probe = None
                now = time.monotonic()
                if size_probe is None and now - last_probe_at >= _PROBE_INTERVAL:
                    last_probe_at = now
                    try:
                        size_probe = _probe_in_daemon_thread(_measure_output_sync)
                    except ProbeCapacityExceeded:
                        # No sample this tick. The growth signal degrades to
                        # "nothing observed", which the stall watchdog already
                        # handles -- and which is the correct reading anyway
                        # when that many probes are stuck on this storage.
                        size_probe = None
                return probed_size

            def _update_output_activity(now: float):
                nonlocal last_output_size, last_activity_at
                size = _measure_output()
                if size is None:
                    return
                if last_output_size is None or size > last_output_size:
                    last_output_size = size
                    last_activity_at = now

            def _size_update(now: float) -> dict | None:
                # Status from output-file growth, the universal fallback for a
                # tool whose stdout tells us nothing: every conversion writes a
                # file, so a growing file is a progress signal even when the CLI
                # prints no parseable percent (its TTY bar goes silent on a pipe)
                # -- which is most of them. This is what makes a merely *slow*
                # job legible instead of indistinguishable from a hung one
                # (issue #263).
                #
                # Native parsing wins whenever it works: once a real percent has
                # been parsed, that tool is reporting for itself and the fallback
                # stands down rather than fighting it for the message line. A
                # percentage is emitted only for a mode with a known size ratio;
                # otherwise the bar holds at its floor and the message alone
                # carries the news, which needs no ratio and so costs a new tool
                # no wiring at all.
                nonlocal last_output_size, last_activity_at, last_progress_value
                nonlocal last_growth_at
                size = _measure_output()
                if size is None:
                    return None
                if last_output_size is not None and size <= last_output_size:
                    return None
                delta_bytes = size - (last_output_size or 0)
                delta_seconds = now - last_growth_at
                last_output_size = size
                last_growth_at = now
                last_activity_at = now
                if saw_native_progress:
                    # The tool speaks for itself, so don't replace its status
                    # line -- but a file that is still growing is still
                    # liveness, and a native tool can sit on one integer
                    # percentage for many minutes on a large image. Republish
                    # the current status carrying the flag so the job manager's
                    # clock sees it (issue #263).
                    return {
                        "progress": last_progress_value,
                        "message": last_message,
                        "activity": True,
                    }
                progress = last_progress_value
                if expected_size:
                    # Clamped to the floor: an estimate must never walk the bar
                    # backward from a higher seeded value.
                    progress = max(progress, output_size_progress(size, expected_size))
                    last_progress_value = progress
                return {
                    "progress": progress,
                    "message": output_size_message(size, delta_bytes, delta_seconds),
                    "activity": True,
                }

            async def _check_stall(now: float) -> bool:
                nonlocal stall_error
                if stall_timeout <= 0:
                    return False
                _update_output_activity(now)
                if not probe_completed and now - start < _FIRST_SAMPLE_GRACE:
                    # No growth measurement has landed yet: the probe is off the
                    # event loop and read one tick later, so the very first
                    # checks have nothing to judge by. Don't call a stall on the
                    # absence of a measurement -- with a stall timeout shorter
                    # than the ~2s probe cadence that would kill a converter
                    # whose output is growing steadily.
                    #
                    # Bounded by the grace, though: a probe against a dead mount
                    # never completes, and waiting on it forever would disable
                    # the watchdog entirely and hang the queue on exactly the
                    # storage failure this exists to catch. Past the grace the
                    # check proceeds without a sample, so an unresponsive output
                    # still stalls out on schedule.
                    return False
                if now - last_activity_at < stall_timeout:
                    return False
                stall_error = (
                    "Conversion stalled: no progress increase or output growth "
                    f"for {stall_timeout}s (progress={last_progress_value}%,"
                    f" output_size={last_output_size})"
                )
                if process.returncode is None:
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    except ProcessLookupError:
                        pass
                return True

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(100), timeout=2,
                    )
                except asyncio.TimeoutError:
                    if cancel_event and cancel_event.is_set():
                        # Stop reading once cancellation is requested. A child
                        # that is still running is terminated by the watcher
                        # (-> cancelled_by_request -> ConversionCancelled); a
                        # child that has already exited 0 finished in that instant
                        # and is reported complete. A cancel that races a clean
                        # exit delivers the result rather than discarding it (a
                        # deliberate product choice for this inherently ambiguous
                        # race).
                        break
                    now = time.monotonic()
                    update = _size_update(now)
                    if update is not None:
                        yield update
                        # A size update carries strictly more than the generic
                        # heartbeat (bytes and a rate, not just elapsed seconds),
                        # so it stands in for one rather than being overwritten
                        # by one on the same tick.
                        last_heartbeat_at = now
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

                lines, buffer = _split_stream_lines(buffer)
                for line in lines:
                    _record_line(line)
                    now = time.monotonic()
                    progress = parse_progress(line)
                    advanced = False
                    if progress is not None:
                        saw_native_progress = True
                        if progress > last_progress_value:
                            last_progress_value = progress
                            last_activity_at = now
                            advanced = True
                    # Clamp to the running floor (incl. initial_progress) so a
                    # parsed value below it can't move the bar backward.
                    last_message = line
                    update = {"progress": last_progress_value, "message": line}
                    if advanced:
                        update["activity"] = True
                    yield update
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
                advanced = False
                if progress is not None:
                    saw_native_progress = True
                    if progress > last_progress_value:
                        last_progress_value = progress
                        last_activity_at = now
                        advanced = True
                last_message = line
                update = {"progress": last_progress_value, "message": line}
                if advanced:
                    update["activity"] = True
                yield update
                update = _size_update(time.monotonic())
                if update is not None:
                    yield update
                await _check_stall(time.monotonic())

            # The read loop can drain stdout to EOF without ever yielding, so a
            # cancel set before or during that drain may not have reached the
            # watcher yet. If the reap below sent TERM first, the watcher would
            # then find an exited child and never record the cancel, and a job
            # the user cancelled would be reported as a failure (return code
            # -15). Let the watcher act first: it records the cancel only while
            # the child is still running, so a child that already finished stays
            # complete. Bounded: the watcher waits its own TERM grace, then sends
            # KILL without waiting, and the reap below handles anything left.
            if cancel_task is not None and cancel_event.is_set() and not cancel_task.done():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(cancel_task), timeout=_TERM_GRACE + 1,
                    )

            # A cancel or a stall already sent TERM (and KILL). Waiting out the
            # voluntary-exit grace again would hold the queue's only slot for
            # another minute for no reason, so go straight to the ladder.
            already_signalled = stall_error is not None or (
                cancel_event is not None and cancel_event.is_set()
            )
            if not await self.reap(
                process, exit_timeout=0 if already_signalled else _EXIT_GRACE,
            ):
                reap_failed = True
                abandoned_error = (
                    f"{fail_label} did not exit and could not be killed "
                    f"(pid {process.pid}); it is likely blocked on unresponsive "
                    "storage. Abandoning it so the queue can continue."
                )
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "%s pid=%s exit=%s", self._owner, process.pid, process.returncode,
                )

            if abandoned_error:
                # Abandonment outranks the stall that triggered it: the child is
                # still alive and still holding its output, which is the part an
                # operator has to act on.
                raise RuntimeError(
                    f"{stall_error} {abandoned_error}" if stall_error else abandoned_error
                )

            if stall_error:
                raise RuntimeError(stall_error)

            # Only raise when this run actually observed cancellation while the
            # process was still running (the watcher sets the flag only if
            # returncode was None when the event fired). A child that finished
            # before cancellation took effect is reported complete — its output
            # is valid and must not be deleted as a false cancellation.
            if cancelled_by_request:
                if abandoned_error:
                    # Reporting a clean CANCELLED would be a lie: the child is
                    # still running and still holding its output.
                    raise RuntimeError(
                        f"Cancellation did not stop {fail_label}. {abandoned_error}"
                    )
                raise ConversionCancelled("Conversion cancelled")

            # Nothing below can be trusted for an abandoned child: it has no
            # return code and never will.
            if abandoned_error:
                raise RuntimeError(abandoned_error)

            _note_exit(process.returncode, wrapped)
            if process.returncode is not None and process.returncode < 0:
                # Killed (by the OOM killer, most likely): its last output is
                # a progress line, not the reason, so it goes to the log only.
                self._logger.warning(
                    "%s pid=%s was killed by signal %s; last output: %r",
                    fail_label, process.pid, -process.returncode,
                    "\n".join(output_lines[-6:]),
                )
                raise RuntimeError(killed_by_signal_message(fail_label, process.returncode))
            if process.returncode != 0:
                tail = clean_tool_output("\n".join(output_lines[-6:]))
                if tail:
                    raise RuntimeError(
                        f"{fail_label} failed with return code {process.returncode}."
                        f"\nLast output:\n{tail}",
                    )
                raise RuntimeError(
                    f"{fail_label} failed with return code {process.returncode}",
                )

            # A clean exit that left no file at output_path is an anomaly the
            # caller asked us to enforce (require_output): surface it with the
            # recorded stdout tail, since a tool that exits 0 without producing
            # output usually printed the reason first (e.g. nsz, whose output is
            # the temp file the runner already watches).
            if require_output and not os.path.exists(output_path):
                tail = clean_tool_output("\n".join(output_lines[-6:]))
                if tail:
                    raise RuntimeError(
                        f"{fail_label} produced no output file.\nLast output:\n{tail}",
                    )
                raise RuntimeError(f"{fail_label} produced no output file")

            yield {"progress": 100, "message": complete_message, "activity": True}
        finally:
            self.untrack_pid(process.pid)
            if size_probe is not None and not size_probe.done():
                size_probe.cancel()
            if cancel_task:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
            # exit_timeout=0: nothing is reading the child's output any more, so
            # go straight to signalling instead of waiting out a voluntary exit.
            if not reap_failed:
                await self.reap(process, exit_timeout=0)
