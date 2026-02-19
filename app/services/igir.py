"""Service wrapper for the igir ROM collection manager."""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from fastapi.concurrency import run_in_threadpool
from models import (
    DatDirectoryListing,
    DatFileEntry,
    IgirCommand,
    IgirJobCreateRequest,
    IgirValidationResult,
)
from services.chdman import ConversionCancelled

logger = logging.getLogger("chd.igir")

# igir phase symbols used in its terminal output
_PHASE_PATTERNS = {
    "scanning": re.compile(r"Scanning\b", re.IGNORECASE),
    "hashing": re.compile(r"Hashing\b|Checksum", re.IGNORECASE),
    "matching": re.compile(r"Matching\b|Finding\b", re.IGNORECASE),
    "writing": re.compile(
        r"\bWrit(?:ing|e)\b|\bCop(?:y|ying)\b|\bMov(?:ing|e)\b|"
        r"\b(?:hard|sym|re)?link(?:ing)?\b|\bExtract(?:ing)?\b|(?<!\.)\bZip(?:ping)?\b",
        re.IGNORECASE,
    ),
    "testing": re.compile(r"Test(?:ing)?\b|Verif(?:y|ying)\b", re.IGNORECASE),
    "cleaning": re.compile(r"Clean(?:ing)?\b", re.IGNORECASE),
    "reporting": re.compile(r"Report(?:ing)?\b|Fixdat\b|dir2dat\b", re.IGNORECASE),
    "done": re.compile(r"\bDone\b|\bFinish(?:ed|ing)?\b|\bComplete(?:d)?\b", re.IGNORECASE),
}

# Progress weight per phase for estimating overall progress
_PHASE_WEIGHTS = {
    "scanning": (0, 10),
    "hashing": (10, 20),
    "matching": (20, 30),
    "writing": (30, 80),
    "testing": (80, 90),
    "cleaning": (90, 95),
    "reporting": (95, 99),
    "done": (100, 100),
}

# File count patterns (e.g., "42/100", "(42 of 100)", "Processing 42/100 files")
_FILE_COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_PERCENTAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# Scanning files-found pattern (e.g., "Found 123 files")
_FILES_FOUND_RE = re.compile(r"[Ff]ound\s+(\d+)\s+file", re.IGNORECASE)

# Report/fixdat/dir2dat output pattern
_REPORT_LINE_RE = re.compile(
    r"(?:report|fixdat|dir2dat|\.csv|wrote\s+\S+\.(?:dat|csv|m3u))",
    re.IGNORECASE,
)

# Clean dry-run output pattern (files that *would* be deleted)
_CLEAN_DRY_RE = re.compile(
    r"(?:would\s+(?:delete|clean|remove))|(?:clean.*dry.*run)",
    re.IGNORECASE,
)

# Write commands (only one allowed per job)
_WRITE_COMMANDS = {IgirCommand.COPY, IgirCommand.MOVE, IgirCommand.LINK}

# Archive commands (require a write command)
_ARCHIVE_COMMANDS = {IgirCommand.EXTRACT, IgirCommand.ZIP}


class IgirProcessError(RuntimeError):
    """Raised when igir exits with a non-zero return code."""

    def __init__(self, message: str, output_log: list[str] | None = None):
        super().__init__(message)
        self.output_log = output_log or []


