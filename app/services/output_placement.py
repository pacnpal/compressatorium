"""The final move of a finished product onto its output path, safe to give up on.

A tool that writes into a private work folder (extract-xiso, rom-converto)
leaves the service one last step: move the product onto the output path. That
step can block on a stuck mount and complete long after the job gave up, so
three rules hold here, for every such service:

* **Never over anything.** The move is ``place_without_clobbering``: a file
  already at the output path was not written by this job (an overwrite job's
  old output was removed before conversion, and a non-overwrite job's
  destination was free when planned), so it is refused, never replaced.
* **Only a move that has completed makes the output this job's.** Until then,
  whatever is at the output path belongs to someone else, even if the move has
  started and is stuck. :meth:`OutputPlacement.abandon_and_sweep` removes the
  product only when the move completed, and only while the path is still that
  same file (device and inode).
* **A move that lands after the job gave up removes its own product**, again
  only while it is still the same file, so nothing is left behind either way.

The completion and the giving-up are synchronized under a lock held only for a
couple of assignments, never across I/O. :meth:`OutputPlacement.place` is
blocking: callers run it detached and bounded
(``run_detached_within_stall_bound``), since the child's stall watchdog has
already stopped by then.
"""
from __future__ import annotations

import os
import threading

from services.subprocess_runner import remove_partial_output
from utils.path_utils import is_same_file, place_without_clobbering, remove_if_same_file


class OutputPlacement:
    """Move one product onto *output_path*; *label* names the format in errors
    ("XISO", "ZAR")."""

    def __init__(self, output_path: str, *, label: str) -> None:
        self._output_path = output_path
        self._label = label
        self._lock = threading.Lock()
        self._done = False
        self._abandoned = False
        self._ours: os.stat_result | None = None

    def place(self, produced: str) -> None:
        """Move *produced* onto the output path. Blocking.

        Raises ``RuntimeError`` naming the format when a file is already there
        or the move fails.
        """
        ours = os.stat(produced)
        try:
            place_without_clobbering(produced, self._output_path)
        except FileExistsError as exc:
            raise RuntimeError(
                f"{self._output_path} appeared while the {self._label} was being "
                "written; not overwriting it",
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Could not place the {self._label} output: {exc}") from exc
        with self._lock:
            self._done, self._ours = True, ours
            abandoned_first = self._abandoned
        if abandoned_first:
            # Only while it is still our product: never a file someone else has
            # put at the same name since.
            remove_if_same_file(self._output_path, ours)

    def abandon(self) -> os.stat_result | None:
        """Give up on the move: the placed product's identity when it completed,
        else None (and a move still in flight removes its product when it lands)."""
        with self._lock:
            self._abandoned = True
            return self._ours if self._done else None

    async def abandon_and_sweep(self) -> None:
        """:meth:`abandon`, then remove the placed product while it is still ours.

        Bounded like any partial-output sweep (the output volume is the one most
        likely to have just failed); a move that never completed leaves the
        output path untouched.
        """
        ours = self.abandon()
        if ours is None:
            return
        output_path = self._output_path
        await remove_partial_output(
            discover=lambda: [output_path] if is_same_file(output_path, ours) else [],
            label=f"{self._label} output {output_path}",
        )
