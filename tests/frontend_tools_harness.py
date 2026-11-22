"""Evaluate the frontend's pure tool modules with Node, the registry inlined.

``registry.js`` imports only ``api`` (stubbed) and is loaded from the
backend's own ``/api/tools`` payload; the modules built on it
(``compression.js``, ``convertTargets.js``) import only the registry and
``levels.js``. Imports are dropped and exports turned into plain declarations,
so everything runs as one script. Skips the calling test without Node.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_registry_derives_186 import _find_node, _strip_imports, loaded_registry_src

_TOOLS = Path(__file__).resolve().parents[1] / "src" / "lib" / "tools"


def _unexport(src: str) -> str:
    return re.sub(r"^export (const|function|class) ", r"\1 ", src, flags=re.MULTILINE)


def _plain(name: str) -> str:
    return _unexport(_strip_imports((_TOOLS / name).read_text(encoding="utf-8")))


def run_tools(tmp_path: Path, modules: list[str], body: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend tools")
    script = tmp_path / "tools_eval.mjs"
    # The registry loaded from the backend's payload, as App.svelte loads it.
    parts = [_unexport(loaded_registry_src()), _plain("levels.js")]
    parts += [_plain(m) for m in modules]
    script.write_text("\n".join(parts) + "\n" + body, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)
