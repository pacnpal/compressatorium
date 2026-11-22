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

No setting is needed (#513). The template tree is found from the zip itself:
``!dos`` beside the zip, then in each parent folder up to the Volume, after the
optional ``EXODOS_DOS_DIR`` override. A game with no template is still
repacked, with no ``dosbox.conf``, and the job warns that DOSBox Pure's start
menu will offer the program to run. Such a zip counts as a DOS game only when
its one folder holds a DOS program (``.exe``, ``.com`` or ``.bat``).

Behaviour modelled on derkommutator/eXo2romm (Unlicense); no code is shared.
"""
from __future__ import annotations

import asyncio
import errno
import os
import re
import stat
import threading
import zipfile
import zlib
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from services.archive import ArchiveSafetyRefused, archive_service
from services.archive_edit import rewrite_zip
from services.subprocess_runner import (
    ConversionCancelled,
    ReadCancelled,
    SubprocessRunner,
    collect_verify,
    conversion_stall_bound,
    run_detached,
    run_detached_within_stall_bound,
    verify_exception_event,
    verify_preflight,
)
from utils.path_utils import (
    is_within_configured_volumes,
    lock_boundary_root,
    read_beneath,
)

from config import settings

# The product's extension: a zip, under the name DOSBox Pure documents for one.
OUTPUT_EXTENSION = ".dosz"
INPUT_EXTENSIONS = frozenset({".zip"})
# A dosbox.conf is a few KB; anything far larger is not a template.
_MAX_TEMPLATE_BYTES = 1024 * 1024
_PROGRESS_INTERVAL = 0.5
_CHECK_CHUNK = 1024 * 1024
# What DOSBox Pure's start menu offers to run. A zip with no template counts as
# a DOS game only when it holds one of these.
_DOS_PROGRAMS = (".exe", ".com", ".bat")
_DOS_PROGRAMS_TEXT = "DOS program (.exe, .com or .bat)"


def _has_dos_program(names) -> bool:
    return any(name.lower().endswith(_DOS_PROGRAMS) for name in names)


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
    # Folder mounts that cannot work from inside the file: any drive but C:,
    # and a C: mount of a subfolder. DOSBox Pure's MOUNT reads the host
    # filesystem and C: is already the file itself (IMGMOUNT and BOOT do
    # read from it). See _folder_mount_misses.
    folder_mounts: list[str] = field(default_factory=list)


# A line DOSBox runs as a mount: optional blanks, an optional ``@``, then the
# command as the first word.
_MOUNT_LINE = re.compile(rb"^[ \t]*(?:@[ \t]*)?(?:mount|imgmount|boot)(?:[ \t]|$)", re.IGNORECASE)
# ``mount <drive> <path>``, the drive written with or without its colon. A
# switch in place of the drive (``mount -u d``) does not match.
_FOLDER_MOUNT = re.compile(
    rb'^[ \t]*(?:@[ \t]*)?mount[ \t]+([a-z]):?[ \t]+("[^"]*"|[^-\s][^\s]*)', re.IGNORECASE,
)
# The spellings of "the root of the file" a C: mount may name after the
# rewrite; any other C: mount names a subfolder.
_ROOT_PATHS = frozenset({b".", b".\\", b"./"})
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


def _folder_mount_misses(body: bytes) -> bool:
    """Whether a ``mount`` line cannot reach its folder from inside the file.

    DOSBox Pure mounts the loaded file as C: and reads a folder mount from the
    host disk. So a folder mount on any other drive finds nothing, and a C:
    mount of a subfolder is refused ("already mounted"), leaving C: at the
    file root where the game's files are not. Only a C: mount of the root
    itself (``.``, ``.\\``) is harmless.
    """
    found = _FOLDER_MOUNT.match(body)
    if not found:
        return False
    if found.group(1).lower() != b"c":
        return True
    target = found.group(2).strip(b'"')
    return target not in _ROOT_PATHS


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
        if _folder_mount_misses(body):
            folder_mounts.append(body.strip().decode("cp437"))
        lines.append(body + eol)
    return ConfRewrite(
        data=b"".join(lines), leftovers=leftovers, folder_mounts=folder_mounts,
    )


class NotRepackable(ValueError):
    """The file is not an eXoDOS game zip this tool can repack, and why.

    ``top`` names the game folder when the zip got as far as having one (its
    template is the problem), so a cached miss can watch that folder.
    """

    def __init__(self, message: str, *, top: str | None = None) -> None:
        super().__init__(message)
        self.top = top


class ProbeUnavailable(NotRepackable):
    """The zip or its template could not be read just now (a permission or
    I/O error, an unreadable folder, a timeout), which says nothing about the
    game: the probe never caches it and asks again next time. Still a
    :class:`NotRepackable`, so a job fails and keeps its Source. Only a
    verdict (not an eXoDOS layout, no template) is ever cached."""


class NoTemplate(NotRepackable):
    """This template tree has no template for the game: look in the next one."""


class ZipUnreadable(ProbeUnavailable):
    """The game zip could not be read just now."""


class TemplateUnreadable(ProbeUnavailable):
    """The template could not be read just now."""


# What a template read can fail with that is a fact about the tree, not a
# passing condition: a link where a file or folder should be (``ELOOP``,
# ``EMLINK`` on some systems), a file where a folder should be, or not a
# regular file (``read_beneath``'s ``EINVAL``).
_STRUCTURAL_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.EINVAL})


@dataclass(frozen=True)
class RepackPlan:
    top: str                 # the game folder every member sits in, "Abaron"
    template: str | None     # its template's path, for messages; None: none found
    total_size: int          # uncompressed bytes of every member
    root: str | None = None  # the tree's real path; reads go through read_beneath
    # Why no template was found, worked out during the bounded inspection so
    # the last update reads nothing on the event loop; None when one was.
    no_template_reason: str | None = None


class ExodosService:
    """Reads one eXo pack's game zips and template tree. Holds no state."""

    def __init__(self, profile: CollectionProfile = EXODOS) -> None:
        self.profile = profile

    # --- finding the template tree (#513) ----------------------------------

    def override(self) -> str | None:
        """``EXODOS_DOS_DIR``'s real path, or ``None`` when unset or unusable.

        Optional: a folder inside a configured Volume, searched before any
        ``!dos`` found from the zip. A value that is not one is skipped, and a
        game repacked without a template says so.
        """
        raw = settings.exodos_dos_dir
        return self._usable(raw) if raw else None

    @staticmethod
    def _usable(candidate: str) -> str | None:
        """``candidate``'s real path if it is a folder inside a Volume.

        A folder that is missing (or not a folder) is skipped; one that can't
        be read just now raises :class:`TemplateUnreadable`, never a reason
        to repack without its template (CodeRabbit PR #517)."""
        real = os.path.realpath(candidate)
        try:
            if not stat.S_ISDIR(os.stat(real).st_mode):
                return None
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            if exc.errno in _STRUCTURAL_ERRNOS:
                return None
            raise TemplateUnreadable(
                f"The template folder {real} could not be read: {exc}",
            ) from exc
        if not is_within_configured_volumes(real, treat_archives=False):
            return None
        return real

    def candidates(self, path: str) -> list[str]:
        """Every place a template tree may be for the zip at ``path``, in order.

        The ``EXODOS_DOS_DIR`` override first, then ``!dos`` beside the zip and
        in each parent folder up to the outermost Volume root over it
        (``lock_boundary_root``). Not yet checked: :meth:`_usable` decides, so
        each one must still be a folder inside a Volume.
        """
        raw = settings.exodos_dos_dir
        found = [os.path.realpath(raw)] if raw else []
        boundary = lock_boundary_root(path, treat_archives=False)
        if boundary is None:
            return found
        folder = Path(os.path.realpath(path)).parent
        while folder == boundary or boundary in folder.parents:
            found.append(str(folder / self.profile.template_dir))
            if folder == boundary:
                break
            folder = folder.parent
        return list(dict.fromkeys(found))

    def _find_tree(self, top: str, candidates: list[str]) -> str | None:
        """The first usable tree holding ``top``'s template, read the confined
        way, or ``None``. A template that is there but refused, or unreadable
        just now, raises: it is never a reason to repack without it."""
        for candidate in candidates:
            root = self._usable(candidate)
            if root is None:
                continue
            try:
                self._read_template(root, top)
            except NoTemplate:
                continue
            return root
        return None

    # --- inspection --------------------------------------------------------

    def inspect(self, path: str) -> RepackPlan:
        """Plan a repack, or raise :class:`NotRepackable` saying why not.

        Reads only the zip's directory. Every member name passes the shared
        archive path rules and the shared entry/size limits, and all members
        sit in one game folder. That game's template, when one is found,
        resolves inside its tree; with none, the folder must hold a DOS program.
        """
        infos, top = self._read_zip(path)
        return self._plan(infos, top, self.candidates(path))

    def _plan(
        self, infos: list[zipfile.ZipInfo], top: str, candidates: list[str],
    ) -> RepackPlan:
        root = self._find_tree(top, candidates)
        if root is None and not _has_dos_program(info.filename for info in infos):
            raise NotRepackable(
                f"No dosbox.conf template was found for {top}, and its folder holds "
                f"no {_DOS_PROGRAMS_TEXT}",
                top=top,
            )
        return RepackPlan(
            top=top,
            template=os.path.join(root, top, self.profile.conf_name) if root else None,
            total_size=sum(info.file_size for info in infos),
            root=root,
            no_template_reason=None if root else self._no_template_reason(top),
        )

    def _read_zip(self, path: str) -> tuple[list[zipfile.ZipInfo], str]:
        """The zip's members and its one game folder, or NotRepackable.
        A zip that is not a zip is a verdict; any OSError reading it is
        :class:`ZipUnreadable`, which the probe never caches."""
        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
        except zipfile.BadZipFile as exc:
            raise NotRepackable(f"Cannot read zip: {exc}") from exc
        except OSError as exc:
            raise ZipUnreadable(f"Cannot read zip: {exc}") from exc
        return infos, self._game_folder(infos)

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

    def is_repackable(self, path: str) -> bool:
        """Whether ``path`` can be repacked.

        Cached, a yes and a no alike, per (path, size, mtime) of the zip, and
        valid only while every candidate template tree is unchanged: each
        tree's and its game folder's mtimes, and the template file's own mtime,
        size and inode. Adding, removing, renaming or fixing a template in
        place changes one of them, and the zip is asked again (#331).
        """
        if Path(path).suffix.lower() not in INPUT_EXTENSIONS:
            return False
        try:
            st = os.stat(path)
        except OSError:
            return False
        return _probe(self.profile, path, st.st_size, st.st_mtime_ns)

    def _read_template(self, root: str, top: str) -> bytes:
        """Read ``<root>/<top>/dosbox.conf`` through one confined descriptor.

        Every template byte comes from :func:`utils.path_utils.read_beneath`:
        no component may be a symlink, the file must be a regular file, and
        nothing is opened by path afterwards. Someone who can write the Volume
        therefore cannot swap the template for a link between the check in
        ``inspect`` and this read (PR #324's security review). Any refusal is
        ``NotRepackable``, which fails the job and keeps the Source; one that
        may pass (a permission or I/O error, an unreadable folder, a timeout)
        is :class:`TemplateUnreadable`, which the probe cache never keeps.
        """
        relative = f"{top}/{self.profile.conf_name}"
        try:
            return read_beneath(root, relative, max_bytes=_MAX_TEMPLATE_BYTES)
        except FileNotFoundError as exc:
            raise NoTemplate(
                f"The game has no template: {relative} is not in {root}", top=top,
            ) from exc
        except OSError as exc:
            if exc.errno not in _STRUCTURAL_ERRNOS:
                raise TemplateUnreadable(
                    f"The template for {top} could not be read: {exc}", top=top,
                ) from exc
            raise NotRepackable(
                f"The template for {top} is a link, not a plain file, or lies "
                f"outside its {self.profile.template_dir} folder: {exc}",
                top=top,
            ) from exc
        except ValueError as exc:
            raise NotRepackable(
                f"The template for {top} was refused: {exc}", top=top,
            ) from exc

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

        Raises :class:`NotRepackable` for a zip that is not one DOS game
        folder, and ``ConversionCancelled`` when cancelled. Either way no
        product is left behind (see :func:`services.archive_edit.rewrite_zip`).
        The last update says what the game got: its conf and where it came
        from, or no conf and why. It carries ``warning`` when there is no conf
        or the conf still points outside the game.
        """
        # These reads run before the rewrite's own watchdog exists, so each is
        # bounded by the same no-progress timeout and raced against Cancel, as
        # xiso's are: a Volume that hangs here can't hold the job, or the
        # queue slot behind it, and the Source is only ever read.
        try:
            plan = await run_detached_within_stall_bound(
                self.inspect, input_path,
                what="Reading the game zip", cancel_event=cancel_event,
            )
            template = None
            if plan.root is not None:
                template = await run_detached_within_stall_bound(
                    self._read_template, plan.root, plan.top,
                    what="Reading the dosbox.conf template", cancel_event=cancel_event,
                )
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc
        rewrite = None if template is None else rewrite_conf(template, plan.top, self.profile)
        prefix = f"{plan.top}/"
        # "beats" counts the worker's polls of is_cancelled, which it makes
        # between every chunk it copies or checks: a worker still polling is a
        # worker still making progress.
        progress = {"done": 0, "total": plan.total_size, "beats": 0}
        abandoned = threading.Event()
        # With no template, the zip's own top-level conf is left out: it was
        # never rewritten, and DOSBox Pure would load it instead of offering
        # the start menu (Codex PR #517).
        own_conf = self.profile.conf_name.lower()
        left_out: list[str] = []

        def rename(name: str) -> str | None:
            if name == prefix:
                return None
            # Any case: DOSBox Pure reads C:\DOSBOX.CONF as dosbox.conf.
            if rewrite is None and name[len(prefix):].lower() == own_conf:
                left_out.append(name)
                return None
            return name[len(prefix):]

        def on_progress(done: int, total: int) -> None:
            progress["done"], progress["total"] = done, total

        def is_cancelled() -> bool:
            progress["beats"] += 1
            return abandoned.is_set() or (cancel_event is not None and cancel_event.is_set())

        yield {"progress": 0, "message": f"Repacking {plan.top}"}
        # Detached, not pooled: a mount that stops answering blocks the worker
        # in a syscall nothing can interrupt. Cancel and the no-progress bound
        # below return without waiting for it, and set `abandoned`, which the
        # worker reads before it would place the product.
        task = asyncio.ensure_future(run_detached(
            rewrite_zip, input_path, output_path,
            rename=rename,
            add={self.profile.conf_name: rewrite.data} if rewrite else None,
            # The same CRC and structure check Verify runs, on the finished
            # temp file: a product that fails it is never placed, so the job
            # fails and leaves no output. The Source is only ever read. A
            # product that was given a conf must hold it.
            check=lambda path: self._own_check(
                path, is_cancelled, require_conf=rewrite is not None,
            ),
            is_cancelled=is_cancelled,
            on_progress=on_progress,
            cancel_event=cancel_event,
        ))
        try:
            async for update in self._watch(task, plan.top, progress):
                yield update
        except BaseException:
            abandoned.set()
            raise
        yield self._outcome(plan, rewrite, left_out=bool(left_out))

    def _outcome(
        self, plan: RepackPlan, rewrite: ConfRewrite | None, *, left_out: bool = False,
    ) -> dict:
        """The last update: what the game got, its conf or no conf and why."""
        if rewrite is None:
            own = (
                f" The zip's own {plan.top}/{self.profile.conf_name} was left out:"
                " it was never rewritten for the file."
                if left_out else ""
            )
            return {
                "progress": 100,
                "message": (
                    f"Repacked with no {self.profile.conf_name}: "
                    f"{plan.no_template_reason}.{own} DOSBox Pure's start "
                    "menu will offer the program to run."
                ),
                "warning": True,
            }
        got = f"Repacked with its {self.profile.conf_name} from {plan.template}"
        caveats = []
        if rewrite.leftovers:
            caveats.append(
                "The conf still points outside the game: "
                + "; ".join(rewrite.leftovers),
            )
        if rewrite.folder_mounts:
            caveats.append(
                "These folder mounts cannot reach the file's contents (DOSBox Pure "
                "mounts the file as C: and reads folders from the host disk): "
                + "; ".join(rewrite.folder_mounts),
            )
        if caveats:
            return {
                "progress": 100,
                "message": f"{got}, but it may not start. {'. '.join(caveats)}.",
                "warning": True,
            }
        return {"progress": 100, "message": got}

    def _no_template_reason(self, top: str) -> str:
        reason = (
            f"no {top}/{self.profile.conf_name} template was found in a "
            f"{self.profile.template_dir} folder beside the zip or above it, up to the volume"
        )
        if settings.exodos_dos_dir and self.override() is None:
            reason += " (EXODOS_DOS_DIR is set but is not a folder inside a volume)"
        elif settings.exodos_dos_dir:
            reason += ", or in EXODOS_DOS_DIR"
        return reason

    @staticmethod
    async def _watch(
        task: asyncio.Future, top: str, progress: dict,
    ) -> AsyncGenerator[dict, None]:
        """Report the worker's progress until it ends; give up on a stalled one.

        A cancel surfaces from ``run_detached`` as ``ReadCancelled`` the moment
        the event fires, and becomes ``ConversionCancelled``. A worker that
        stops polling for ``conversion_stall_bound()`` seconds is abandoned
        with a ``RuntimeError``, freeing the job and the queue slot.
        """
        bound = conversion_stall_bound()
        loop = asyncio.get_running_loop()
        last_beat, last_change, last_done = -1, loop.time(), -1
        while not task.done():
            await asyncio.wait({task}, timeout=_PROGRESS_INTERVAL)
            if task.done():
                break
            now = loop.time()
            if progress["beats"] != last_beat:
                last_beat, last_change = progress["beats"], now
            elif bound and now - last_change > bound:
                task.cancel()
                raise RuntimeError(
                    f"Repacking {top} stopped responding (no progress in {bound}s); "
                    "the volume may be offline",
                )
            done = progress["done"]
            yield {
                "progress": min(99, done * 100 // max(progress["total"], 1)),
                "message": f"Repacking {top}",
                "activity": done != last_done,
            }
            last_done = done
        try:
            task.result()
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc

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
        except (ReadCancelled, ConversionCancelled):
            yield {
                "type": "error", "valid": False, "cancelled": True,
                "message": "Verification cancelled",
            }
            return
        except ArchiveSafetyRefused as exc:
            # A rule this app sets (a size or entry limit), not damage: the file
            # can't be checked here, the same answer romz gives (#306).
            yield SubprocessRunner._verify_error(
                "This archive can't be checked (refused by the archive safety "
                f"rules: {exc.rule})",
                uncheckable=True,
            )
            return
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            # The shared rule, as every tool's verify uses it: a file that
            # could not be read (EACCES, EIO, a mount gone) is no verdict
            # (`inconclusive`, so a stored check stays); a format error is one.
            yield verify_exception_event(exc)
            return
        yield {"type": "complete", "valid": True, "message": "File is Intact"}

    def _own_check(
        self, path: str, is_cancelled: Callable[[], bool], *, require_conf: bool,
    ) -> None:
        try:
            self._check_product(path, is_cancelled, require_conf=require_conf)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise RuntimeError(
                f"The repacked file failed its own check, so it was not kept: {exc}",
            ) from exc

    def _check_product(
        self,
        path: str,
        is_cancelled: Callable[[], bool] = _never_cancelled,
        *,
        require_conf: bool = False,
    ) -> None:
        """CRCs, member names and limits, and the root conf, or ``ValueError``.

        A product repacked with no template has no conf; it must then hold a
        DOS program, the rule that let it be repacked (#513). ``require_conf``
        is the repack's own check of a product it gave a conf.

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
            names = [info.filename for info in infos]
            if self.profile.conf_name not in names:
                if require_conf:
                    raise ValueError(f"No {self.profile.conf_name} at the root of the file")
                if not _has_dos_program(names):
                    raise ValueError(
                        f"No {self.profile.conf_name} at the root of the file, and "
                        f"no {_DOS_PROGRAMS_TEXT} in it",
                    )
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
        # ConversionCancelled subclasses only Exception, so none of these
        # handlers can turn a cancel into a ValueError.
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise ValueError(f"CRC check failed for {info.filename}: {exc}") from exc
        except (RuntimeError, NotImplementedError) as exc:
            # zipfile's answer for an encrypted member (RuntimeError) or a
            # compression method it can't decode (NotImplementedError).
            raise ValueError(f"Cannot read {info.filename}: {exc}") from exc

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


_PROBE_CACHE: OrderedDict[tuple, tuple[bool, str | None, tuple]] = OrderedDict()
_PROBE_CACHE_SIZE = 4096
_PROBE_LOCK = threading.Lock()


def _tree_stamp(candidates: list[str], top: str | None, conf_name: str) -> tuple:
    """What a cached answer depends on: each candidate tree's (mtime, size,
    inode) and, when the zip named a game folder, that folder's and the
    template file's own. A tree or template created, removed, renamed or
    rewritten in place changes one of them. ``None`` for anything not there."""
    paths = []
    for candidate in candidates:
        root = os.path.realpath(candidate)
        paths.append(root)
        if top:
            folder = os.path.join(root, top)
            paths += [folder, os.path.join(folder, conf_name)]
    stamps: list[tuple[int, int, int] | None] = []
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            stamps.append(None)
            continue
        stamps.append((info.st_mtime_ns, info.st_size, info.st_ino))
    return tuple(stamps)


def _probe(profile: CollectionProfile, path: str, size: int, mtime_ns: int) -> bool:
    """Whether the zip can be repacked, from the cache while it is still true."""
    service = ExodosService(profile)
    candidates = service.candidates(path)
    key = (profile, path, size, mtime_ns, tuple(candidates))
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached is not None:
        answer, top, stamp = cached
        if _tree_stamp(candidates, top, profile.conf_name) == stamp:
            return answer
    top = None
    # The stamp is taken before the template is read and checked again after:
    # a tree that moved while it was read keeps the answer out of the cache.
    before = _tree_stamp(candidates, None, profile.conf_name)
    try:
        infos, top = service._read_zip(path)
        before = _tree_stamp(candidates, top, profile.conf_name)
        service._plan(infos, top, candidates)
        answer = True
    except ProbeUnavailable:
        return False  # not a verdict: asked again next time, never cached
    except NotRepackable:
        answer = False
    if _tree_stamp(candidates, top, profile.conf_name) != before:
        return answer
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = (answer, top, before)
        _PROBE_CACHE.move_to_end(key)
        while len(_PROBE_CACHE) > _PROBE_CACHE_SIZE:
            _PROBE_CACHE.popitem(last=False)
    return answer


exodos_service = ExodosService()
