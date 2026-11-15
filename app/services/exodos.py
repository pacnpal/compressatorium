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
  root, and
* names the product ``<name>.dosz``, the extension DOSBox Pure documents as a
  zip under another name, so it sits beside its Source and can never replace it.

Behaviour modelled on derkommutator/eXo2romm (Unlicense); no code is shared.
"""
from __future__ import annotations

import functools
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from services.archive import archive_service
from utils.path_utils import is_within_configured_volumes

from config import settings

# The product's extension: a zip, under the name DOSBox Pure documents for one.
OUTPUT_EXTENSION = ".dosz"
INPUT_EXTENSIONS = frozenset({".zip"})


@dataclass(frozen=True)
class CollectionProfile:
    """What differs between the eXo packs: where templates live, what paths say.

    Only eXoDOS ships. eXoWin3x and the other packs follow the same pattern with
    a different template folder and path token, so adding one is a new profile,
    not new logic.
    """

    name: str                 # "eXoDOS"
    template_dir: str         # the per-game template tree's folder name, "!dos"
    path_token: str           # the pack folder mount paths start with, "eXoDOS"
    conf_name: str = "dosbox.conf"
    platform_slug: str = "dos"
    # ``.\<token>\`` lowercased, the literal every rewrite looks for.
    needle: bytes = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "needle", b".\\" + self.path_token.lower().encode("ascii") + b"\\",
        )


EXODOS = CollectionProfile(name="eXoDOS", template_dir="!dos", path_token="eXoDOS")


@dataclass(frozen=True)
class ConfRewrite:
    data: bytes
    # Lines (comments aside) that still point into the pack after the rewrite:
    # the game reaches outside its own folder and will probably not start.
    leftovers: list[str]


# A line DOSBox runs as a mount: optional blanks, an optional ``@``, then the
# command as the first word.
_MOUNT_LINE = re.compile(rb"^[ \t]*(?:@[ \t]*)?(?:mount|imgmount|boot)(?:[ \t]|$)", re.IGNORECASE)
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
    for line in data.splitlines(keepends=True):
        body, eol = _split_eol(line)
        if _MOUNT_LINE.match(body):
            body = _rewrite_line(body, profile.needle, top_bytes)
        if profile.needle in body.lower() and not _COMMENT_LINE.match(body):
            leftovers.append(body.strip().decode("cp437"))
        lines.append(body + eol)
    return ConfRewrite(data=b"".join(lines), leftovers=leftovers)


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
            tops.add(segments[0])
            has_file = has_file or (len(segments) > 1 and not info.is_dir())
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

    # --- output ------------------------------------------------------------

    @staticmethod
    def get_output_path(
        input_path: str, output_dir: str | None = None, *, treat_as_stem: bool = False,
    ) -> str:
        """``<name>.dosz`` beside the Source, or in ``output_dir``."""
        source = Path(input_path)
        filename = f"{source.stem}{OUTPUT_EXTENSION}"
        return str(Path(output_dir) / filename if output_dir else source.parent / filename)


@functools.lru_cache(maxsize=4096)
def _probe(
    profile: CollectionProfile, path: str, size: int, mtime_ns: int, root: str,
) -> bool:
    try:
        ExodosService(profile)._inspect(path, root)
    except NotRepackable:
        return False
    return True


exodos_service = ExodosService()
