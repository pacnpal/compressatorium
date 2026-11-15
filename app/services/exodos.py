"""eXoDOS repack for RomM: an eXoDOS game zip turned into a standalone game file.

eXoDOS ships each game as a zip holding one folder (``Abaron/``) and keeps the
game's DOSBox config in a separate template tree, ``!dos/<Game>/dosbox.conf``,
whose mounts point into the whole pack (``mount c .\\eXoDOS\\Abaron``). Copied
alone into RomM, the game cannot start.

The product is what RomM's in-browser player (EmulatorJS running DOSBox Pure)
can start by itself. DOSBox Pure mounts the loaded zip as C: and, with its
``dosbox_pure_conf`` core option set to ``inside``, loads ``C:\\DOSBOX.CONF``:
``dosbox.conf`` at the zip root. So the repack

* moves every member out of the game folder (``Abaron/x`` becomes ``x``), which
  makes C:\\ hold exactly what eXoDOS's own C: held,
* writes the template, its mount paths rewritten, as ``dosbox.conf`` at the
  root and as the first entry (RomM's default EmulatorJS picks the first file
  whose extension the core lists, and DOSBox Pure lists ``.conf``), and
* names the product ``<name>.dosz``, the extension DOSBox Pure documents as a
  zip under another name, so it sits beside its Source and can never replace it.

Behaviour modelled on derkommutator/eXo2romm (Unlicense); no code is shared.
"""
from __future__ import annotations

import asyncio
import functools
import os
import re
import zipfile
import zlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from services.archive import archive_service
from services.archive_edit import rewrite_zip
from services.subprocess_runner import (
    ConversionCancelled,
    ReadCancelled,
    collect_verify,
    run_detached,
    verify_preflight,
)
from utils.path_utils import is_within_configured_volumes

from config import settings

# The product's extension: a zip, under the name DOSBox Pure documents for one.
OUTPUT_EXTENSION = ".dosz"
INPUT_EXTENSIONS = frozenset({".zip"})
# A dosbox.conf is a few KB; anything far larger is not a template.
_MAX_TEMPLATE_BYTES = 1024 * 1024
_PROGRESS_INTERVAL = 0.5
_CHECK_CHUNK = 1024 * 1024


def _never_cancelled() -> bool:
    return False


@dataclass(frozen=True)
class CollectionProfile:
    """What differs between the eXo packs: where templates live, what paths say.

    Only eXoDOS ships. eXoWin3x and the other packs follow the same pattern with
    a different template folder and pack folder name, so adding one is a new profile,
    not new logic.
    """

    name: str                 # "eXoDOS"
    template_dir: str         # the per-game template tree's folder name, "!dos"
    pack_dir: str             # the pack folder mount paths start with, "eXoDOS"
    conf_name: str = "dosbox.conf"
    platform_slug: str = "dos"
    # ``.\<token>\`` lowercased, the literal every rewrite looks for.
    needle: bytes = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "needle", b".\\" + self.pack_dir.lower().encode("ascii") + b"\\",
        )


EXODOS = CollectionProfile(name="eXoDOS", template_dir="!dos", pack_dir="eXoDOS")


@dataclass(frozen=True)
class ConfRewrite:
    data: bytes
    # Lines (comments aside) that still point into the pack after the rewrite:
    # the game reaches outside its own folder and will probably not start.
    leftovers: list[str]
    # Folder mounts on a drive other than C:. DOSBox Pure's MOUNT reads the
    # host filesystem, not the loaded file, so these cannot work from inside
    # it (C: is the file itself; IMGMOUNT and BOOT do read from it).
    folder_mounts: list[str] = field(default_factory=list)


# A line DOSBox runs as a mount: optional blanks, an optional ``@``, then the
# command as the first word.
_MOUNT_LINE = re.compile(rb"^[ \t]*(?:@[ \t]*)?(?:mount|imgmount|boot)(?:[ \t]|$)", re.IGNORECASE)
# ``mount <drive> <path>``, the drive written with or without its colon. A
# switch in place of the drive (``mount -u d``) does not match.
_FOLDER_MOUNT = re.compile(
    rb"^[ \t]*(?:@[ \t]*)?mount[ \t]+([a-z]):?[ \t]+[^-\s]", re.IGNORECASE,
)
_COMMENT_LINE = re.compile(rb"^[ \t]*(?:#|@?[ \t]*rem(?:[ \t]|$))", re.IGNORECASE)
# What may follow a path that ends at the pack or game folder.
_PATH_ENDS = (b"", b" ", b"\t", b'"')


