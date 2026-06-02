"""Wrapper for the ``nsz`` tool (https://github.com/nicoboss/nsz).

nsz compresses Nintendo Switch ``.nsp``/``.xci`` dumps into ``.nsz``/``.xcz``
and back. Unlike the other tools here, it needs the console's ``prod.keys`` to
decrypt the NCA content before compressing (and to re-encrypt on the way back).

Two things about real nsz (4.6.x) drive this wrapper:

- **Keys are loaded at import time** from ``keys.txt`` next to the binary or
  ``~/.switch/prod.keys``. There is no ``--keys`` flag. So we point the child's
  ``$HOME`` at a throwaway dir whose ``.switch/prod.keys`` is a symlink to the
  operator's configured key file. We ship no keys; the operator mounts their own.
- **Progress is drawn with enlighten**, which goes silent on a non-TTY pipe, so
  there is no parseable percentage text. We estimate progress from the growth of
  the output file, like the z3ds service does.

nsz writes its output into a directory (``-o``) and names the file itself. We
hand it a private temp dir, then move the single result onto the exact
``output_path`` the job expects. That avoids nsz's same-name skip behaviour and
keeps duplicate handling in the job layer where it belongs.
"""
import asyncio
import contextlib
import logging
from logging_setup import get_logger
import os
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.chdman import ConversionCancelled
from services.subprocess_runner import ioprio_prefix, nice_prefix
from services.timeout_policy import compute_progress_stall_timeout
from utils.junk import is_junk_entry

# Input -> output extension, both directions. The four extensions are distinct,
# so this one map covers compress and decompress without ambiguity.
NSZ_COMPRESS_EXTENSIONS = {".nsp", ".xci"}
NSZ_DECOMPRESS_EXTENSIONS = {".nsz", ".xcz"}
NSZ_OUTPUT_FORMATS = {
    ".nsp": ".nsz",
    ".xci": ".xcz",
    ".nsz": ".nsp",
    ".xcz": ".xci",
}

# Rough output:input size ratios, used only to smooth the progress bar.
_COMPRESS_RATIO = 0.6   # compressed output is ~60% of the source
_DECOMPRESS_RATIO = 1.7  # decompressed output is ~1.7x the source

# Key filenames nsz/homebrew tools use.
_KEY_FILENAMES = ("prod.keys", "keys.txt")
# Cap the recursive volume walk so a huge library can't stall startup or a
# request. If keys live deeper than this, set SWITCH_KEYS directly.
_MAX_KEY_SEARCH_DIRS = 5000

logger = get_logger("nsz")


