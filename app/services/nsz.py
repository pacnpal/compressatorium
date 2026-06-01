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
import os
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.chdman import ConversionCancelled
from services.timeout_policy import compute_progress_stall_timeout

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

logger = logging.getLogger("chd.nsz")


class NszService:
    """Wrapper for the nsz binary."""

    def __init__(self):
        self.nsz_path = settings.nsz_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    # ----- keys -------------------------------------------------------------

    def resolved_keys_file(self) -> str | None:
        """Path to a readable key file, or None.

        ``SWITCH_KEYS`` is the source of truth when set: it names a directory
        holding ``prod.keys`` (or ``keys.txt``). When unset, best-effort search
        the locations nsz itself uses, so a deployment that mounts keys at
        ``~/.switch`` works without setting anything.
        """
        configured_dir = settings.switch_keys_dir
        if configured_dir:
            for name in ("prod.keys", "keys.txt"):
                candidate = os.path.join(configured_dir, name)
                if self._readable(candidate):
                    return candidate
            return None
        for candidate in self._default_keys_candidates():
            if self._readable(candidate):
                return candidate
        return None

    def keys_available(self) -> bool:
        return self.resolved_keys_file() is not None

    @staticmethod
    def _readable(path: str) -> bool:
        return os.path.isfile(path) and os.access(path, os.R_OK)

    @staticmethod
    def _default_keys_candidates() -> list[str]:
        """A bounded, non-blocking set of likely key locations.

        Standard nsz/homebrew locations first, then a shallow look at each
        configured game volume (its root and a ``.switch`` subdir) and the app
        data dir. This deliberately does NOT recurse into game libraries: a full
        walk of multi-GB trees can't be both exhaustive and non-blocking. Set
        SWITCH_KEYS to point directly at the keys dir if they live deeper.
        """
        home = os.path.expanduser("~")
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        roots = [
            os.path.join(home, ".switch"),
            os.path.join(xdg, "nsz"),
            os.path.join(home, ".config", "nsz"),
        ]
        # Game volumes + the data dir: a user may just drop prod.keys there.
        # never let key discovery break a job
        with contextlib.suppress(Exception):
            roots.extend(settings.volumes)
        with contextlib.suppress(Exception):
            roots.append(str(settings.data_dir))

        candidates: list[str] = []
        for root in roots:
            candidates.append(os.path.join(root, "prod.keys"))
            candidates.append(os.path.join(root, "keys.txt"))
            candidates.append(os.path.join(root, ".switch", "prod.keys"))
        return candidates

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

    def _build_command(self, input_path: str, work_dir: str, mode: str) -> list[str]:
        cmd = [self.nsz_path]
        if mode == "nsz_decompress":
            cmd.append("-D")
        else:
            cmd += ["-C", "-l", str(settings.nsz_compression_level)]
        cmd += ["-o", work_dir, input_path]

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
        extension. ``treat_as_stem`` is unused (nsz modes do not accept archive
        members) but kept for interface parity with z3ds."""
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
        compression: str | None = None,  # unused; kept for interface parity
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
                    input_path, produced_path, work_dir, mode, verb, env, cancel_event,
                ):
                    yield update

            await asyncio.to_thread(os.replace, produced_path, output_path)
            yield {"progress": 100, "message": f"Switch {verb} complete"}
        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

    async def _run_convert(self, input_path, produced_path, work_dir, mode, verb,
                           env, cancel_event) -> AsyncGenerator[dict, None]:
        cmd = self._build_command(input_path, work_dir, mode)

        def _preexec():
            if settings.chdman_nice is not None:
                try:
                    os.nice(settings.chdman_nice)
                except OSError:
                    pass

        # cmd is built from validated settings paths (no shell interpretation);
        # args are a fixed list, never shell-expanded.
        process = await asyncio.create_subprocess_exec(  # nosemgrep
            cmd[0], *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_preexec if os.name == "posix" else None,
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
