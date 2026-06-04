"""Tool plugin registry: the single place first-party tools are registered.

Consumers import ``registry`` and ask it for the tool that handles a mode /
input / verify target instead of branching on tool identity.
"""
from __future__ import annotations

from config import settings

from .base import BaseTool, ToolPlugin
from .chdman import ChdmanTool
from .dolphin import DolphinTool
from .maxcso import MaxcsoTool
from .nsz import NszTool
from .registry import ToolRegistry
from .romz import RomzTool
from .spec import ModeKind, ModeSpec
from .z3ds import Z3dsTool

registry = ToolRegistry()
registry.register(ChdmanTool(settings.chdman_path))
registry.register(DolphinTool(settings.dolphin_tool_path))
registry.register(Z3dsTool(settings.z3ds_compressor_path))
registry.register(NszTool(settings.nsz_path))
registry.register(MaxcsoTool(settings.maxcso_path))
registry.register(RomzTool(settings.sevenzip_path))

__all__ = [
    "BaseTool",
    "ModeKind",
    "ModeSpec",
    "ToolPlugin",
    "ToolRegistry",
    "registry",
]