def _split_eol(line: bytes) -> tuple[bytes, bytes]:
    if line.endswith(b"\r\n"):
        return line[:-2], b"\r\n"
    if line.endswith((b"\n", b"\r")):
        return line[:-1], line[-1:]
    return line, b""


def _rewrite_line(body: bytes, needle: bytes, top: bytes) -> bytes:
    """Rewrite every ``.\\<token>\\<top>`` on one mount line to ``.\\``.

    Matched as literal, case-insensitive text. A path into a different game's
    folder is left as it is.
    """
    lower = body.lower()
    out: list[bytes] = []
    i = 0
    while (pos := lower.find(needle, i)) >= 0:
        out.append(body[i:pos])
        after = pos + len(needle)
        rest = lower[after:]
        if rest.startswith(top):
            nxt = rest[len(top):len(top) + 1]
            if nxt == b"\\":
                out.append(b".\\")
                i = after + len(top) + 1
                continue
            if nxt in _PATH_ENDS:
                out.append(b".\\")
                i = after + len(top)
                continue
        if rest[:1] in _PATH_ENDS:
            out.append(b".\\")
            i = after
            continue
        out.append(body[pos:after])
        i = after
    out.append(body[i:])
    return b"".join(out)


def rewrite_conf(data: bytes, top: str, profile: CollectionProfile = EXODOS) -> ConfRewrite:
    """Rewrite a template's mount paths for a game repacked at the zip root.

    Works on bytes, so the file's encoding (usually CP437) and its line endings
    pass through untouched. Only ``mount``, ``imgmount`` and ``boot`` lines
    change.
    """
    top_bytes = top.lower().encode("cp437", errors="replace")
    lines: list[bytes] = []
    leftovers: list[str] = []
    folder_mounts: list[str] = []
    for line in data.splitlines(keepends=True):
        body, eol = _split_eol(line)
        if _MOUNT_LINE.match(body):
            body = _rewrite_line(body, profile.needle, top_bytes)
        if profile.needle in body.lower() and not _COMMENT_LINE.match(body):
            leftovers.append(body.strip().decode("cp437"))
        drive = _FOLDER_MOUNT.match(body)
        if drive and drive.group(1).lower() != b"c":
            folder_mounts.append(body.strip().decode("cp437"))
        lines.append(body + eol)
    return ConfRewrite(
        data=b"".join(lines), leftovers=leftovers, folder_mounts=folder_mounts,
    )


class NotRepackable(ValueError):
    """The file is not an eXoDOS game zip this tool can repack, and why."""


@dataclass(frozen=True)
class RepackPlan:
    top: str          # the game folder every member sits in, "Abaron"
    template: str     # realpath of that game's template, inside the tree
    total_size: int   # uncompressed bytes of every member


