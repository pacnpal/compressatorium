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

import re
from dataclasses import dataclass, field


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
