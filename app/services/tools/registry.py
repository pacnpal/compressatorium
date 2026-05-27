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
        # Validate before mutating so a duplicate can't leave the registry in
        # a partially-registered state (future phases iterate registry.all()).
        if tool.id in self._tools:
            raise ValueError(f"duplicate tool id {tool.id}")
        for m in tool.modes:
            if m.mode in self._by_mode:
                raise ValueError(f"duplicate mode {m.mode}")
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

    def tools_for_input(self, filename: str) -> list[ToolPlugin]:
        ext = Path(filename).suffix.lower()
        return [t for t in self._tools.values() if ext in t.input_extensions]

    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        ext = Path(path).suffix.lower()
        return next(
            (t for t in self._tools.values() if ext in t.verify_extensions),
            None,
        )
