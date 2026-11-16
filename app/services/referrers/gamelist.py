"""The ``gamelist.xml`` Referrer: EmulationStation frontends' game lists (#386).

ES-DE, Batocera, Knulli, muOS and legacy EmulationStation key each game in a
Platform's ``gamelist.xml`` on its ``<path>``, and hang the artwork, the
description, the favourite flag and the play count off that entry. When the
file a ``<path>`` names is replaced, the entry is rewritten to the output
before the Source goes, so the frontend keeps all of it.

Opt-in per Library (``Library.referrer_enabled("gamelist")``): the app never
edits another app's files unless the user said the Library is used by an
EmulationStation frontend. File phase, like ``.m3u``.

**Where.** A ``gamelist.xml`` directly in the replaced file's folder or in
any folder above it up to its Volume root (``files.search_dirs``): that is the
Platform folder, where Batocera, Knulli, muOS and legacy EmulationStation keep
it, with the Image beside it or in a subfolder. Each entry is read relative to
its gamelist's folder.

**Which entries.** The ``<path>`` of each ``<game>`` or ``<folder>`` under the
root, read the way ES-DE reads it: backslashes are separators, ``./`` is the
gamelist's folder, anything else is taken as written. An entry is rewritten
only when it resolves to a replaced file, matched by identity
(``same_identity``), so relative (``./Game.iso``), bare, nested
(``./Multi Disc/Game.cue``) and absolute spellings all count. The new text
keeps the entry's own style: its ``./`` prefix, its folder part, backslashes.

**Nothing else changes.** The file is parsed (``defusedxml``, which refuses
entity declarations and external references) only to find each ``<path>``'s
byte span; the new text replaces exactly that span, escaped and encoded in
the file's own encoding. The declaration, BOM, encoding, line endings,
comments, attributes, element order and every other element stay byte for
byte. The result is parsed again and must read back to the same entries with
only the rewritten ones moved to the output.

**Fail closed.** A gamelist that can't be read or parsed, that names a
replaced file in an entry that doesn't resolve to it, or whose matching entry
can't be edited in place (CDATA, a comment inside it, a child element, a
UTF-16 file) raises ``ReferrerError`` naming the file: the Source is kept.

Writes go through ``files.rewrite_in_place``: re-read under the in-process
and cross-process locks, one ``gamelist.xml.bak`` kept, atomic replace.
"""

from __future__ import annotations

import codecs
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser, ParseError
from utils.path_utils import file_identity

from .base import Phase, Referrer, ReferrerChange, ReferrerError
from .files import (
    Renames,
    Target,
    dirs_for,
    may_read,
    named_file,
    points_at,
    read_target,
    rewrite_in_place,
    search_dirs,
    target_lock,
)

GAMELIST_NAME = "gamelist.xml"
# How long ``apply`` waits for another process rewriting the same gamelist;
# inside the hook it also ends as soon as the job stops waiting.
GAMELIST_LOCK_WAIT_SECONDS = 30.0

