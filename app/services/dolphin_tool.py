import asyncio
import re
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

from logging_setup import get_logger
from services.subprocess_runner import (
    RunnerService,
    StorageAbandoned,
    SubprocessRunner,
    abandonment_checkpoint,
    info_timeout,
    resolve_verify_timeout,
)

from config import settings

DOLPHIN_CONVERTIBLE_EXTENSIONS = {".iso", ".gcz", ".wia", ".rvz", ".wbfs"}

DOLPHIN_OUTPUT_FORMATS = {
    "dolphin_rvz": ("rvz", ".rvz"),
    "dolphin_wia": ("wia", ".wia"),
    "dolphin_gcz": ("gcz", ".gcz"),
    "dolphin_iso": ("iso", ".iso"),
}
DEFAULT_DOLPHIN_COMPRESSION_LEVEL = "19"

logger = get_logger("dolphin_tool")


class DolphinToolService(RunnerService):
    """Wrapper for dolphin-tool binary."""

    verify_fallback_message = "Disc verification failed"

    def __init__(self):
        self.dolphin_tool_path = settings.dolphin_tool_path
        self._runner = SubprocessRunner(owner="dolphin_tool")

    def _build_convert_command(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        compression: str | None = None,
        block_size: int | None = None,
    ) -> list[str]:
        fmt_name, _ = DOLPHIN_OUTPUT_FORMATS.get(mode, ("rvz", ".rvz"))

        cmd = [
            self.dolphin_tool_path,
            "convert",
            "-i", input_path,
            "-o", output_path,
            "-f", fmt_name,
        ]

        # The mode's block size (ModeSpec.block_size, from the plugin's
        # per-container table); dolphin-tool refuses GCZ, RVZ and WIA without.
        if block_size:
            cmd.extend(["-b", str(block_size)])

        if compression and fmt_name in ("rvz", "wia"):
            if "," in compression:
                raise ValueError(
                    "dolphin-tool supports a single compression codec at a time",
                )
            codec = compression
            level = None
            if ":" in compression:
                codec, level = compression.split(":", 1)
            if codec == "none":
                level = None
            elif level is None:
                level = DEFAULT_DOLPHIN_COMPRESSION_LEVEL
            cmd.extend(["-c", codec])
            if level:
                cmd.extend(["-l", level])

        return cmd

    def _wrap_with_stdbuf(self, cmd: list[str]) -> list[str]:
        """Wrap command with stdbuf if available to reduce stdout buffering."""
        stdbuf = shutil.which("stdbuf")
        if not stdbuf:
            return cmd
        try:
            idx = cmd.index(self.dolphin_tool_path)
        except ValueError:
            return [stdbuf, "-oL", "-eL"] + cmd
        return cmd[:idx] + [stdbuf, "-oL", "-eL"] + cmd[idx:]

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "dolphin_rvz",
        *,
        compression: str | None = None,
        block_size: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run dolphin-tool conversion and yield progress updates."""
        cmd = self._build_convert_command(
            mode, input_path, output_path,
            compression=compression, block_size=block_size,
        )
        cmd = self._wrap_with_stdbuf(cmd)
        async for update in self._runner.run(
            cmd,
            input_path=input_path,
            output_path=output_path,
            parse_progress=self._parse_progress,
            cancel_event=cancel_event,
            heartbeat=True,
            fail_label="dolphin-tool",
            mode=mode,
        ):
            yield update

    async def header(self, path: str) -> dict:
        """Get header information about a disc image.

        Goes through the shared capture rather than a hand-rolled spawn, for the
        same reasons as ``chdman.info``: PID tracking, a bounded teardown in
        place of an unbounded ``wait()`` after ``kill()``, the tool priority
        policy, and an unkillable child reported into any open
        ``collect_abandonment()`` sink (issue #268).
        """
        timeout = info_timeout()
        with abandonment_checkpoint() as abandoned:
            returncode, stdout, stderr = await self._runner.run_capture(
                [self.dolphin_tool_path, "header", "-i", path],
                timeout=timeout or None,
            )
        # An ordinary timeout leaves the child dead; this one does not, and the
        # caller's answer differs (retry vs. tell the client the storage is not
        # answering). Folding both into one RuntimeError makes it a generic 500
        # and invites a refresh that strands another child (issue #268).
        if abandoned:
            raise StorageAbandoned(
                f"dolphin-tool header on the file left {', '.join(abandoned)} stuck on "
                "unresponsive storage; it is still running"
            )
        if returncode is None:
            raise RuntimeError(f"dolphin-tool header timed out after {timeout}s")

        if returncode != 0:
            raise RuntimeError(
                stderr.decode()
                or f"dolphin-tool header failed with code {returncode}",
            )

        info = self._parse_header(stdout.decode())
        # A junk image can make dolphin-tool exit 0 and print nothing. No
        # parsed field means it never read the image, not that the image has
        # no header (issue #495).
        if not info.keys() - {"raw_data"}:
            raise RuntimeError("dolphin-tool could not read a header from this file")
        return info

    async def disc_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[str]:
        """Return the disc image's content SHA1(s) via ``dolphin-tool verify``.

        ``dolphin-tool verify -i <path> --algorithm sha1`` reconstructs the
        full disc image and prints its SHA1 (the hash redump DATs index for
        GameCube/Wii discs). For the compressed/container formats this is the
        only hash that can match a DAT, file-level SHA1 of an ``.rvz``/``.wia``
        is meaningless against redump.

        Best-effort: returns the 40-char hex tokens parsed from the tool's
        output, or an empty list on any failure / unsupported input. When
        ``cancel_event`` fires (a background scan/match job was cancelled) the
        verify subprocess is terminated promptly and an empty list returned,
        rather than blocking until the disc finishes reconstructing. The
        cancel/timeout/terminate handling lives in the shared
        ``SubprocessRunner.run_capture``.
        """
        stdout = await self._verify_sha1_stdout(path, cancel_event=cancel_event)
        if stdout is None:
            return []
        # The hash line is formatted as "SHA-1: <hex>" / "<hex>"; pull every
        # standalone 40-char hex token so format tweaks don't break matching.
        return [m.lower() for m in re.findall(r"\b[0-9a-fA-F]{40}\b", stdout)]

    async def disc_sha1(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        """The disc SHA1 for the Round-trip check, parsed strictly.

        Same run as :meth:`disc_hashes`, but the delete gate accepts only the
        exact shape ``--algorithm sha1`` prints: one bare 40-hex line and
        nothing else. Anything looser returns ``None`` (not proven).
        """
        stdout = await self._verify_sha1_stdout(path, cancel_event=cancel_event)
        if stdout is None:
            return None
        match = re.fullmatch(r"\s*([0-9a-fA-F]{40})\s*", stdout)
        return match.group(1).lower() if match else None

    async def _verify_sha1_stdout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        """Stdout of ``dolphin-tool verify --algorithm sha1``, or None on failure."""
        cmd = [
            self.dolphin_tool_path, "verify", "-i", path, "--algorithm", "sha1",
        ]
        # Same size-scaled bound as verify(): this *is* a verify run, just one
        # whose output we read for a hash instead of a verdict.
        #
        # Checked before spawning, not after: sizing the file is a read of the
        # same storage this verify is about to reconstruct, and when that probe
        # has to be abandoned the resolver quietly falls back to the flat
        # baseline. Spawning anyway would buy a full verify timeout against a
        # mount already proven unresponsive, and likely a second written-off
        # resource -- the design's "re-check the cancel after resolving the
        # bound, before spawning" rule, applied to abandonment (issue #268).
        # The checkpoint still forwards to the caller's sink, so a scan or batch
        # walking a list stops too.
        with abandonment_checkpoint() as probe_abandoned:
            timeout = await resolve_verify_timeout(
                path, cancel_event=cancel_event,
            )
        if probe_abandoned:
            logger.warning(
                "dolphin-tool verify (hash) not started for %s: sizing it "
                "abandoned %s, so the storage is not answering",
                path, ", ".join(probe_abandoned),
            )
            return None
        returncode, stdout, _ = await self._runner.run_capture(
            cmd, timeout=timeout or None, cancel_event=cancel_event,
        )
        if returncode is None:
            logger.warning(
                "dolphin-tool verify (hash) aborted (cancel/timeout) for %s", path,
            )
            return None
        if returncode != 0:
            return None
        return stdout.decode("utf-8", "replace")

    def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream disc image verification progress.

        The loop is the shared :meth:`SubprocessRunner.run_verify`, so this
        verify is bounded and cancellable on the same terms as every other
        tool's; dolphin contributes the stdbuf wrap (its progress bar is
        block-buffered on a pipe) and its progress parser.
        """
        return self._runner.run_verify(
            self._wrap_with_stdbuf([self.dolphin_tool_path, "verify", "-i", path]),
            path=path,
            parse_progress=self._parse_progress,
            success_message="Disc image is Intact",
            failure_message="Disc verification failed",
            cancel_event=cancel_event,
        )


    def _parse_progress(self, line: str) -> int | None:
        """Parse dolphin-tool output for progress percentage."""
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return None

    def _parse_header(self, output: str) -> dict:
        """Parse dolphin-tool header output into structured data."""
        info = {"raw_data": output}
        for line in output.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                info[key] = value
        return info

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible by dolphin-tool."""
        ext = Path(filename).suffix.lower()
        return ext in DOLPHIN_CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output path for a dolphin-tool conversion."""
        input_p = Path(input_path)
        # ``treat_as_stem`` inputs are synthetic flattened archive-member
        # filenames (e.g. "games_disc.iso"); strip the extension like a real
        # source so the output is "games_disc.rvz", not "games_disc.iso.rvz".
        stem = input_p.stem
        _, ext = DOLPHIN_OUTPUT_FORMATS.get(mode, ("rvz", ".rvz"))
        filename = f"{stem}{ext}"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


dolphin_tool_service = DolphinToolService()
