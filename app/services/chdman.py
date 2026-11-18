from __future__ import annotations
import asyncio
from logging_setup import get_logger
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import (
    ReadCancelled,
    StorageAbandoned,
    abandonment_checkpoint,
    ConversionCancelled,
    SubprocessRunner,
    collect_verify,
    info_timeout,
)

# Re-exported for backwards compatibility: ``ConversionCancelled`` historically
# lived here and is imported as ``from services.chdman import ConversionCancelled``
# by job_manager, dolphin_tool and z3ds_compress.  Keeping the import above (not
# redefining the class) preserves its identity so those ``except`` clauses still
# catch it.
__all__ = ["ConversionCancelled", "ChdmanService", "chdman_service"]

CHDMAN_CONVERTIBLE_EXTENSIONS = {".gdi", ".iso", ".cue", ".bin"}
CONVERTIBLE_EXTENSIONS = CHDMAN_CONVERTIBLE_EXTENSIONS

logger = get_logger("chdman")


class ChdmanService:
    """Wrapper for chdman binary."""

    def __init__(self):
        self.chdman_path = settings.chdman_path
        self._runner = SubprocessRunner(owner="chdman")

    def _build_command(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        compression: str | None = None,
    ) -> list[str]:
        cmd = [self.chdman_path, mode, "-f", "-i", input_path, "-o", output_path]
        if mode == "createraw":
            # chdman refuses a raw image without a unit size; 512 is the
            # sector size of the raw hard-disk images this mode targets.
            cmd = cmd[:2] + ["-us", "512"] + cmd[2:]
        if mode == "createdvd":
            # Insert -hs 2048 after mode for PSP compatibility
            cmd = [
                self.chdman_path,
                mode,
                "-hs",
                "2048",
                "-f",
                "-i",
                input_path,
                "-o",
                output_path,
            ]

        if compression and mode in {
            "createcd",
            "createdvd",
            "createraw",
            "createhd",
            "createld",
            "copy",
        }:
            cmd = cmd[:2] + ["-c", compression] + cmd[2:]

        return cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    def abandoned_pids(self) -> list[int]:
        """Children that outlived SIGKILL; see ``SubprocessRunner``."""
        return self._runner.abandoned_pids()

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "createcd",
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run chdman conversion and yield ``{"progress", "message"}`` updates."""
        cmd = self._build_command(
            mode, input_path, output_path, compression=compression,
        )
        async for update in self._runner.run(
            cmd,
            input_path=input_path,
            output_path=output_path,
            parse_progress=self._parse_progress,
            cancel_event=cancel_event,
            fail_label="chdman",
            mode=mode,
        ):
            yield update

    @property
    def runner(self) -> SubprocessRunner:
        """This tool's runner, shared with helpers that spawn chdman children.

        ``services.disc_id`` shells out to chdman for the GAME/NAME tag work, so
        it goes through this same instance rather than one of its own: one PID
        set that ``active_pids()`` fully describes, and one place that records a
        child the teardown ladder had to give up on.
        """
        return self._runner

    async def info(
        self, chd_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        """Get information about a CHD file.

        Goes through the shared capture rather than a hand-rolled spawn: that
        tracks the PID, bounds the teardown (the old path ended in an unbounded
        ``wait()`` after ``kill()``), applies the tool priority policy, and
        reports an unkillable child into any open ``collect_abandonment()`` sink
        so a scan walking a library can stop rather than strand one per file
        (issue #268).
        """
        timeout = info_timeout(self._runner.owner)
        with abandonment_checkpoint() as abandoned:
            returncode, stdout, stderr = await self._runner.run_capture(
                [self.chdman_path, "info", "-i", chd_path],
                timeout=timeout or None,
                cancel_event=cancel_event,
            )
        # An ordinary timeout leaves the child dead; this one does not, and the
        # caller's answer differs (retry vs. tell the client the storage is not
        # answering). Folding both into one RuntimeError makes it a generic 500
        # and invites a refresh that strands another child (issue #268).
        if abandoned:
            raise StorageAbandoned(
                f"chdman info on the file left {', '.join(abandoned)} stuck on "
                "unresponsive storage; it is still running"
            )
        if returncode is None:
            if cancel_event is not None and cancel_event.is_set():
                # The caller's cancel stopped the child: no answer, no error.
                raise ReadCancelled("chdman info cancelled")
            raise RuntimeError(f"chdman info timed out after {timeout}s")

        if returncode != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip()
                or f"chdman info failed with code {returncode}",
            )

        return self._parse_info(stdout.decode())

    async def extract_cd(
        self,
        chd_path: str,
        sheet_path: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """``chdman extractcd`` the CD in ``chd_path`` to ``sheet_path``.

        The sheet's extension picks the format (``.cue`` or ``.gdi``); the track
        files land beside it. Unbounded here: the Round-trip check bounds the
        whole digest. True only when chdman exited 0; a cancel, a timeout or a
        failure is False.
        """
        returncode, _, stderr = await self._runner.run_capture(
            [self.chdman_path, "extractcd", "-f", "-i", chd_path, "-o", sheet_path],
            cancel_event=cancel_event,
        )
        if returncode != 0:
            logger.warning(
                "chdman extractcd %s for %s: %s",
                "aborted" if returncode is None else f"failed ({returncode})",
                chd_path, stderr.decode(errors="replace").strip(),
            )
            return False
        return True

    async def verify(
        self, chd_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        """Verify the integrity of a CHD file.

        Returns:
            dict: {"valid": bool, "message": str}, plus "cancelled": True when
            ``cancel_event`` stopped the run before it reached a verdict.

        """
        return await collect_verify(
            self.verify_stream(chd_path, cancel_event=cancel_event),
            fallback_message="CHD verification failed",
        )

    def verify_stream(
        self, chd_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream ``chdman verify`` progress.

        The loop itself is the shared :meth:`SubprocessRunner.run_verify` --
        bounds, cancellation, line segmentation and the reap ladder are the same
        for every streaming verifier, so chdman contributes only its command and
        its progress parser.
        """
        return self._runner.run_verify(
            [self.chdman_path, "verify", "-i", chd_path],
            path=chd_path,
            parse_progress=self._parse_progress,
            success_message="CHD file is Intact",
            failure_message="CHD verification failed",
            cancel_event=cancel_event,
            judge_output=_judge_verify_output,
        )


    def _parse_progress(self, line: str) -> int | None:
        """Parse chdman output for progress percentage, None if the line has none.

        None -- not 0 -- for a non-progress line (a banner, a status message).
        The runner reads "a percent was parsed" as proof the tool reports its own
        progress and stands its size-growth fallback down; a 0 sentinel made the
        very first banner line look like a report and disabled the fallback for
        the whole run (issue #263).
        """
        # chdman outputs: "Compressing, 45.2% complete..."
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return None

    def _parse_info(self, output: str) -> dict:
        """Parse chdman info output into structured data."""
        info = {"raw_data": output}
        metadata_lines = []

        # Parse key-value pairs
        for line in output.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                info[key] = value
                if key == "metadata":
                    metadata_lines.append(value)

        if metadata_lines:
            info["metadata_lines"] = metadata_lines

        return info

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible to CHD."""
        ext = Path(filename).suffix.lower()
        return ext in CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_chd_path(
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output CHD path for an input file or stem."""
        input_p = Path(input_path)
        # ``treat_as_stem`` inputs are synthetic flattened archive-member
        # filenames (e.g. "games_disc.cue"); strip the extension like a real
        # source so the CHD name is "games_disc.chd", not "games_disc.cue.chd".
        chd_name = input_p.stem + ".chd"

        if output_dir:
            return str(Path(output_dir) / chd_name)
        return str(input_p.parent / chd_name)

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        input_p = Path(input_path)
        # ``treat_as_stem`` inputs are synthetic flattened archive-member
        # filenames; treat them like real sources and strip the extension so
        # the output base matches the on-disk path (Path.stem also drops the
        # trailing ".chd" for extract modes).
        stem = input_p.stem

        if mode == "copy":
            filename = f"{stem}_copy.chd"
        elif mode in {"createcd", "createdvd", "createraw", "createhd", "createld"}:
            filename = f"{stem}.chd"
        elif mode == "extractcd":
            filename = stem if stem.lower().endswith(".cue") else f"{stem}.cue"
        elif mode == "extractdvd":
            filename = stem if stem.lower().endswith(".iso") else f"{stem}.iso"
        elif mode in {"extractraw", "extracthd"}:
            filename = stem if stem.lower().endswith(".raw") else f"{stem}.raw"
        elif mode == "extractld":
            filename = stem if stem.lower().endswith(".avi") else f"{stem}.avi"
        else:
            filename = f"{stem}.out"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


NO_CHECKSUM_MESSAGE = "This CHD carries no checksum, so it can't be checked"


def _judge_verify_output(lines: list[str]) -> str | None:
    """Why a ``chdman verify`` that exited 0 still failed, or None if it passed.

    chdman's ``do_verify`` (unchanged from MAME 0.146 to master) prints a raw
    or overall SHA1 mismatch to stderr as ``Error: ... SHA1 in header = ...``
    and exits 0, and also exits 0 with ``No verification to be done; ...`` when
    the CHD carries no checksum. So the exit code alone would call a CHD whose
    hunks no longer decode to the data it was made from Intact. It passes only
    when it printed ``Raw SHA1 verification successful!`` and no error line.
    """
    text = [line.strip() for line in lines]
    errors = [
        line for line in text
        if line.lower().startswith("error") or "actual sha1" in line.lower()
    ]
    if errors:
        return "\n".join(errors)
    if any("No verification to be done" in line for line in text):
        # Uncompressed, or no checksum: nothing was read, so not Intact. Said
        # as "can't be checked" so a library file does not look damaged.
        return NO_CHECKSUM_MESSAGE
    if not any("Raw SHA1 verification successful" in line for line in text):
        return "chdman verify did not report a raw SHA1 match"
    return None


chdman_service = ChdmanService()
