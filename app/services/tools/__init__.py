"""Tool plugin registry: the single place first-party tools are registered.

Consumers import ``registry`` and ask it for the tool that handles a mode /
input / verify target instead of branching on tool identity.
"""
from __future__ import annotations

from config import settings

from .base import BaseTool, ToolPlugin, not_ready_message
from .chain import ChainTool
from .chdman import ChdmanTool
from .dolphin import DolphinTool
from .exodos import ExodosTool
from .jwud import JwudTool
from .makeps3iso import MakePs3IsoTool
from .maxcso import MaxcsoTool
from .nkit2iso import Nkit2IsoTool
from .nsz import NszTool
from .registry import ToolRegistry
from .rom_converto import RomConvertoTool
from .romz import RomzTool
from .spec import ChainSpec, ChainStep, DeleteProof, InputKind, ModeKind, ModeSpec
from .xiso import XisoTool
from .z3ds import Z3dsTool

registry = ToolRegistry()
registry.register(ChdmanTool(settings.chdman_path))
registry.register(DolphinTool(settings.dolphin_tool_path))
registry.register(Z3dsTool(settings.z3ds_compressor_path))
registry.register(NszTool(settings.nsz_path))
registry.register(MaxcsoTool(settings.maxcso_path))
registry.register(RomzTool(settings.sevenzip_path))
registry.register(MakePs3IsoTool(settings.makeps3iso_path))
registry.register(Nkit2IsoTool(settings.nkit2iso_path))
registry.register(JwudTool(settings.jwudtool_path))
registry.register(XisoTool(settings.extract_xiso_path))
registry.register(RomConvertoTool(settings.rom_converto_path))
# Pure Python: no binary to locate, gated on EXODOS_DOS_DIR instead.
registry.register(ExodosTool(""))
# Composite/pipeline modes register last: ChainTool drives the component tools
# above through the registry, so they must already be present.
registry.register(ChainTool(registry))

__all__ = [
    "BaseTool",
    "ChainSpec",
    "ChainStep",
    "DeleteProof",
    "InputKind",
    "ModeKind",
    "ModeSpec",
    "ToolPlugin",
    "ToolRegistry",
    "not_ready_message",
    "registry",
]