class ExodosService:
    """Reads one eXo pack's game zips and template tree. Holds no state."""

    def __init__(self, profile: CollectionProfile = EXODOS) -> None:
        self.profile = profile

    # --- gating ------------------------------------------------------------

    def dos_dir(self) -> str | None:
        """The template tree's real path, or ``None`` when it cannot be used.

        ``EXODOS_DOS_DIR`` is the only source, so a wrong value shows as an
        unavailable tool rather than a silent fallback. It must be a directory
        inside a configured Volume, the outer boundary of what the app reads.
        """
        raw = settings.exodos_dos_dir
        if not raw:
            return None
        real = os.path.realpath(raw)
        if not os.path.isdir(real):
            return None
        if not is_within_configured_volumes(real, treat_archives=False):
            return None
        return real

    def is_ready(self) -> bool:
        return self.dos_dir() is not None

    # --- inspection --------------------------------------------------------

    def inspect(self, path: str) -> RepackPlan:
        """Plan a repack, or raise :class:`NotRepackable` saying why not.

        Reads only the zip's directory. Every member name passes the shared
        archive path rules and the shared entry/size limits, all members sit in
        one game folder, and that game's template resolves inside the tree.
        """
        root = self.dos_dir()
        if root is None:
            raise NotRepackable(
                "No eXoDOS template tree: set EXODOS_DOS_DIR to the "
                f"{self.profile.template_dir} folder, inside a volume",
            )
        return self._inspect(path, root)

    def _inspect(self, path: str, root: str) -> RepackPlan:
        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise NotRepackable(f"Cannot read zip: {exc}") from exc
        top = self._game_folder(infos)
        return RepackPlan(
            top=top,
            template=self._template(root, top),
            total_size=sum(info.file_size for info in infos),
        )

    @staticmethod
    def _game_folder(infos: list[zipfile.ZipInfo]) -> str:
        try:
            archive_service.enforce_archive_limits(
                [(info.filename, info.file_size) for info in infos],
            )
        except ValueError as exc:
            raise NotRepackable(str(exc)) from exc
        tops: set[str] = set()
        has_file = False
        for info in infos:
            name = info.filename[:-1] if info.is_dir() else info.filename
            try:
                segments = archive_service.member_segments(name)
            except ValueError as exc:
                raise NotRepackable(f"{exc}: {info.filename!r}") from exc
            # The shared rules check a stripped name; the repack slices the
            # raw one. Refuse any name the two would read differently.
            if "/".join(segments) != name:
                raise NotRepackable(
                    f"Member name has leading or trailing spaces: {info.filename!r}",
                )
            if len(segments) == 1 and not info.is_dir():
                raise NotRepackable(
                    "The zip does not hold a single game folder "
                    f"(a file sits at its root: {info.filename!r})",
                )
            tops.add(segments[0])
            has_file = has_file or len(segments) > 1
        if len(tops) != 1 or not has_file:
            raise NotRepackable("The zip does not hold a single game folder")
        return tops.pop()

    def _template(self, root: str, top: str) -> str:
        real = os.path.realpath(os.path.join(root, top, self.profile.conf_name))
        if os.path.commonpath([real, root]) != root:
            raise NotRepackable(
                f"The template for {top} resolves outside EXODOS_DOS_DIR",
            )
        if not os.path.isfile(real):
            raise NotRepackable(
                f"The game has no template: {top}/{self.profile.conf_name} "
                "is not in EXODOS_DOS_DIR",
            )
        return real

    def is_repackable(self, path: str) -> bool:
        """Whether ``path`` can be repacked. Cached per (path, size, mtime)."""
        if Path(path).suffix.lower() not in INPUT_EXTENSIONS:
            return False
        root = self.dos_dir()
        if root is None:
            return False
        try:
            st = os.stat(path)
        except OSError:
            return False
        return _probe(self.profile, path, st.st_size, st.st_mtime_ns, root)

    def _read_template(self, path: str) -> bytes:
        if os.path.getsize(path) > _MAX_TEMPLATE_BYTES:
            raise NotRepackable(f"The template is larger than {_MAX_TEMPLATE_BYTES} bytes")
        with open(path, "rb") as handle:
            return handle.read()

    # --- convert -----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Write the repacked game to ``output_path``; the Source is only read.

        Raises :class:`NotRepackable` for a zip that is not one game folder with
        a template, and ``ConversionCancelled`` when cancelled. Either way no
        product is left behind (see :func:`services.archive_edit.rewrite_zip`).
        The last update carries ``warning`` when the conf still points outside
        the game.
        """
        plan = await run_in_threadpool(self.inspect, input_path)
        template = await run_in_threadpool(self._read_template, plan.template)
        rewrite = rewrite_conf(template, plan.top, self.profile)
        prefix = f"{plan.top}/"
        progress = {"done": 0, "total": plan.total_size}

        def rename(name: str) -> str | None:
            return None if name == prefix else name[len(prefix):]

        def on_progress(done: int, total: int) -> None:
            progress["done"], progress["total"] = done, total

        def is_cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        yield {"progress": 0, "message": f"Repacking {plan.top}"}
        task = asyncio.ensure_future(run_in_threadpool(
            rewrite_zip, input_path, output_path,
            rename=rename,
            add={self.profile.conf_name: rewrite.data},
            # The same CRC and structure check Verify runs, on the finished
            # temp file: a product that fails it is never placed, so the job
            # fails and leaves no output. The Source is only ever read.
            check=lambda path: self._own_check(path, is_cancelled),
            is_cancelled=is_cancelled,
            on_progress=on_progress,
        ))
        last = -1
        while not task.done():
            await asyncio.wait({task}, timeout=_PROGRESS_INTERVAL)
            if task.done() or is_cancelled():
                continue
            done = progress["done"]
            yield {
                "progress": min(99, done * 100 // max(progress["total"], 1)),
                "message": f"Repacking {plan.top}",
                "activity": done != last,
            }
            last = done
        task.result()
        caveats = []
        if rewrite.leftovers:
            caveats.append(
                "The conf still points outside the game: "
                + "; ".join(rewrite.leftovers),
            )
        if rewrite.folder_mounts:
            caveats.append(
                "These folder mounts read the host disk, not the file: "
                + "; ".join(rewrite.folder_mounts),
            )
        if caveats:
            yield {
                "progress": 100,
                "message": f"Repacked, but it may not start. {'. '.join(caveats)}.",
                "warning": True,
            }
        else:
            yield {"progress": 100, "message": "Repack complete"}

    # --- verify ------------------------------------------------------------

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await collect_verify(
            self.verify_stream(path, cancel_event=cancel_event),
            fallback_message="Verification failed",
        )

    async def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """The product's CRC and structure check.

        Every member's CRC must read back, every name must pass the shared
        archive path rules and limits, and ``dosbox.conf`` must sit at the
        root, where DOSBox Pure looks for it. It does not compare with the
        Source: the product differs from it by design.
        """
        problem, _size = await verify_preflight(
            path, {OUTPUT_EXTENSION}, cancel_event=cancel_event,
        )
        if problem is not None:
            yield problem
            return
        try:
            await run_detached(
                self._check_product, path,
                cancel_event.is_set if cancel_event else _never_cancelled,
                cancel_event=cancel_event,
            )
        except ReadCancelled:
            yield {
                "type": "error", "valid": False, "cancelled": True,
                "message": "Verification cancelled",
            }
            return
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            yield {"type": "error", "valid": False, "message": str(exc)}
            return
        yield {"type": "complete", "valid": True, "message": "File is Intact"}

    def _own_check(self, path: str, is_cancelled: Callable[[], bool]) -> None:
        try:
            self._check_product(path, is_cancelled)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise RuntimeError(
                f"The repacked file failed its own check, so it was not kept: {exc}",
            ) from exc

    def _check_product(
        self, path: str, is_cancelled: Callable[[], bool] = _never_cancelled,
    ) -> None:
        """CRCs, member names and limits, and the root conf, or ``ValueError``.

        Reads every member, so it polls ``is_cancelled`` between chunks and
        raises ``ConversionCancelled`` rather than make a cancelled job wait
        out a multi-GB read.
        """
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            archive_service.enforce_archive_limits(
                [(info.filename, info.file_size) for info in infos],
            )
            for info in infos:
                name = info.filename[:-1] if info.is_dir() else info.filename
                try:
                    archive_service.member_segments(name)
                except ValueError as exc:
                    raise ValueError(f"{exc}: {info.filename!r}") from exc
            if self.profile.conf_name not in {info.filename for info in infos}:
                raise ValueError(f"No {self.profile.conf_name} at the root of the file")
            for info in infos:
                if not info.is_dir():
                    self._read_member(zf, info, is_cancelled)

    @staticmethod
    def _read_member(
        zf: zipfile.ZipFile, info: zipfile.ZipInfo, is_cancelled: Callable[[], bool],
    ) -> None:
        # zipfile checks the CRC once the member has been read to its end.
        try:
            with zf.open(info) as member:
                while True:
                    if is_cancelled():
                        raise ConversionCancelled("Conversion cancelled")
                    if not member.read(_CHECK_CHUNK):
                        return
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise ValueError(f"CRC check failed for {info.filename}: {exc}") from exc

    # --- info --------------------------------------------------------------

    def info(self, path: str) -> dict:
        """What the file is, for the shared basic-info shape. Synchronous."""
        size = os.path.getsize(path)
        return {
            "file": os.path.basename(path),
            "size": size,
            "size_display": f"{size} bytes",
            "format": "DOSBox Pure zip" if path.lower().endswith(OUTPUT_EXTENSION) else "zip",
            "extension": Path(path).suffix.lower(),
            "compressed": True,
            "compression_type": "zip",
        }

    # --- output ------------------------------------------------------------

    @staticmethod
    def get_output_path(
        input_path: str, output_dir: str | None = None, *, treat_as_stem: bool = False,
    ) -> str:
        """``<name>.dosz`` beside the Source, or in ``output_dir``."""
        source = Path(input_path)
        filename = f"{source.stem}{OUTPUT_EXTENSION}"
        return str(Path(output_dir) / filename if output_dir else source.parent / filename)

    def get_output_path_for_mode(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        return self.get_output_path(input_path, output_dir, treat_as_stem=treat_as_stem)


def _probe(
    profile: CollectionProfile, path: str, size: int, mtime_ns: int, root: str,
) -> bool:
    """Whether the zip can be repacked. Only a yes is cached.

    A no can turn into a yes without the zip changing (its template appears
    in the tree mid-way through an eXoDOS update), so it is asked again.
    """
    try:
        return _probe_yes(profile, path, size, mtime_ns, root)
    except NotRepackable:
        return False


@functools.lru_cache(maxsize=4096)
def _probe_yes(
    profile: CollectionProfile, path: str, size: int, mtime_ns: int, root: str,
) -> bool:
    # lru_cache does not cache a raised exception, so only a yes is kept.
    ExodosService(profile)._inspect(path, root)
    return True


exodos_service = ExodosService()
