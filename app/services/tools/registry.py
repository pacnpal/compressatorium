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

    def tools_for_input(self, filename: str) -> list[ToolPlugin]:
        ext = Path(filename).suffix.lower()
        return [t for t in self._tools.values() if ext in t.input_extensions]

    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        ext = Path(path).suffix.lower()
        return next(
            (t for t in self._tools.values() if ext in t.verify_extensions),
            None,
        )

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
