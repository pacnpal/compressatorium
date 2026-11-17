"""A component must declare every prop its callers pass (#412).

Svelte 5 drops a prop the child's ``$props()`` does not name, without a
warning. ``Checkbox`` had no ``onchange``, so the nine handlers the Rule editor
passed never ran and its toggles saved nothing. ``tests/support/undeclared_props.mjs``
parses every component under ``src/``, matches each ``<Component .../>`` use to
the child's ``$props()`` and lists the attributes the child cannot receive.
Needs Node and the installed ``svelte`` package; skipped without them.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_registry_derives_186 import _find_node

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "tests" / "support" / "undeclared_props.mjs"
_CHECKBOX = _ROOT / "src" / "lib" / "components" / "ui" / "Checkbox.svelte"


def _undeclared() -> list[dict]:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to parse the Svelte components")
    if not (_ROOT / "node_modules" / "svelte").is_dir():
        pytest.skip("svelte is not installed (run npm install)")
    proc = subprocess.run(
        [node, str(_SCRIPT), str(_ROOT / "src")],
        capture_output=True, text=True, timeout=120, cwd=_ROOT,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not parse the components:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_every_prop_a_caller_passes_is_declared():
    found = _undeclared()
    assert not found, "props passed but never declared (Svelte drops them): " + "; ".join(
        f"{f['file']}:{f['line']} passes {f['prop']} to {f['component']}" for f in found
    )


def test_checkbox_calls_onchange_with_the_new_value():
    src = _CHECKBOX.read_text(encoding="utf-8")
    assert "onchange" in src.split("$props()")[0].split("let {")[-1]
    assert "onchange?.(checked)" in src or "onchange?.(" in src