# <gameList> (depth 0) > <game> or <folder> (depth 1) > <path> (depth 2).
_PATH_DEPTH = 2
_PATH_TAG = "path"
_DECLARED = re.compile(rb"""^<\?xml[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
# Bytes an ASCII-compatible encoding writes the same as ASCII, so a span can
# be found by its markup bytes.
_MARKUP = "<>&;/.\"'= \t\r\n?!-[]"


@dataclass(frozen=True)
class _Entry:
    """One ``<path>`` as the parser read it."""

    text: str
    # The byte span of its text in the file, or None when it can't be edited
    # in place (empty element, CDATA or a comment inside, child elements, an
    # encoding that isn't ASCII-compatible).
    span: tuple[int, int] | None


class _Paths:
    """Parser target that records each ``<path>`` and where its text sits."""

    def __init__(self) -> None:
        self.parser: DefusedXMLParser | None = None
        self.found: list[tuple[str, int, int, bool]] = []  # text, start, end, plain
        self._depth = 0
        self._open: tuple[int, list[str]] | None = None  # start index, text parts
        self._plain = True

    def _index(self) -> int:
        return self.parser.parser.CurrentByteIndex

    def start(self, tag, _attrib) -> None:
        if self._open is not None:
            self._plain = False  # a child element inside <path>
        elif tag == _PATH_TAG and self._depth == _PATH_DEPTH:
            self._open, self._plain = (self._index(), []), True
        self._depth += 1

    def data(self, text: str) -> None:
        if self._open is not None:
            self._open[1].append(text)

    def end(self, tag) -> None:
        self._depth -= 1
        if self._open is not None and tag == _PATH_TAG and self._depth == _PATH_DEPTH:
            start, parts = self._open
            self.found.append(("".join(parts), start, self._index(), self._plain))
            self._open = None

    def close(self) -> list[tuple[str, int, int, bool]]:
        return self.found


def _codec(data: bytes) -> str | None:
    """The Python codec of an ASCII-compatible gamelist, or None when it
    isn't one (UTF-16 or UTF-32, an unknown name)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:4096]:
        return None
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8"
    declared = _DECLARED.match(data)
    name = declared.group(1).decode("ascii") if declared else "utf-8"
    try:
        codec = codecs.lookup(name).name
    except LookupError:
        return None
    try:
        same = _MARKUP.encode(codec) == _MARKUP.encode("ascii")
    except UnicodeEncodeError:
        return None
    return codec if same else None


def _text_span(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """The bytes between ``<path ...>`` at *start* and ``</path>`` at *end*,
    or None when that isn't plain text (an empty ``<path/>``, CDATA, a
    comment or processing instruction inside)."""
    quote = None
    for index in range(start + 1, len(data)):
        byte = data[index:index + 1]
        if quote:
            quote = None if byte == quote else quote
        elif byte in (b'"', b"'"):
            quote = byte
        elif byte == b">":
            break
    else:
        return None
    if data[index - 1:index] == b"/" or not data.startswith(b"</", end):
        return None
    opened = index + 1
    if b"<" in data[opened:end]:
        return None
    return opened, end


def _entries(gamelist: str, data: bytes) -> list[_Entry]:
    """Every ``<path>`` in *data*, in order. ReferrerError when it can't be parsed."""
    target = _Paths()
    parser = DefusedXMLParser(target=target)
    target.parser = parser
    try:
        parser.feed(data)
        found = parser.close()
    except (ParseError, DefusedXmlException, ValueError, LookupError) as exc:
        raise ReferrerError(gamelist, f"can't be parsed ({exc})") from exc
    editable = _codec(data) is not None
    return [
        _Entry(text, _text_span(data, start, end) if editable and plain else None)
        for text, start, end, plain in found
    ]


def _posix(text: str) -> str:
    """An entry as a path: ES-DE reads backslashes as separators."""
    return text.replace("\\", "/")


def _reaches(text: str, base: str, renames: Renames) -> tuple[Target | None, str | None]:
    """``(target, name)``: the rename an entry resolves to, or (None, name)
    when it doesn't resolve but names a replaced file, else (None, None)."""
    if not text:
        return None, None
    identity = file_identity(os.path.join(base, _posix(text)))
    target = renames.target_for(identity)
    if target is not None:
        return target, named_file(text)
    name = named_file(text.strip())
    if identity is None and name in renames.names:
        return None, name
    return None, None


def _new_text(text: str, base: str, target: Target, *, strict: bool) -> str | None:
    """The entry rewritten to reach the output, in its own style, or None.

    Its ``./`` prefix and folder part are kept, and the step from the
    replaced file's folder to the output appended; the tidy (normalised) form
    first, then the literal one, which still resolves through a symlinked
    folder part. Written back with backslashes when the entry used them.
    """
    backslashed = "\\" in text and "/" not in text
    posix = _posix(text)
    prefix = "./" if posix.startswith("./") else ""
    folder = posix[len(prefix):].rpartition("/")[0]
    step = os.path.relpath(target.new, target.old_dir)
    literal = f"{folder}/{step}" if folder else step
    candidates = [literal] if "/" not in step else [os.path.normpath(literal), literal]
    for candidate in dict.fromkeys(candidates):
        written = prefix + candidate
        if any(unicodedata.category(ch) == "Cc" for ch in written):
            continue
        if points_at(base, written, target, strict=strict):
            return written.replace("/", "\\") if backslashed else written
    return None


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rewrite(
    gamelist: str, data: bytes, base: str, renames: Renames, *, strict: bool,
) -> tuple[bytes, list[tuple[str, str]]]:
    """*data* with every entry that reaches a replaced file moved to its
    output, and the ``(old, new)`` entry texts. Only those text spans change."""
    entries = _entries(gamelist, data)
    codec = _codec(data)
    out: list[bytes] = []
    edits: list[tuple[str, str]] = []
    moved: dict[int, Target] = {}
    position = 0
    for number, entry in enumerate(entries, start=1):
        target, named = _reaches(entry.text, base, renames)
        if target is None:
            if named is not None:
                raise ReferrerError(
                    gamelist, f"<path> {number} names {named} but doesn't point at it",
                )
            continue
        if entry.span is None or codec is None:
            raise ReferrerError(
                gamelist, f"<path> {number} ({entry.text}) can't be edited in place",
            )
        new = _new_text(entry.text, base, target, strict=strict)
        if new is None:
            raise ReferrerError(
                gamelist, f"<path> {number}: can't write a path that reaches the output",
            )
        begin, end = entry.span
        out += [data[position:begin], _escape(new).encode(codec, "xmlcharrefreplace")]
        position = end
        edits.append((entry.text, new))
        moved[number - 1] = target
    if not edits:
        return data, []
    result = b"".join(out) + data[position:]
    _check_read_back(gamelist, base, entries, result, moved, strict)
    return result, edits


def _check_read_back(
    gamelist: str, base: str, before: list[_Entry], after: bytes,
    moved: dict[int, Target], strict: bool,
) -> None:
    """Parse the bytes about to be written: the same entries, every untouched
    one unchanged, every rewritten one reaching its output. Else keep the Source."""
    try:
        new = _entries(gamelist, after)
    except ReferrerError as exc:
        raise ReferrerError(gamelist, "the rewritten gamelist wouldn't parse") from exc
    ok = len(new) == len(before) and all(
        points_at(base, _posix(new[i].text), moved[i], strict=strict) if i in moved
        else new[i].text == before[i].text
        for i in range(len(new))
    )
    if not ok:
        raise ReferrerError(gamelist, "the rewritten gamelist wouldn't read back to the output")


def _gamelists(directory: str) -> list[str]:
    """The ``gamelist.xml`` directly in *directory*, if there is one (a
    symlink counts, so planning refuses it rather than skipping it)."""
    path = os.path.join(directory, GAMELIST_NAME)
    return [path] if os.path.islink(path) or os.path.isfile(path) else []


class GamelistReferrer(Referrer):
    id = "gamelist"
    label = "gamelist.xml"
    opt_in = True
    phase = Phase.FILE

    def detect(self, image_path: str) -> bool:
        return any(_gamelists(d) for d in search_dirs(image_path))

    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        targets = Renames.of(renames)
        changes = []
        for directory in dirs_for(renames):
            for gamelist in _gamelists(directory):
                if not may_read(gamelist):
                    continue
                with target_lock(gamelist):
                    _, edits = _rewrite(
                        gamelist, read_target(gamelist), directory, targets, strict=False,
                    )
                if edits:
                    changes.append(ReferrerChange(
                        referrer=self.id, target=gamelist, edits=tuple(edits),
                        payload=(directory, targets),
                    ))
        return changes

    def apply(self, change: ReferrerChange) -> None:
        path = change.target
        base, targets = change.payload

        def rewrite(original: bytes) -> bytes | None:
            # strict: the output must exist now, however late this runs.
            updated, edits = _rewrite(path, original, base, targets, strict=True)
            return updated if edits else None

        rewrite_in_place(path, rewrite, wait=GAMELIST_LOCK_WAIT_SECONDS)
