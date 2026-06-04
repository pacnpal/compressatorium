"""In-process registry of first-party conversion tools.

Dispatch sites (job_manager, convert/files/info routes) ask the registry for
the tool that handles a mode / input / verify target instead of branching on
tool identity. No dynamic or third-party plugin discovery: tools are registered
explicitly in ``__init__.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ToolPlugin
    from .spec import ModeSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolPlugin] = {}
        self._by_mode: dict[str, ToolPlugin] = {}

    def register(self, tool: ToolPlugin) -> None:
        # Validate fully before mutating so a bad tool can't leave the registry
        # in a partially-registered state (future phases iterate registry.all()).
        if tool.id in self._tools:
            raise ValueError(f"duplicate tool id {tool.id}")
        seen: set[str] = set()
        for m in tool.modes:
            if m.tool_id != tool.id:
                raise ValueError(
                    f"mode {m.mode} declares tool_id {m.tool_id!r} "
                    f"but belongs to tool {tool.id!r}"
                )
            if m.mode in self._by_mode or m.mode in seen:
                raise ValueError(f"duplicate mode {m.mode}")
            seen.add(m.mode)
        self._tools[tool.id] = tool
        for m in tool.modes:
            self._by_mode[m.mode] = tool

    def all(self) -> list[ToolPlugin]:
        return list(self._tools.values())

    def get(self, tool_id: str) -> ToolPlugin:
        return self._tools[tool_id]

    def for_mode(self, mode: str) -> ToolPlugin:
        return self._by_mode[mode]

    def spec(self, mode: str) -> ModeSpec:
        return self.for_mode(mode).spec(mode)

    def mode_specs(self) -> list[ModeSpec]:
        return [m for t in self._tools.values() for m in t.modes]

    def convertible_extensions(self) -> frozenset[str]:
        return frozenset().union(
            *(t.input_extensions for t in self._tools.values())
        )

    def archive_input_extensions(self) -> frozenset[str]:
        """Input extensions accepted by at least one mode that allows
        archive members as input.

        Used by the archive listing to decide which members inside a
        ``.zip`` / ``.7z`` / ``.rar`` are worth surfacing. Driving this off
        the same mode specs that gate ``plan_job`` keeps the listing and the
        conversion path in lockstep: a member is only listed as convertible
        when some mode could actually accept it from an archive. Previously
        the listing hard-coded CHDMAN's source set, so 3DS members were
        silently hidden even though z3ds could compress them (issue #113).
        """
        exts: set[str] = set()
        for tool in self._tools.values():
            for mode in tool.modes:
                if mode.allows_archive_input:
                    exts |= set(mode.input_extensions)
        return frozenset(exts)

    def archive_listable_extensions(self) -> frozenset[str]:
        """Extensions surfaced when browsing INTO an archive.

        Superset of :meth:`archive_input_extensions`: a member is listed when
        some mode can convert it in place (``allows_archive_input``) OR when a
        tool just wants it visible without an in-place conversion action
        (``lists_archive_members``, e.g. romz single-ROM archives). Decouples
        "show this member" from "convert this member" so listing a romz ROM
        doesn't make ``plan_job`` accept ``archive.zip::game.gb`` and recompress
        a ROM that's already archived (recursive).
        """
        exts: set[str] = set()
        for tool in self._tools.values():
            for mode in tool.modes:
                if mode.allows_archive_input or mode.lists_archive_members:
                    exts |= set(mode.input_extensions)
        return frozenset(exts)

    def tools_accepting_archive_member(self, ext: str) -> list[str]:
        """Tool ids with at least one ``allows_archive_input`` mode for ``ext``.

        The archive-member analogue of ``ext in tool.input_extensions``: a tool
        is only convertible-in-place when some mode of it both takes the ext AND
        allows archive input. romz lists members (``lists_archive_members``) but
        has no ``allows_archive_input`` mode, so it is correctly excluded — the
        ROM is visible but carries no in-place conversion affordance.
        """
        ext = ext.lower()
        return [
            tool.id
            for tool in self._tools.values()
            if any(
                mode.allows_archive_input and ext in mode.input_extensions
                for mode in tool.modes
            )
        ]

    def tools_for_input(self, filename: str) -> list[ToolPlugin]:
        ext = Path(filename).suffix.lower()
        return [t for t in self._tools.values() if ext in t.input_extensions]

    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        ext = Path(path).suffix.lower()
        return next(
            (t for t in self._tools.values() if ext in t.verify_extensions),
            None,
        )

    def tools_verifying_path(self, path: str) -> list[ToolPlugin]:
        """Tools whose verify/info applies to a concrete file path.

        The per-file companion to :meth:`tool_for_verify`: each tool refines
        the plain extension match via ``verifies_path`` (romz inspects archive
        members so a non-single-ROM ``.7z``/``.zip`` is excluded). Returns
        every claiming tool, in registration order, so the file listing can
        surface a tool-neutral ``verifiable_by`` flag the frontend gates the
        Verify/Info row-actions on. May do disk I/O (archive inspection); call
        it off the event loop.
        """
        return [t for t in self._tools.values() if t.verifies_path(path)]

    def verify_extensions(self) -> frozenset[str]:
        """Union of every registered tool's verify_extensions.

        Used by file rename/delete handlers to decide whether the path
        carries a verification record worth clearing, historically the
        check hard-coded `.chd`, which left .rvz / .z3ds / etc. records
        orphaned in the persistent store when the file was removed.
        """
        return frozenset().union(
            *(t.verify_extensions for t in self._tools.values())
        )

    def output_extensions(self) -> frozenset[str]:
        """Union of every registered tool's output_extensions.

        These are the file types the tools *produce* (.chd, .rvz, .nsz, ...).
        """
        return frozenset().union(
            *(t.output_extensions for t in self._tools.values())
        )

    def scannable_extensions(self) -> frozenset[str]:
        """Extensions the library metadata scan should walk for.

        The union of produced outputs and verifiable inputs, so every
        registered tool's outputs are eligible for discovery rather than
        the historical hard-coded ``.chd`` walk (issue #131).
        """
        return self.output_extensions() | self.verify_extensions()
