"""Wrapper for the ``7z`` CLI, packing handheld ROM dumps into archives.

This tool losslessly compresses Game Boy, Game Boy Color, Game Boy Advance and
Nintendo DS ROM dumps (``.gb`` / ``.gbc`` / ``.gba`` / ``.nds``) into standard
``.7z`` (LZMA2) or ``.zip`` (deflate) archives, and extracts them back to the
exact original ROM. The ``7z`` binary ships in the image already (``p7zip-full``),
so no extra build step is needed; the read/metadata side reuses ``zipfile`` /
``py7zr`` directly.

Three modes:

- ``romz_7z``      ``.gb``/``.gbc``/``.gba``/``.nds`` -> ``<name>.7z``
- ``romz_zip``     ``.gb``/``.gbc``/``.gba``/``.nds`` -> ``<name>.zip``
- ``romz_extract`` ``.7z``/``.zip`` (single ROM member) -> original ROM

Output names preserve the ROM extension (``Game.gba`` -> ``Game.gba.7z``), so the
round-trip is deterministic in both directions: extract simply restores the
single member. Compression strength is a Fast / Default / Max effort preset; Max
mirrors the reference ``-mx=9 -md=256m -mfb=273 -m0=lzma2`` profile.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessRunner,
    ioprio_prefix,
    verify_timeout,
)

try:
    import py7zr

    HAS_7Z = True
except ImportError:  # pragma: no cover - py7zr ships in requirements
    HAS_7Z = False

import zipfile

# SubprocessRunner "owner" for the shared priority/timeout policy. An optional
# COMPRESSATORIUM_ROMZ_* override takes precedence over the tool-neutral
# COMPRESSATORIUM_TOOL_* default (see services/subprocess_runner.py).
_OWNER = "romz"

# Compress takes a loose ROM; extract takes one of the archives this tool writes.
ROMZ_COMPRESS_EXTENSIONS = {".gb", ".gbc", ".gba", ".nds"}
ROMZ_ARCHIVE_EXTENSIONS = {".7z", ".zip"}

# Output container is decided by the mode, not the input ROM. The ROM extension
# is preserved *in front of* the archive suffix (Game.gba -> Game.gba.7z) so the
# extract direction is a pure suffix strip and same-stem ROMs of different
# platforms (Game.gb vs Game.gba) never collide.
ROMZ_OUTPUT_BY_MODE = {
    "romz_7z": ".7z",
    "romz_zip": ".zip",
}
ROMZ_COMPRESS_MODES = frozenset(ROMZ_OUTPUT_BY_MODE)
ROMZ_EXTRACT_MODE = "romz_extract"

_ROM_FORMAT_LABELS = {
    ".gb": "Game Boy ROM",
    ".gbc": "Game Boy Color ROM",
    ".gba": "Game Boy Advance ROM",
    ".nds": "Nintendo DS ROM",
}
_ARCHIVE_FORMAT_LABELS = {
    ".7z": "7-Zip archive",
    ".zip": "ZIP archive",
}
_ARCHIVE_COMPRESSION_LABELS = {
    ".7z": "7-Zip (LZMA2)",
    ".zip": "ZIP (Deflate)",
}

# 7z prints progress as a leading "NN%" token when -bsp1 is set; parse it for the
# streamed progress bar. Output-file growth is the fallback the runner watches.
_PCT_RE = re.compile(r"(\d{1,3})%")

logger = get_logger("romz")


def _parse_progress(line: str) -> int | None:
    match = _PCT_RE.search(line)
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def _compress_flags(mode: str, compression: str | None) -> list[str]:
    """7z switches for an effort preset (fast | default | max)."""
    effort = (compression or "max").strip().lower()
    if mode == "romz_zip":
        level = {"fast": "1", "default": "7", "max": "9"}.get(effort, "9")
        return ["-tzip", f"-mx={level}", "-mmt=on"]
    # romz_7z (LZMA2)
    if effort == "fast":
        return ["-t7z", "-m0=lzma2", "-mx=1", "-mmt=on"]
    if effort == "default":
        return ["-t7z", "-m0=lzma2", "-mx=7", "-md=64m", "-mmt=on"]
    # "max" (and any unknown token) -> the reference best-settings profile.
    return ["-t7z", "-m0=lzma2", "-mx=9", "-md=256m", "-mfb=273", "-mmt=on"]


class RomzService:
    """Wrapper for the 7z binary, packing/unpacking handheld ROM dumps."""

    def __init__(self):
        self.sevenzip_path = settings.sevenzip_path
        self._runner = SubprocessRunner(owner=_OWNER)

    # ----- archive member listing (read side; reuses zipfile / py7zr) -------

    @staticmethod
    def _list_members(archive_path: str) -> list[tuple[str, int]]:
        """Return ``(internal_path, uncompressed_size)`` for every file member."""
        ext = Path(archive_path).suffix.lower()
        members: list[tuple[str, int]] = []
        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    members.append((info.filename, int(info.file_size)))
        elif ext == ".7z":
            if not HAS_7Z:
                raise RuntimeError("py7zr is required to read .7z archives")
            with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
                for entry in zf.list():
                    if entry.is_directory:
                        continue
                    # py7zr >=1.1.0 FileInfo.uncompressed is a required int
                    # (archive.py reads it the same way); `or 0` just guards a
                    # 0-byte/None edge without the redundant getattr default.
                    size = entry.uncompressed or 0
                    members.append((entry.filename, int(size)))
        else:
            raise ValueError(f"Unsupported archive extension: {ext}")
        return members

    @classmethod
    def _single_rom_member(cls, archive_path: str) -> str:
        """Return the sole ROM member of ``archive_path`` or raise a clear error.

        The extract mode is for reverting this tool's own single-ROM archives,
        not for unpacking arbitrary multi-file archives (the file browser already
        handles those), so anything but exactly one handheld-ROM member is a hard
        error rather than a silent partial extract.
        """
        members = cls._list_members(archive_path)
        roms = [
            name for name, _ in members
            if Path(name).suffix.lower() in ROMZ_COMPRESS_EXTENSIONS
        ]
        if len(roms) == 1 and len(members) == 1:
            return roms[0]
        if not roms:
            raise ValueError(
                "Archive holds no Game Boy / GBA / DS ROM to extract",
            )
        raise ValueError(
            "Archive holds more than one file; open it in the file browser "
            "to extract a specific member",
        )

    # ----- command ----------------------------------------------------------

    def _build_command(
        self, input_path: str, output_path: str, mode: str,
        compression: str | None, member: str | None,
    ) -> list[str]:
        if mode in ROMZ_COMPRESS_MODES:
            cmd = [self.sevenzip_path, "a", output_path, input_path]
            cmd += _compress_flags(mode, compression)
            cmd += ["-bsp1", "-y"]
        elif mode == ROMZ_EXTRACT_MODE:
            out_dir = os.path.dirname(output_path) or "."
            cmd = [
                self.sevenzip_path, "e", input_path, member or "",
                f"-o{out_dir}", "-bsp1", "-y",
            ]
        else:
            raise ValueError(f"Unsupported romz mode: {mode}")
        # Apply ionice via a command wrapper (the shared nice level is applied by
        # SubprocessRunner.run's preexec); both honor the COMPRESSATORIUM_ROMZ_*
        # overrides, falling back to the tool-neutral policy.
        return ioprio_prefix(_OWNER) + cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    # ----- output paths -----------------------------------------------------

    @classmethod
    def get_output_path_for_mode(
        cls,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Output path for a romz mode.

        Compress preserves the ROM extension in front of the archive suffix
        (``Game.gba`` -> ``Game.gba.7z``). Extract restores the archived ROM's
        own basename (peeked from the archive, falling back to a suffix strip so
        a caller can still compute a path for an unreadable file).
        ``treat_as_stem`` is accepted for interface parity; romz never takes
        archive members as input.
        """
        input_p = Path(input_path)
        if mode in ROMZ_COMPRESS_MODES:
            filename = f"{input_p.name}{ROMZ_OUTPUT_BY_MODE[mode]}"
        elif mode == ROMZ_EXTRACT_MODE:
            try:
                filename = os.path.basename(cls._single_rom_member(input_path))
            except Exception:
                # Fall back to stripping the archive suffix (matches this tool's
                # own naming convention) when the archive can't be read or is
                # corrupt. Broad by design: zipfile.BadZipFile / py7zr errors
                # don't derive from OSError, and this is a best-effort path
                # computation, not the place to surface a read failure.
                filename = input_p.stem
        else:
            raise ValueError(f"Unsupported romz mode: {mode}")
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "romz_7z",
        *,
        compression: str | None = None,  # effort preset: fast | default | max
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        member: str | None = None
        if mode in ROMZ_COMPRESS_MODES:
            complete_message = "Compression complete"
            # `7z a` APPENDS to an existing archive, so clear any stale/partial
            # output first to avoid silently merging into it. suppress(OSError)
            # covers the already-absent case (FileNotFoundError) without a
            # separate stat on the event loop.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.remove, output_path)
        elif mode == ROMZ_EXTRACT_MODE:
            member = await asyncio.to_thread(self._single_rom_member, input_path)
            complete_message = "Extraction complete"
        else:
            raise ValueError(f"Unsupported romz mode: {mode}")

        cmd = self._build_command(input_path, output_path, mode, compression, member)

        try:
            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=output_path,
                parse_progress=_parse_progress,
                cancel_event=cancel_event,
                fail_label="7z",
                complete_message=complete_message,
            ):
                yield update
        except BaseException:
            # Any failure/cancel can leave a partial archive or ROM on disk; the
            # runner doesn't own output_path, so clean it up here so a retry
            # isn't blocked by (or silently trusts) a truncated file.
            # suppress(OSError) covers the already-absent case.
            with contextlib.suppress(OSError):
                os.remove(output_path)
            raise

    # ----- info -------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Filesystem-level info plus, for archives, the contained ROM + ratio.

        Synchronous; wrap callers in a threadpool.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()
        is_archive = ext in ROMZ_ARCHIVE_EXTENSIONS

        contained_name: str | None = None
        original_size: int | None = None
        ratio: str | None = None
        if is_archive:
            try:
                members = self._list_members(file_path)
            except Exception as exc:  # unreadable/corrupt archive
                logger.debug("romz info: failed to list %s: %s", file_path, exc)
                members = []
            if members:
                contained_name = os.path.basename(members[0][0])
                total = sum(size for _, size in members)
                if total > 0:
                    original_size = total
                    ratio = f"{file_size / total * 100:.1f}%"

        size_mb = file_size / (1024 * 1024)
        size_display = (
            f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
        )

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": (
                _ARCHIVE_FORMAT_LABELS.get(ext)
                if is_archive
                else _ROM_FORMAT_LABELS.get(ext)
            ),
            "extension": ext,
            "compressed": is_archive,
            "compression_type": (
                _ARCHIVE_COMPRESSION_LABELS.get(ext) if is_archive else None
            ),
            "contained_name": contained_name,
            "original_size": original_size,
            "ratio": ratio,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in ROMZ_COMPRESS_EXTENSIONS or ext in ROMZ_ARCHIVE_EXTENSIONS

    # ----- verify -----------------------------------------------------------

    async def verify(self, file_path: str) -> dict:
        final = {"valid": False, "message": "Verification failed"}
        async for update in self.verify_stream(file_path):
            if update.get("type") in ("complete", "error"):
                final = update
        return {
            "valid": bool(final.get("valid", False)),
            "message": final.get("message") or "Verification failed",
        }

    async def verify_stream(self, file_path: str) -> AsyncGenerator[dict, None]:
        """Verify an archive by running ``7z t`` (tests every member's CRC).

        A clean (exit 0) run proves the archive decompresses intact, the analog
        of maxcso ``--crc`` / nsz ``-V`` / z3ds ``zstd -t``.
        """
        if not os.path.exists(file_path):
            yield {"type": "error", "valid": False, "message": "File not found"}
            return
        try:
            is_empty = os.path.getsize(file_path) == 0
        except OSError as e:
            yield {"type": "error", "valid": False, "message": f"Error reading file: {e}"}
            return
        if is_empty:
            yield {"type": "error", "valid": False, "message": "File is empty"}
            return
        ext = Path(file_path).suffix.lower()
        if ext not in ROMZ_ARCHIVE_EXTENSIONS:
            yield {"type": "error", "valid": False, "message": f"Invalid extension: {ext}"}
            return

        yield {"type": "progress", "progress": 0, "message": "Verifying integrity..."}
        # run_capture applies the shared nice/ionice policy and honors the
        # verify timeout (0 disables); a heavy `7z t` is throttled like a convert.
        timeout = verify_timeout(_OWNER)
        returncode, stdout, _ = await self._runner.run_capture(
            [self.sevenzip_path, "t", file_path],
            timeout=timeout or None,
            stderr_to_stdout=True,
        )
        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        if returncode == 0:
            yield {"type": "progress", "progress": 100, "message": "Integrity check passed"}
            yield {"type": "complete", "valid": True, "message": "File verified successfully"}
        elif returncode is None:
            yield {
                "type": "error",
                "valid": False,
                "message": f"Verification timed out after {timeout}s",
            }
        else:
            tail = "\n".join(output.splitlines()[-5:]) if output else "verification failed"
            yield {
                "type": "error",
                "valid": False,
                "message": f"Integrity check failed: {tail}",
            }


# Global service instance
romz_service = RomzService()


# Re-export for callers that catch cancellation from this module.
__all__ = [
    "ROMZ_ARCHIVE_EXTENSIONS",
    "ROMZ_COMPRESS_EXTENSIONS",
    "ROMZ_OUTPUT_BY_MODE",
    "ConversionCancelled",
    "romz_service",
]