class IgirService:
    """Wrapper for the igir ROM collection manager binary."""

    def __init__(self):
        self.igir_path = settings.igir_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()
        self._version_cache: str | None = None

    def _track_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.add(pid)

    def _untrack_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.discard(pid)

    def active_pids(self) -> list[int]:
        with self._pid_lock:
            return list(self._active_pids)

    def _build_command(self, request: IgirJobCreateRequest) -> list[str]:
        """Build the igir CLI command from a job request.

        Returns a list of strings suitable for create_subprocess_exec.
        """
        cmd: list[str] = [self.igir_path]

        def _append_many(flag: str, values: list[str] | None) -> None:
            if not values:
                return
            for value in values:
                item = str(value).strip()
                if item:
                    cmd.extend([flag, item])

        def _append_csv(flag: str, values: list[str] | None) -> None:
            if not values:
                return
            cleaned = [str(v).strip() for v in values if str(v).strip()]
            if cleaned:
                cmd.extend([flag, ",".join(cleaned)])

        # Commands
        for command in request.commands:
            cmd.append(command.value)

        # Input sources
        _append_many("--input", request.input_paths)
        _append_many("--input-exclude", request.input_exclude)
        _append_many("--dat", request.dat_paths)
        _append_many("--dat-exclude", request.dat_exclude)
        _append_many("--patch", request.patch)
        _append_many("--patch-exclude", request.patch_exclude)
        if request.output_path:
            cmd.extend(["--output", request.output_path])

        # ROM input options
        if request.input_checksum_quick:
            cmd.append("--input-checksum-quick")
        if request.input_checksum_min:
            cmd.extend(["--input-checksum-min", request.input_checksum_min])
        if request.input_checksum_max:
            cmd.extend(["--input-checksum-max", request.input_checksum_max])
        if request.input_checksum_archives:
            cmd.extend(["--input-checksum-archives", request.input_checksum_archives])

        # DAT options
        if request.dat_name_regex:
            cmd.extend(["--dat-name-regex", request.dat_name_regex])
        if request.dat_name_regex_exclude:
            cmd.extend(["--dat-name-regex-exclude", request.dat_name_regex_exclude])
        if request.dat_description_regex:
            cmd.extend(["--dat-description-regex", request.dat_description_regex])
        if request.dat_description_regex_exclude:
            cmd.extend(
                ["--dat-description-regex-exclude", request.dat_description_regex_exclude],
            )
        if request.dat_combine:
            cmd.append("--dat-combine")
        if request.dat_ignore_parent_clone:
            cmd.append("--dat-ignore-parent-clone")

        # Output path organization
        if request.dir_mirror:
            cmd.append("--dir-mirror")
        if request.dir_dat_mirror:
            cmd.append("--dir-dat-mirror")
        if request.dir_dat_name:
            cmd.append("--dir-dat-name")
        if request.dir_dat_description:
            cmd.append("--dir-dat-description")
        if request.dir_letter:
            cmd.append("--dir-letter")
        if request.dir_letter_count is not None:
            cmd.extend(["--dir-letter-count", str(request.dir_letter_count)])
        if request.dir_letter_limit is not None:
            cmd.extend(["--dir-letter-limit", str(request.dir_letter_limit)])
        if request.dir_letter_group:
            cmd.append("--dir-letter-group")
        if request.dir_game_subdir:
            cmd.extend(["--dir-game-subdir", request.dir_game_subdir])

        # Writing behavior
        if request.fix_extension:
            cmd.extend(["--fix-extension", request.fix_extension])
        if request.overwrite:
            cmd.append("--overwrite")
        if request.overwrite_invalid:
            cmd.append("--overwrite-invalid")
        if request.move_delete_dirs:
            cmd.extend(["--move-delete-dirs", request.move_delete_dirs])

        # Zip options
        if request.zip_format:
            cmd.extend(["--zip-format", request.zip_format])
        if request.zip_exclude:
            cmd.extend(["--zip-exclude", request.zip_exclude])
        if request.zip_dat_name:
            cmd.append("--zip-dat-name")

        # Link options
        normalized_link_mode = None
        force_relative_symlink = False
        if request.link_mode:
            normalized_link_mode = (
                request.link_mode.value
                if hasattr(request.link_mode, "value")
                else str(request.link_mode)
            )
        elif request.symlink:
            normalized_link_mode = "symlink"
        if normalized_link_mode:
            if normalized_link_mode == "relative":
                force_relative_symlink = True
            legacy_map = {
                "hard": "hardlink",
                "symbolic": "symlink",
                "relative": "symlink",
            }
            normalized_link_mode = legacy_map.get(
                normalized_link_mode, normalized_link_mode,
            )
            cmd.extend(["--link-mode", normalized_link_mode])
        if request.symlink_relative or force_relative_symlink:
            cmd.append("--symlink-relative")

        # Header / trim options
        if request.header:
            cmd.extend(["--header", request.header])
        if request.remove_headers:
            cmd.extend(["--remove-headers", request.remove_headers])
        if request.trimmed_glob:
            cmd.extend(["--trimmed-glob", request.trimmed_glob])
        if request.trim_scan_archives:
            cmd.append("--trim-scan-archives")

        # ROM set options
        if request.merge_roms:
            cmd.extend(["--merge-roms", request.merge_roms])
        if request.merge_discs:
            cmd.append("--merge-discs")
        if request.exclude_disks:
            cmd.append("--exclude-disks")
        if request.allow_excess_sets:
            cmd.append("--allow-excess-sets")
        if request.allow_incomplete_sets:
            cmd.append("--allow-incomplete-sets")

        # Filtering flags
        _bool_flags = [
            ("no_bios", "--no-bios"),
            ("only_bios", "--only-bios"),
            ("no_device", "--no-device"),
            ("only_device", "--only-device"),
            ("no_unlicensed", "--no-unlicensed"),
            ("only_unlicensed", "--only-unlicensed"),
            ("only_retail", "--only-retail"),
            ("no_debug", "--no-debug"),
            ("only_debug", "--only-debug"),
            ("no_demo", "--no-demo"),
            ("only_demo", "--only-demo"),
            ("no_beta", "--no-beta"),
            ("only_beta", "--only-beta"),
            ("no_sample", "--no-sample"),
            ("only_sample", "--only-sample"),
            ("no_prototype", "--no-prototype"),
            ("only_prototype", "--only-prototype"),
            ("no_program", "--no-program"),
            ("only_program", "--only-program"),
            ("no_aftermarket", "--no-aftermarket"),
            ("only_aftermarket", "--only-aftermarket"),
            ("no_homebrew", "--no-homebrew"),
            ("only_homebrew", "--only-homebrew"),
            ("no_unverified", "--no-unverified"),
            ("only_unverified", "--only-unverified"),
            ("no_bad", "--no-bad"),
            ("only_bad", "--only-bad"),
        ]
        for attr, flag in _bool_flags:
            if getattr(request, attr, False):
                cmd.append(flag)

        # Filtering value flags
        if request.filter_regex:
            cmd.extend(["--filter-regex", request.filter_regex])
        if request.filter_regex_exclude:
            cmd.extend(["--filter-regex-exclude", request.filter_regex_exclude])
        if request.filter_category_regex:
            cmd.extend(["--filter-category-regex", request.filter_category_regex])
        _append_csv("--filter-language", request.filter_language)
        _append_csv("--filter-region", request.filter_region)

        # 1G1R
        if request.single:
            cmd.append("--single")
        if request.prefer_game_regex:
            cmd.extend(["--prefer-game-regex", request.prefer_game_regex])
        if request.prefer_rom_regex:
            cmd.extend(["--prefer-rom-regex", request.prefer_rom_regex])
        if request.prefer_verified:
            cmd.append("--prefer-verified")
        if request.prefer_good:
            cmd.append("--prefer-good")
        _append_csv("--prefer-language", request.prefer_language)
        _append_csv("--prefer-region", request.prefer_region)
        if request.prefer_revision:
            cmd.extend(["--prefer-revision", request.prefer_revision])
        if request.prefer_retail:
            cmd.append("--prefer-retail")
        if request.prefer_parent:
            cmd.append("--prefer-parent")

        # Command-specific output options
        if request.playlist_extensions:
            cmd.extend(["--playlist-extensions", request.playlist_extensions])
        if request.dir2dat_output:
            cmd.extend(["--dir2dat-output", request.dir2dat_output])
        if request.fixdat_output:
            cmd.extend(["--fixdat-output", request.fixdat_output])
        if request.report_output:
            cmd.extend(["--report-output", request.report_output])

        # Clean options
        _append_many("--clean-exclude", request.clean_exclude)
        if request.clean_backup:
            cmd.extend(["--clean-backup", request.clean_backup])
        if request.clean_dry_run:
            cmd.append("--clean-dry-run")

        # Threading/retry/cache/temp
        if request.dat_threads is not None:
            cmd.extend(["--dat-threads", str(request.dat_threads)])
        if request.reader_threads is not None:
            cmd.extend(["--reader-threads", str(request.reader_threads)])
        if request.writer_threads is not None:
            cmd.extend(["--writer-threads", str(request.writer_threads)])
        if request.write_retry is not None:
            cmd.extend(["--write-retry", str(request.write_retry)])
        if request.disable_cache:
            cmd.append("--disable-cache")
        if request.cache_path:
            cmd.extend(["--cache-path", request.cache_path])
        effective_temp_dir = request.temp_dir or settings.igir_temp_dir
        if effective_temp_dir:
            cmd.extend(["--temp-dir", effective_temp_dir])

        # Verbosity
        if request.verbose > 0:
            cmd.append("-" + "v" * min(request.verbose, 3))

        # Wrap with ionice if configured
        if (
            settings.chdman_ioprio_class is not None
            and settings.chdman_ioprio_level is not None
        ):
            ionice = shutil.which("ionice")
            if ionice:
                cmd = [
                    ionice,
                    "-c", str(settings.chdman_ioprio_class),
                    "-n", str(settings.chdman_ioprio_level),
                ] + cmd

        return cmd

    def build_command_preview(self, request: IgirJobCreateRequest) -> str:
        """Return the full CLI command as a human-readable string."""
        cmd = self._build_command(request)
        # Strip ionice wrapper for display
        try:
            igir_idx = cmd.index(self.igir_path)
            display_cmd = cmd[igir_idx:]
        except ValueError:
            display_cmd = cmd
        return " ".join(display_cmd)

    def _detect_phase(self, line: str) -> str | None:
        """Detect the current igir processing phase from an output line."""
        for phase, pattern in _PHASE_PATTERNS.items():
            if pattern.search(line):
                return phase
        return None

    def _parse_progress_line(
        self,
        line: str,
        current_phase: str,
    ) -> dict:
        """Parse an igir output line into a progress update dict."""
        result: dict = {"message": line.strip()}

        # Detect phase change
        detected_phase = self._detect_phase(line)
        if detected_phase:
            current_phase = detected_phase
        result["phase"] = current_phase

        # Extract file counts (e.g., "42/100")
        count_match = _FILE_COUNT_RE.search(line)
        if count_match:
            processed = int(count_match.group(1))
            total = int(count_match.group(2))
            result["files_processed"] = processed
            result["files_total"] = total
            # Calculate progress within the current phase
            if total > 0:
                phase_start, phase_end = _PHASE_WEIGHTS.get(
                    current_phase, (0, 100),
                )
                phase_range = phase_end - phase_start
                phase_progress = (processed / total) * phase_range
                result["progress"] = min(99, int(phase_start + phase_progress))

        # Extract percentage if present
        pct_match = _PERCENTAGE_RE.search(line)
        if pct_match and "progress" not in result:
            pct = float(pct_match.group(1))
            phase_start, phase_end = _PHASE_WEIGHTS.get(
                current_phase, (0, 100),
            )
            phase_range = phase_end - phase_start
            result["progress"] = min(99, int(phase_start + (pct / 100) * phase_range))

        # If no file count or percentage, use phase-based estimation
        if "progress" not in result:
            phase_start, _ = _PHASE_WEIGHTS.get(current_phase, (0, 100))
            result["progress"] = phase_start

        return result

    async def run(
        self,
        request: IgirJobCreateRequest,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Execute igir and yield progress updates.

        Yields dicts with keys:
            progress (int): 0-100
            message (str): status text
            phase (str): current processing phase
            files_processed (int): files completed so far
            files_total (int): total files to process
        """
        try:
            # Ensure output directory exists
            if request.output_path:
                await run_in_threadpool(
                    os.makedirs, request.output_path, exist_ok=True,
                )

            cmd = self._build_command(request)

            def _preexec():
                if settings.chdman_nice is not None:
                    try:
                        os.nice(settings.chdman_nice)
                    except OSError:
                        pass

            process = await asyncio.create_subprocess_exec(
                cmd[0], *cmd[1:],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=_preexec if os.name == "posix" else None,
            )
            if process.stdout is None:
                raise RuntimeError("igir stdout is not available")

            self._track_pid(process.pid)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Starting igir pid=%s cmd=%s",
                    process.pid,
                    " ".join(cmd),
                )

            # Stall detection: use a generous base timeout since igir scans
            # can take a while for large collections
            stall_timeout = max(
                getattr(settings, "progress_timeout", 0),
                1800,  # Minimum 30 minutes for large collections
            )
            last_activity_at = time.monotonic()

            cancelled_by_request = False
            cancel_task = None
            if cancel_event:

                async def _cancel_watcher():
                    nonlocal cancelled_by_request
                    await cancel_event.wait()
                    cancelled_by_request = True
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                    except ProcessLookupError:
                        pass

                cancel_task = asyncio.create_task(_cancel_watcher())

            yield {
                "progress": 0,
                "message": "Starting igir...",
                "phase": "starting",
                "files_processed": 0,
                "files_total": 0,
            }

            output_lines: list[str] = []
            report_lines: list[str] = []
            clean_dry_run_lines: list[str] = []
            files_found = 0
            buffer = ""
            current_phase = "scanning"
            last_files_processed = 0
            last_files_total = 0

            def _record_line(raw: str) -> None:
                line = raw.strip()
                if not line:
                    return
                if not output_lines or output_lines[-1] != line:
                    output_lines.append(line)
                    if len(output_lines) > 500:
                        output_lines.pop(0)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("igir output: %s", line)

            def _parse_progress_update(stripped: str) -> dict | None:
                nonlocal current_phase, last_files_processed, last_files_total, files_found
                if not stripped:
                    return None
                update = self._parse_progress_line(stripped, current_phase)
                current_phase = update.get("phase", current_phase)
                if "files_processed" in update:
                    last_files_processed = update["files_processed"]
                if "files_total" in update:
                    last_files_total = update["files_total"]

                # Capture files_found from scanning phase
                if current_phase == "scanning":
                    found_match = _FILES_FOUND_RE.search(stripped)
                    if found_match:
                        files_found = int(found_match.group(1))

                # Capture report/fixdat/dir2dat output
                if _REPORT_LINE_RE.search(stripped):
                    report_lines.append(stripped)

                # Capture clean dry-run output
                if _CLEAN_DRY_RE.search(stripped):
                    clean_dry_run_lines.append(stripped)

                return {
                    "progress": update.get("progress", 0),
                    "message": update.get("message", ""),
                    "phase": current_phase,
                    "files_processed": last_files_processed,
                    "files_total": last_files_total,
                }

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            process.stdout.read(4096), timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        chunk = None

                    if chunk == b"":
                        break

                    if chunk:
                        buffer += chunk.decode("utf-8", errors="replace")
                        last_activity_at = time.monotonic()

                        while True:
                            sep_positions = [
                                i for i in (buffer.find("\r"), buffer.find("\n"))
                                if i >= 0
                            ]
                            if not sep_positions:
                                break
                            sep_index = min(sep_positions)
                            line = buffer[:sep_index]
                            _record_line(line)
                            buffer = buffer[sep_index + 1:]
                            last_activity_at = time.monotonic()

                            # Parse progress from this line
                            stripped = line.strip()
                            progress_update = _parse_progress_update(stripped)
                            if progress_update is not None:
                                yield progress_update

                    if process.returncode is not None:
                        break

                    # Stall detection
                    if stall_timeout > 0:
                        elapsed_since_activity = time.monotonic() - last_activity_at
                        if elapsed_since_activity > stall_timeout:
                            logger.warning(
                                "igir pid=%s stalled (no output for %ds), killing",
                                process.pid, int(elapsed_since_activity),
                            )
                            try:
                                process.kill()
                            except ProcessLookupError:
                                pass
                            raise TimeoutError(
                                f"igir stalled (no output for {stall_timeout}s)",
                            )

                await process.wait()

                if buffer.strip():
                    stripped = buffer.strip()
                    _record_line(buffer)
                    progress_update = _parse_progress_update(stripped)
                    if progress_update is not None:
                        yield progress_update

            finally:
                if cancel_task:
                    cancel_task.cancel()
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        pass
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()
                self._untrack_pid(process.pid)

            if cancelled_by_request:
                raise ConversionCancelled("igir job cancelled by user")

            if process.returncode != 0:
                error_msg = (
                    "\n".join(output_lines[-15:])
                    if output_lines
                    else "Unknown error"
                )
                raise IgirProcessError(
                    f"igir failed with exit code {process.returncode}: {error_msg}",
                    output_log=list(output_lines),
                )

            yield {
                "progress": 100,
                "message": "igir completed successfully",
                "phase": "done",
                "files_processed": last_files_processed,
                "files_total": last_files_total,
                "files_found": files_found,
                "report_output": "\n".join(report_lines) if report_lines else None,
                "clean_dry_run_results": clean_dry_run_lines if clean_dry_run_lines else None,
                "output_log": list(output_lines),
            }

        except ConversionCancelled:
            logger.info("igir job cancelled")
            raise
        except Exception as e:
            logger.exception("Error in igir.run: %s", e)
            raise

    def validate_request(self, request: IgirJobCreateRequest) -> IgirValidationResult:
        """Validate an igir job request before execution.

        Returns an IgirValidationResult with errors, warnings, and command preview.
        """
        errors: list[str] = []
        warnings: list[str] = []
        commands = list(request.commands or [])
        command_set = set(commands)
        has_copy_or_move = any(c in {IgirCommand.COPY, IgirCommand.MOVE} for c in commands)

        # Must have at least one command
        if not commands:
            errors.append("At least one command is required")

        # Only one write command
        write_cmds = [c for c in commands if c in _WRITE_COMMANDS]
        if len(write_cmds) > 1:
            errors.append(
                f"Only one write command allowed, got: {', '.join(c.value for c in write_cmds)}",
            )

        # Archive commands require copy or move specifically
        archive_cmds = [c for c in commands if c in _ARCHIVE_COMMANDS]
        if archive_cmds and not has_copy_or_move:
            errors.append(
                f"Archive commands ({', '.join(c.value for c in archive_cmds)}) "
                "require copy or move",
            )

        # Write commands require output_path
        if write_cmds and not request.output_path:
            errors.append("Output path is required for write commands (copy, move, link)")

        # Commands that require DATs
        dat_required_commands = {IgirCommand.CLEAN, IgirCommand.REPORT, IgirCommand.FIXDAT}
        required = [c.value for c in command_set if c in dat_required_commands]
        if required and not request.dat_paths:
            errors.append(
                f"DAT files are required for command(s): {', '.join(sorted(required))}",
            )

        # 1G1R and merge options are DAT-dependent
        has_merge_option = any([
            request.merge_roms,
            request.merge_discs,
            request.exclude_disks,
            request.allow_excess_sets,
            request.allow_incomplete_sets,
        ])
        if request.single and not request.dat_paths:
            errors.append("--single requires DAT files with parent/clone information")
        if has_merge_option and not request.dat_paths:
            errors.append("ROM set merge options require DAT files")

        # Must have at least one input path
        if not request.input_paths:
            errors.append("At least one input path is required")

        # Validate input paths are within configured volumes
        from utils.path_utils import is_within_configured_volumes

        for input_path in request.input_paths:
            if not is_within_configured_volumes(input_path, treat_archives=False):
                errors.append(f"Input path outside configured volumes: {input_path}")

        # Validate output path is within configured volumes
        if request.output_path:
            if not is_within_configured_volumes(
                request.output_path, treat_archives=False,
            ):
                errors.append(
                    f"Output path outside configured volumes: {request.output_path}",
                )

        def _validate_dat_paths(paths: list[str] | None, label: str) -> None:
            if not paths:
                return
            for dat_path in paths:
                dat_ok = self._is_within_dat_path(dat_path)
                vol_ok = is_within_configured_volumes(
                    dat_path, treat_archives=False,
                )
                if not dat_ok and not vol_ok:
                    errors.append(
                        f"{label} path outside allowed directories: {dat_path}",
                    )

        _validate_dat_paths(request.dat_paths, "DAT")
        _validate_dat_paths(request.dat_exclude, "DAT exclude")

        # Validate optional data paths are within configured volumes
        for path_group_name, paths in (
            ("patch", request.patch),
            ("patch exclude", request.patch_exclude),
        ):
            if not paths:
                continue
            for path_value in paths:
                if not is_within_configured_volumes(path_value, treat_archives=False):
                    errors.append(
                        f"{path_group_name} path outside configured volumes: {path_value}",
                    )

        # clean_exclude values are glob patterns, not filesystem paths.
        # Validate only non-empty values below; do not require volume membership.

        def _validate_optional_output_path(path: str | None, label: str) -> None:
            if not path:
                return
            if os.path.isabs(path):
                if not is_within_configured_volumes(path, treat_archives=False):
                    errors.append(f"{label} outside configured volumes: {path}")
            else:
                errors.append(
                    f"{label} must be an absolute path within configured volumes: {path}",
                )

        _validate_optional_output_path(request.clean_backup, "clean backup path")
        _validate_optional_output_path(request.dir2dat_output, "dir2dat output path")
        _validate_optional_output_path(request.fixdat_output, "fixdat output path")
        _validate_optional_output_path(request.report_output, "report output path")
        _validate_optional_output_path(request.temp_dir, "temp dir")
        _validate_optional_output_path(request.cache_path, "cache path")

        # Validate remove_headers
        if request.remove_headers and request.remove_headers.lower() not in (
            "all", "known",
        ):
            warnings.append(
                f"Non-standard remove-headers value: {request.remove_headers} "
                "(typical values are 'all' or 'known')",
            )

        # Warn if patch paths set but no write command
        if request.patch and not write_cmds:
            warnings.append(
                "Patch files have no effect without a write command (copy, move, or link)",
            )

        # Validate list values are not empty
        for label, values in (
            ("input exclude", request.input_exclude),
            ("DAT exclude", request.dat_exclude),
            ("patch exclude", request.patch_exclude),
            ("clean exclude", request.clean_exclude),
        ):
            if values:
                for value in values:
                    if not str(value).strip():
                        errors.append(f"{label} value cannot be empty")

        # Check for conflicting filter flags
        _filter_pairs = [
            ("no_bios", "only_bios"),
            ("no_device", "only_device"),
            ("no_unlicensed", "only_unlicensed"),
            ("no_debug", "only_debug"),
            ("no_demo", "only_demo"),
            ("no_beta", "only_beta"),
            ("no_sample", "only_sample"),
            ("no_prototype", "only_prototype"),
            ("no_program", "only_program"),
            ("no_aftermarket", "only_aftermarket"),
            ("no_homebrew", "only_homebrew"),
            ("no_unverified", "only_unverified"),
            ("no_bad", "only_bad"),
        ]
        for no_flag, only_flag in _filter_pairs:
            if getattr(request, no_flag, False) and getattr(request, only_flag, False):
                errors.append(
                    f"Conflicting filters: --{no_flag.replace('_', '-')} "
                    f"and --{only_flag.replace('_', '-')}",
                )

        # fix_extension validation
        if request.fix_extension and request.fix_extension not in (
            "auto", "always", "never",
        ):
            errors.append(
                f"Invalid fix-extension value: {request.fix_extension} "
                "(must be auto, always, or never)",
            )

        # dir_game_subdir validation
        if request.dir_game_subdir and request.dir_game_subdir not in (
            "never", "multiple", "always",
        ):
            errors.append(
                f"Invalid dir-game-subdir value: {request.dir_game_subdir} "
                "(must be never, multiple, or always)",
            )

        # prefer_revision validation
        if request.prefer_revision and request.prefer_revision not in (
            "older", "newer",
        ):
            errors.append(
                f"Invalid prefer-revision value: {request.prefer_revision} "
                "(must be older or newer)",
            )

        # checksum validation
        checksum_levels = ("CRC32", "MD5", "SHA1", "SHA256")
        level_order = {value: idx for idx, value in enumerate(checksum_levels)}
        if request.input_checksum_min and request.input_checksum_min.upper() not in checksum_levels:
            errors.append(
                f"Invalid input-checksum-min value: {request.input_checksum_min} "
                "(must be CRC32, MD5, SHA1, or SHA256)",
            )
        if request.input_checksum_max and request.input_checksum_max.upper() not in checksum_levels:
            errors.append(
                f"Invalid input-checksum-max value: {request.input_checksum_max} "
                "(must be CRC32, MD5, SHA1, or SHA256)",
            )
        if (
            request.input_checksum_min
            and request.input_checksum_max
            and request.input_checksum_min.upper() in level_order
            and request.input_checksum_max.upper() in level_order
            and level_order[request.input_checksum_min.upper()]
            > level_order[request.input_checksum_max.upper()]
        ):
            errors.append("--input-checksum-min cannot be stronger than --input-checksum-max")
        if request.input_checksum_archives and request.input_checksum_archives not in (
            "never", "auto", "always",
        ):
            errors.append(
                "Invalid input-checksum-archives value (must be never, auto, or always)",
            )

        # Additional enum validations
        if request.move_delete_dirs and request.move_delete_dirs not in ("never", "auto", "always"):
            errors.append("Invalid move-delete-dirs value (must be never, auto, or always)")
        if request.zip_format and request.zip_format not in ("torrentzip", "rvzstd"):
            errors.append("Invalid zip-format value (must be torrentzip or rvzstd)")
        if request.merge_roms and request.merge_roms not in (
            "fullnonmerged", "nonmerged", "split", "merged",
        ):
            errors.append(
                "Invalid merge-roms value (must be fullnonmerged, nonmerged, split, or merged)",
            )
        normalized_link_mode = None
        if request.link_mode:
            link_mode_value = (
                request.link_mode.value
                if hasattr(request.link_mode, "value")
                else str(request.link_mode)
            )
            normalized_link_mode = link_mode_value
            if link_mode_value not in ("hardlink", "symlink", "reflink", "hard", "symbolic", "relative"):
                errors.append("Invalid link-mode value")
        if request.verbose < 0 or request.verbose > 3:
            errors.append("verbose must be between 0 and 3")

        for name, value, allow_zero in (
            ("dat_threads", request.dat_threads, False),
            ("reader_threads", request.reader_threads, False),
            ("writer_threads", request.writer_threads, False),
            ("dir_letter_count", request.dir_letter_count, False),
            ("dir_letter_limit", request.dir_letter_limit, False),
            ("write_retry", request.write_retry, True),
        ):
            if value is None:
                continue
            if (allow_zero and value < 0) or (not allow_zero and value <= 0):
                errors.append(f"{name.replace('_', '-')} must be greater than {'or equal to 0' if allow_zero else '0'}")

        # 1G1R preferences without --single
        has_prefer = any([
            request.prefer_game_regex,
            request.prefer_rom_regex,
            request.prefer_verified,
            request.prefer_good,
            request.prefer_language,
            request.prefer_region,
            request.prefer_revision,
            request.prefer_retail,
            request.prefer_parent,
        ])
        if has_prefer and not request.single:
            warnings.append(
                "1G1R preference flags have no effect without --single",
            )

        # DAT-sensitive options without DAT inputs
        has_dat_filters = any([
            request.dat_name_regex,
            request.dat_name_regex_exclude,
            request.dat_description_regex,
            request.dat_description_regex_exclude,
            request.dat_combine,
            request.dat_ignore_parent_clone,
        ])
        if has_dat_filters and not request.dat_paths:
            warnings.append("DAT filter options are set without --dat paths")

        # Command-specific option checks
        if request.move_delete_dirs and IgirCommand.MOVE not in command_set:
            warnings.append("--move-delete-dirs has no effect without the move command")
        if any([request.zip_format, request.zip_exclude, request.zip_dat_name]) and IgirCommand.ZIP not in command_set:
            warnings.append("Zip options have no effect without the zip command")
        if request.zip_dat_name and request.dat_threads not in (None, 1):
            warnings.append("--zip-dat-name works best with --dat-threads 1")
        if any([request.clean_exclude, request.clean_backup, request.clean_dry_run]) and IgirCommand.CLEAN not in command_set:
            warnings.append("Clean options have no effect without the clean command")
        if request.playlist_extensions and IgirCommand.PLAYLIST not in command_set:
            warnings.append("--playlist-extensions has no effect without the playlist command")
        if request.dir2dat_output and IgirCommand.DIR2DAT not in command_set:
            warnings.append("--dir2dat-output has no effect without the dir2dat command")
        if request.fixdat_output and IgirCommand.FIXDAT not in command_set:
            warnings.append("--fixdat-output has no effect without the fixdat command")
        if request.report_output and IgirCommand.REPORT not in command_set:
            warnings.append("--report-output has no effect without the report command")

        # Link mode checks
        using_link_opts = bool(request.link_mode or request.symlink or request.symlink_relative)
        if using_link_opts and IgirCommand.LINK not in command_set:
            warnings.append("Link options have no effect without the link command")
        if request.symlink_relative:
            uses_symlink_mode = (
                request.symlink
                or normalized_link_mode in ("symlink", "symbolic", "relative")
            )
            if not uses_symlink_mode:
                warnings.append("--symlink-relative requires link mode symlink")

        # dir_letter options without dir_letter
        if (request.dir_letter_count or request.dir_letter_limit or request.dir_letter_group) and not request.dir_letter:
            warnings.append("Letter directory options have no effect without --dir-letter")

        # dir_letter_group requires dir_letter_limit
        if request.dir_letter_group and not request.dir_letter_limit:
            warnings.append("--dir-letter-group requires --dir-letter-limit")

        # Build command preview
        command_preview = self.build_command_preview(request)

        return IgirValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            command_preview=command_preview,
        )

    @staticmethod
    def _is_within_dat_path(path: str) -> bool:
        """Check if a path is within the configured DAT directory."""
        dat_root = settings.igir_dat_path
        if not dat_root:
            return False
        try:
            real_path = os.path.realpath(path)
            real_root = os.path.realpath(dat_root)
            return real_path.startswith(real_root + os.sep) or real_path == real_root
        except (OSError, ValueError):
            return False

    async def list_dats(
        self, subdir: str | None = None,
    ) -> DatDirectoryListing:
        """List DAT files in the configured DAT directory.

        Args:
            subdir: Optional subdirectory within the DAT root to list.

        Returns:
            DatDirectoryListing with files and subdirectories.
        """
        dat_root = settings.igir_dat_path
        if subdir:
            target = os.path.join(dat_root, subdir)
        else:
            target = dat_root

        # Validate path stays within DAT root
        if not self._is_within_dat_path(target):
            raise ValueError(f"Path outside DAT directory: {target}")

        def _scan():
            entries: list[DatFileEntry] = []
            subdirs: list[str] = []

            if not os.path.isdir(target):
                return entries, subdirs

            for item in sorted(os.listdir(target)):
                if item.startswith("."):
                    continue
                item_path = os.path.join(target, item)

                try:
                    if os.path.isdir(item_path):
                        subdirs.append(item)
                    elif os.path.isfile(item_path):
                        ext = Path(item).suffix.lower()
                        if ext in {".dat", ".xml", ".zip", ".7z"}:
                            stat = os.stat(item_path)
                            entries.append(DatFileEntry(
                                name=item,
                                path=item_path,
                                size=stat.st_size,
                                modified=datetime.fromtimestamp(
                                    stat.st_mtime, tz=timezone.utc,
                                ).isoformat(),
                            ))
                except OSError:
                    continue

            return entries, subdirs

        entries, subdirs = await run_in_threadpool(_scan)

        return DatDirectoryListing(
            path=target,
            entries=entries,
            subdirectories=subdirs,
        )

    async def search_dats(self) -> list[DatFileEntry]:
        """Recursively search for all DAT files in the DAT directory."""
        dat_root = settings.igir_dat_path

        def _scan_recursive():
            results: list[DatFileEntry] = []
            if not os.path.isdir(dat_root):
                return results

            for root, _dirs, files in os.walk(dat_root):
                for filename in sorted(files):
                    ext = Path(filename).suffix.lower()
                    if ext in {".dat", ".xml", ".zip", ".7z"}:
                        file_path = os.path.join(root, filename)
                        try:
                            stat = os.stat(file_path)
                            results.append(DatFileEntry(
                                name=filename,
                                path=file_path,
                                size=stat.st_size,
                                modified=datetime.fromtimestamp(
                                    stat.st_mtime, tz=timezone.utc,
                                ).isoformat(),
                            ))
                        except OSError:
                            continue
            return results

        return await run_in_threadpool(_scan_recursive)

    @staticmethod
    def _extract_version(output: str) -> str | None:
        """Extract a semantic version from igir CLI output."""
        if not output:
            return None

        # Prefer versions explicitly attached to "igir" in the output.
        prefixed = re.search(
            r"\bigir(?:\s+|@|[\/:_-])v?(\d+\.\d+\.\d+)\b",
            output,
            re.IGNORECASE,
        )
        if prefixed:
            return prefixed.group(1)

        generic = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
        if generic:
            return generic.group(1)

        return None

    async def get_version(self) -> str:
        """Get the igir version string (cached after first call)."""
        if self._version_cache is not None:
            return self._version_cache

        probe_args = (("--version",), ("version",))
        had_successful_probe = False
        last_error: Exception | None = None

        for args in probe_args:
            try:
                process = await asyncio.create_subprocess_exec(
                    self.igir_path,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=10,
                )
                had_successful_probe = True
                combined_output = "\n".join(
                    part.decode("utf-8", errors="replace").strip()
                    for part in (stdout, stderr)
                    if part
                ).strip()
                version = self._extract_version(combined_output)
                if version:
                    self._version_cache = version
                    return self._version_cache
            except Exception as e:
                last_error = e

        if not had_successful_probe and last_error is not None:
            logger.warning("Failed to get igir version: %s", last_error)
            return "unavailable"

        self._version_cache = "unknown"
        return self._version_cache

    @staticmethod
    def build_options_summary(request: IgirJobCreateRequest) -> str:
        """Build a human-readable summary of the selected igir options."""
        parts: list[str] = []

        # Commands
        parts.append("Commands: " + ", ".join(c.value for c in request.commands))

        # Input count
        parts.append(f"Inputs: {len(request.input_paths)} path(s)")

        # DATs
        if request.dat_paths:
            parts.append(f"DATs: {len(request.dat_paths)} path(s)")

        # 1G1R
        if request.single:
            prefs: list[str] = []
            if request.prefer_language:
                prefs.append(f"lang={','.join(request.prefer_language)}")
            if request.prefer_region:
                prefs.append(f"region={','.join(request.prefer_region)}")
            if request.prefer_revision:
                prefs.append(f"rev={request.prefer_revision}")
            parts.append("1G1R" + (f" ({', '.join(prefs)})" if prefs else ""))

        # Key filters
        active_filters: list[str] = []
        if request.only_retail:
            active_filters.append("only-retail")
        for attr in ("no_bios", "no_demo", "no_beta", "no_prototype", "no_bad"):
            if getattr(request, attr, False):
                active_filters.append(attr.replace("_", "-"))
        if active_filters:
            parts.append("Filters: " + ", ".join(active_filters))

        # Organization
        org_parts: list[str] = []
        if request.dir_dat_name:
            org_parts.append("by DAT name")
        if request.dir_dat_mirror:
            org_parts.append("by DAT mirror")
        if request.dir_letter:
            org_parts.append("by letter")
        if request.dir_mirror:
            org_parts.append("mirror input")
        if org_parts:
            parts.append("Organize: " + ", ".join(org_parts))

        if request.remove_headers:
            parts.append(f"Remove headers: {request.remove_headers}")
        if request.patch:
            parts.append(f"Patches: {len(request.patch)} path(s)")
        if request.link_mode or request.symlink:
            link_mode = (
                request.link_mode.value
                if hasattr(request.link_mode, "value")
                else request.link_mode
            )
            if not link_mode and request.symlink:
                link_mode = "symlink"
            parts.append(f"Link mode: {link_mode}")
        if request.merge_roms:
            parts.append(f"Merge ROMs: {request.merge_roms}")
        if request.clean_dry_run:
            parts.append("Clean: dry-run")
        if request.overwrite or request.overwrite_invalid:
            overwrite = []
            if request.overwrite:
                overwrite.append("overwrite")
            if request.overwrite_invalid:
                overwrite.append("overwrite-invalid")
            parts.append("Writes: " + ", ".join(overwrite))

        return " | ".join(parts)


# Global service instance
igir_service = IgirService()
