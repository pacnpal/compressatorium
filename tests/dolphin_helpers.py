"""Reading a dolphin-tool command the way dolphin-tool does (#345).

A conversion's argv can start with a priority prefix: on Linux with `ionice`,
`ioprio_prefix()` puts `ionice -c 2 -n …` in front, so a plain search for
`-c` finds ionice's class, not Dolphin's codec. dolphin-tool's own options are
the ones after its binary.
"""
from __future__ import annotations

from services.tools import registry


def dolphin_option(cmd: list[str], flag: str) -> str | None:
    """The value of dolphin-tool's *flag* (``-c``, ``-l``) in *cmd*, or None.

    Read only from the arguments after the dolphin-tool binary (the
    configured ``dolphin_tool_path``), whatever prefix comes before it.
    """
    binary = registry.get("dolphin")._service.dolphin_tool_path
    args = cmd[cmd.index(binary) + 1:]
    return args[args.index(flag) + 1] if flag in args else None