class NszService:
    """Wrapper for the nsz binary."""

    def __init__(self):
        self.nsz_path = settings.nsz_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    # ----- keys -------------------------------------------------------------

    def resolved_keys_file(self) -> str | None:
        """Path to a readable key file, or None.

        ``SWITCH_KEYS`` is the source of truth when set: its directory is checked
        directly and then searched recursively. When unset, check the standard
        nsz/homebrew locations, then recursively search the game volumes and the
        data dir. The recursive walk skips junk dirs and is bounded by
        ``_MAX_KEY_SEARCH_DIRS`` so a huge library can't stall things.
        """
        configured = settings.switch_keys_dir
        if configured:
            return self._first_key_in_dir(configured) or self._recursive_find_keys(
                [configured],
            )
        for candidate in self._standard_key_files():
            if self._readable(candidate):
                return candidate
        return self._recursive_find_keys(self._search_roots())

    def keys_available(self) -> bool:
        return self.resolved_keys_file() is not None

    def key_search_dirs(self) -> list[str]:
        """Locations checked when SWITCH_KEYS is unset (for startup logging)."""
        seen: list[str] = []
        candidates = [os.path.dirname(p) for p in self._standard_key_files()]
        candidates += [f"{root} (recursive)" for root in self._search_roots()]
        for entry in candidates:
            if entry not in seen:
                seen.append(entry)
        return seen

    def log_startup_status(self) -> None:
        """Log once, at startup, whether Switch (nsz) is enabled and where the
        keys were found (or which locations were searched and came up empty)."""
        keys_file = self.resolved_keys_file()
        if keys_file:
            logger.info("Switch (nsz) enabled: prod.keys found at %s", keys_file)
        elif settings.switch_keys_dir:
            logger.warning(
                "Switch (nsz) disabled: SWITCH_KEYS=%s has no prod.keys/keys.txt "
                "(searched recursively)",
                settings.switch_keys_dir,
            )
        else:
            logger.info(
                "Switch (nsz) disabled: no prod.keys found (searched %s). Set "
                "SWITCH_KEYS to the directory holding your prod.keys to enable it.",
                ", ".join(self.key_search_dirs()),
            )

    @staticmethod
    def _readable(path: str) -> bool:
        return os.path.isfile(path) and os.access(path, os.R_OK)

    @staticmethod
    def _standard_key_files() -> list[str]:
        """Standard nsz/homebrew key locations (explicit files, cheap to stat)."""
        home = os.path.expanduser("~")
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return [
            os.path.join(home, ".switch", "prod.keys"),
            os.path.join(xdg, "nsz", "prod.keys"),
            os.path.join(home, ".config", "nsz", "prod.keys"),
        ]

    @staticmethod
    def _search_roots() -> list[str]:
        """Roots walked recursively for keys: the game volumes + the data dir."""
        roots: list[str] = []
        with contextlib.suppress(Exception):  # never let discovery break a job
            roots.extend(settings.volumes)
        with contextlib.suppress(Exception):
            roots.append(str(settings.data_dir))
        return roots

    @classmethod
    def _first_key_in_dir(cls, directory: str) -> str | None:
        for name in _KEY_FILENAMES:
            candidate = os.path.join(directory, name)
            if cls._readable(candidate):
                return candidate
        return None

    def _recursive_find_keys(self, roots: list[str]) -> str | None:
        """Walk ``roots`` for a key file, skipping junk dirs, bounded by the
        directory cap. Returns the first readable prod.keys/keys.txt found."""
        visited = 0
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune junk dirs in place so os.walk won't descend into them.
                dirnames[:] = [d for d in dirnames if not is_junk_entry(d)]
                visited += 1
                if visited > _MAX_KEY_SEARCH_DIRS:
                    logger.warning(
                        "Switch key search hit the %d-directory cap under %s; set "
                        "SWITCH_KEYS to point directly at your prod.keys.",
                        _MAX_KEY_SEARCH_DIRS, root,
                    )
                    break
                for name in _KEY_FILENAMES:
                    if name in filenames:
                        candidate = os.path.join(dirpath, name)
                        if self._readable(candidate):
                            return candidate
        return None

    @contextlib.contextmanager
    def _keys_home(self):
        """Yield an env dict whose HOME exposes the keys at ~/.switch/prod.keys.

        nsz reads keys at import from ``$HOME/.switch/prod.keys``; we symlink the
        configured key file there in a throwaway HOME so the child finds it no
        matter where the operator mounted it.
        """
        keys_file = self.resolved_keys_file()
        if not keys_file:
            raise RuntimeError(
                "nsz needs prod.keys to (de)compress Switch content. Mount your "
                "own prod.keys and set SWITCH_KEYS to the directory holding it "
                "(or place it at ~/.switch/prod.keys). No keys ship with this app.",
            )
        home_dir = tempfile.mkdtemp(prefix=".nsz-home-")
        try:
            switch_dir = os.path.join(home_dir, ".switch")
            os.makedirs(switch_dir, exist_ok=True)
            dest = os.path.join(switch_dir, "prod.keys")
            src = os.path.abspath(keys_file)
            try:
                os.symlink(src, dest)
            except (OSError, NotImplementedError):
                # Symlinks need a privilege on Windows; the key file is tiny, so
                # just copy it into the throwaway HOME instead.
                shutil.copy2(src, dest)
            yield {**os.environ, "HOME": home_dir}
        finally:
            shutil.rmtree(home_dir, ignore_errors=True)

    # ----- command ----------------------------------------------------------

    def _parse_compression(self, compression: str | None) -> tuple[bool | None, int]:
        """Resolve a per-job ``compression`` string into (block, level).

        Format is ``"<mode>:<level>"`` where mode is ``solid`` or ``block``,
        the same ``codec:level`` shape the UI sends for Dolphin. ``block`` is
        None when unspecified (let nsz pick its per-container default). Level
        falls back to the configured default and is clamped to nsz's 1-22 range.
        """
        level = settings.nsz_compression_level
        block: bool | None = None
        if compression and compression.lower() != "none":
            mode_part, _, level_part = compression.partition(":")
            mode_part = mode_part.strip().lower()
            if mode_part == "block":
                block = True
            elif mode_part == "solid":
                block = False
            if level_part.strip():
                try:
                    level = int(level_part)
                except ValueError:
                    pass
        return block, max(1, min(22, level))

    def _build_command(
        self, input_path: str, work_dir: str, mode: str, compression: str | None = None,
    ) -> list[str]:
        cmd = [self.nsz_path]
        if mode == "nsz_decompress":
            cmd.append("-D")
        else:
            block, level = self._parse_compression(compression)
            cmd += ["-C", "-l", str(level)]
            if block is True:
                cmd.append("-B")
            elif block is False:
                cmd.append("-S")
        cmd += ["-o", work_dir, input_path]

        # Apply nice/ionice via command wrappers, NOT preexec_fn: forking a
        # Python callable in a multithreaded app (this one uses threadpools) can
        # deadlock the child before exec. `nice`/`ionice` are exec-only.
        prefix = nice_prefix("nsz") + ioprio_prefix("nsz")
        return prefix + cmd

    def _track_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.add(pid)

    def _untrack_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.discard(pid)

    def active_pids(self) -> list[int]:
        with self._pid_lock:
            return list(self._active_pids)

    # ----- output paths -----------------------------------------------------

    def get_output_path(self, input_path: str, output_dir: str | None = None) -> str:
        input_file = Path(input_path)
        ext = input_file.suffix.lower()
        if ext not in NSZ_OUTPUT_FORMATS:
            raise ValueError(f"Unsupported file extension: {ext}")
        output_name = input_file.stem + NSZ_OUTPUT_FORMATS[ext]
        if output_dir:
            return str(Path(output_dir) / output_name)
        return str(input_file.parent / output_name)

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Output path for an nsz mode. Both modes map purely on the input
        extension via ``NSZ_OUTPUT_FORMATS``.

        ``treat_as_stem`` is accepted for interface parity with z3ds and needs
        no separate branch: archive members arrive as flattened filenames that
        preserve their original extension (see
        ``ArchiveService._output_name_for_member``), so the suffix lookup maps
        them the same way as an on-disk file (.nsp -> .nsz, .xci -> .xcz, and
        back). Every accepted input extension is a known key, so an unknown
        extension is a genuine error rather than a fallback case."""
        input_p = Path(input_path)
        ext = input_p.suffix.lower()
        output_ext = NSZ_OUTPUT_FORMATS.get(ext)
        if output_ext is None:
            raise ValueError(f"Unsupported file extension: {ext}")
        filename = f"{input_p.stem}{output_ext}"
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    def _produced_name(self, input_path: str) -> str:
        p = Path(input_path)
        return f"{p.stem}{NSZ_OUTPUT_FORMATS[p.suffix.lower()]}"

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "nsz_compress",
        *,
        compression: str | None = None,  # per-job "<solid|block>:<level>"
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        verb = "decompression" if mode == "nsz_decompress" else "compression"
        out_dir = os.path.dirname(output_path) or "."
        await asyncio.to_thread(os.makedirs, out_dir, exist_ok=True)

        # Private temp dir on the same filesystem as the destination, so moving
        # the finished file onto output_path is a cheap rename.
        work_dir = await asyncio.to_thread(
            tempfile.mkdtemp, prefix=".nsz-", dir=out_dir,
        )
        produced_path = os.path.join(work_dir, self._produced_name(input_path))

        try:
            with self._keys_home() as env:
                async for update in self._run_convert(
                    input_path, produced_path, work_dir, mode, verb, env,
                    cancel_event, compression,
                ):
                    yield update

            await asyncio.to_thread(os.replace, produced_path, output_path)
            yield {"progress": 100, "message": f"Switch {verb} complete"}
        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

    async def _run_convert(self, input_path, produced_path, work_dir, mode, verb,
                           env, cancel_event, compression=None) -> AsyncGenerator[dict, None]:
        cmd = self._build_command(input_path, work_dir, mode, compression)

        # cmd is built from validated settings paths (no shell interpretation);
        # args are a fixed list, never shell-expanded. nice/ionice are applied
        # as command wrappers in _build_command, so there's no preexec_fn.
        process = await asyncio.create_subprocess_exec(  # nosemgrep
            cmd[0], *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        if process.stdout is None:
            raise RuntimeError("nsz stdout is not available")

        self._track_pid(process.pid)
        if logger.isEnabledFor(logging.DEBUG):
            # Log the argv only; never the keys path contents.
            logger.debug("Starting nsz pid=%s cmd=%s", process.pid, " ".join(cmd))

        stall_timeout = compute_progress_stall_timeout(
            input_path=input_path,
            base_timeout=getattr(settings, "progress_timeout", 0),
            timeout_per_gib=getattr(settings, "progress_timeout_per_gib", 0),
            timeout_cap=getattr(settings, "progress_timeout_cap", 0),
        )
        try:
            input_size = os.path.getsize(input_path)
        except OSError:
            input_size = 0
        ratio = _DECOMPRESS_RATIO if mode == "nsz_decompress" else _COMPRESS_RATIO
        expected_size = max(1, int(input_size * ratio))

        last_output_size: int | None = None
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

        output_tail: list[str] = []

        try:
            yield {"progress": 1, "message": f"Starting Switch {verb}..."}

            while True:
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(256), timeout=2.0)
                except asyncio.TimeoutError:
                    chunk = None

                if chunk == b"":
                    break

                if chunk:
                    text = chunk.decode("utf-8", errors="replace").strip()
                    if text:
                        output_tail.append(text)
                        if len(output_tail) > 30:
                            output_tail.pop(0)
                    last_activity_at = time.monotonic()

                # Estimate progress from the growing output file.
                if os.path.exists(produced_path):
                    try:
                        current = os.path.getsize(produced_path)
                    except OSError:
                        current = None
                    if current is not None and current != last_output_size:
                        last_output_size = current
                        last_activity_at = time.monotonic()
                        pct = min(95, max(1, int(current / expected_size * 90) + 5))
                        yield {
                            "progress": pct,
                            "message": f"Working... ({current // (1024 * 1024)} MB)",
                        }

                if process.returncode is not None:
                    break

                if stall_timeout > 0 and (time.monotonic() - last_activity_at) > stall_timeout:
                    logger.warning(
                        "nsz pid=%s stalled (no progress for %ds), killing",
                        process.pid, stall_timeout,
                    )
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    raise TimeoutError(
                        f"Conversion stalled (no progress for {stall_timeout}s)",
                    )

            await process.wait()
        finally:
            if cancel_task:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            self._untrack_pid(process.pid)

        if cancelled_by_request:
            raise ConversionCancelled("Conversion cancelled by user")

        if process.returncode != 0:
            tail = "\n".join(output_tail[-10:]) if output_tail else "Unknown error"
            raise RuntimeError(f"nsz failed with exit code {process.returncode}: {tail}")

        if not os.path.exists(produced_path):
            tail = "\n".join(output_tail[-10:]) if output_tail else ""
            raise RuntimeError(f"nsz produced no output file. {tail}".strip())

    # ----- info -------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Filesystem-level info (size, format label, compression state). nsz
        exposes no offline metadata dump. Synchronous; wrap callers in a
        threadpool."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()

        is_compressed = ext in NSZ_DECOMPRESS_EXTENSIONS
        base_format = {
            ".nsp": "NSP (Nintendo Submission Package)",
            ".nsz": "NSZ (compressed NSP)",
            ".xci": "XCI (Cartridge Image)",
            ".xcz": "XCZ (compressed XCI)",
        }.get(ext)

        size_mb = file_size / (1024 * 1024)
        size_display = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": base_format,
            "extension": ext,
            "compressed": is_compressed,
            "compression_type": "NSZ (zstandard)" if is_compressed else None,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in NSZ_COMPRESS_EXTENSIONS or ext in NSZ_DECOMPRESS_EXTENSIONS

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
        """Verify a compressed Switch file by running ``nsz -V`` on it.

        NSZ is an NCZ block container, not a raw zstd stream, so a generic zstd
        test can't validate it. nsz's own verify re-derives and checks the
        content hashes, which is why this also needs keys.
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
        if ext not in NSZ_DECOMPRESS_EXTENSIONS:
            yield {"type": "error", "valid": False, "message": f"Invalid extension: {ext}"}
            return
        if not self.keys_available():
            yield {
                "type": "error",
                "valid": False,
                "message": (
                    "nsz needs prod.keys to verify Switch content. Set "
                    "SWITCH_KEYS to a directory with prod.keys, or place it at "
                    "~/.switch/prod.keys."
                ),
            }
            return

        try:
            with self._keys_home() as env:
                process = await asyncio.create_subprocess_exec(  # nosemgrep
                    self.nsz_path, "-V", file_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                self._track_pid(process.pid)
                try:
                    yield {"type": "progress", "progress": 0, "message": "Verifying integrity..."}
                    stdout, _ = await process.communicate()
                    output = (stdout or b"").decode("utf-8", errors="replace").strip()
                    if process.returncode == 0:
                        yield {
                            "type": "progress",
                            "progress": 100,
                            "message": "Integrity check passed",
                        }
                        yield {
                            "type": "complete",
                            "valid": True,
                            "message": "File verified successfully",
                        }
                    else:
                        tail = (
                            "\n".join(output.splitlines()[-5:])
                            if output else "verification failed"
                        )
                        yield {
                            "type": "error",
                            "valid": False,
                            "message": f"Integrity check failed: {tail}",
                        }
                finally:
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        with contextlib.suppress(Exception):
                            await process.wait()
                    self._untrack_pid(process.pid)
        except Exception as e:
            logger.exception("Error during Switch verification: %s", e)
            yield {"type": "error", "valid": False, "message": f"Verification error: {e}"}


# Global service instance
nsz_service = NszService()
