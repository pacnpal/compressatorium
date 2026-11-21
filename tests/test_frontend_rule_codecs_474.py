"""The Rule editor shows and counts only the codecs the Rule's mode takes (#474).

A Rule saved before #471 with Create DVD and cdzl showed no chip pressed (the
DVD chips have no cdzl) yet said "(1 selected)", so the operator could neither
see nor clear the codec. ``registry.offeredCodecs(mode, selection)`` is #471's
narrowing, lifted out of the convert store so the Rule editor and the convert
panel read one rule. Evaluates the shipped ``registry.js`` with Node; skipped
without it. The wiring into the components is asserted against the source.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from .test_frontend_registry_derives_186 import _find_node, loaded_registry_src

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"
_ADVANCED = _SRC / "components" / "library" / "RuleAdvanced.svelte"
_TABLE = _SRC / "components" / "library" / "RuleTable.svelte"
_COMPRESSION = _SRC / "tools" / "compression.js"
_STORE = _SRC / "stores" / "conversion.svelte.js"


def _offered(tmp_path: Path, cases: dict[str, tuple[str, list[str]]]) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend registry")
    registry_src = loaded_registry_src()
    script = tmp_path / "offered_codecs_eval.mjs"
    script.write_text(
        registry_src
        + f"\nconst cases = {json.dumps(cases)};\n"
        + "process.stdout.write(JSON.stringify(Object.fromEntries("
        + "Object.entries(cases).map(([k, [m, s]]) => [k, registry.offeredCodecs(m, s)]))));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate registry.js via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_only_the_codecs_the_mode_offers_are_kept(tmp_path):
    data = _offered(tmp_path, {
        "dvd_cd_only": ("createdvd", ["cdzl"]),
        "dvd_mixed": ("createdvd", ["zlib", "cdzl", "cdlz"]),
        "cd_keeps_cd": ("createcd", ["cdzl", "zlib"]),
        "hd_avhu": ("createhd", ["avhu", "lzma"]),
        # Copy's codecs follow the input CHD, so it names none and keeps all.
        "copy": ("copy", ["cdzl", "zlib"]),
    })

    assert data == {
        "dvd_cd_only": [],
        "dvd_mixed": ["zlib"],
        "cd_keeps_cd": ["cdzl", "zlib"],
        "hd_avhu": ["lzma"],
        "copy": ["cdzl", "zlib"],
    }


def _function(src: str, name: str) -> str:
    found = re.search(rf"function {name}\([^)]*\) \{{.*?\n\s*\}}", src, re.DOTALL)
    assert found, f"{name} not found"
    return found.group(0)


def test_the_rule_editor_counts_and_presses_only_offered_codecs():
    """A console's codecs go through the shared picker and compression.js,
    whose toggle keeps only the codecs the mode offers, so a hidden codec is
    dropped instead of kept unseen (#474)."""
    advanced = _ADVANCED.read_text(encoding="utf-8")
    assert "value={fromRule(rule)}" in advanced
    src = _COMPRESSION.read_text(encoding="utf-8")
    assert "registry.offeredCodecs(mode, value?.codecs ?? [])" in _function(src, "toggleCodec")
    assert "registry.offeredCodecs(mode, chosen)" in src


def test_the_rule_editor_says_which_codecs_were_dropped():
    src = _TABLE.read_text(encoding="utf-8")

    assert "rule.dropped_codecs?.length" in src


def test_the_convert_store_reads_the_same_narrowing():
    src = _STORE.read_text(encoding="utf-8")

    assert src.count("registry.offeredCodecs(this.mode") == 2
