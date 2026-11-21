"""The level control follows the codec a job runs with (#363).

``registry.levelRangeFor(mode, codec)`` is the one rule the Rule editor and the
convert panel read: the codec is the given one, or the mode's default when
blank, and a codec that takes no level (Dolphin's ``none``, WIA's default)
gives null, which hides the level control. It used to fall back to the tool's
1-22, so a WIA Rule on "Tool default (none)" offered "1-22. Blank uses 19."
Evaluates the shipped ``registry.js`` with Node; skipped without it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_registry_derives_186 import _find_node, loaded_registry_src

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


def _ranges(tmp_path: Path, cases: dict[str, tuple[str, str | None]]) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend registry")
    registry_src = loaded_registry_src()
    script = tmp_path / "level_range_eval.mjs"
    script.write_text(
        registry_src
        + f"\nconst cases = {json.dumps(cases)};\n"
        + "process.stdout.write(JSON.stringify(Object.fromEntries("
        + "Object.entries(cases).map(([k, [m, c]]) => [k, registry.levelRangeFor(m, c)]))));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate registry.js via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_the_level_range_is_the_running_codecs(tmp_path):
    data = _ranges(tmp_path, {
        "wia_default": ("dolphin_wia", None),
        "wia_none": ("dolphin_wia", "none"),
        "gcz": ("dolphin_gcz", None),
        "rvz_default": ("dolphin_rvz", None),
        "rvz_lzma2": ("dolphin_rvz", "lzma2"),
        "wia_lzma2": ("dolphin_wia", "lzma2"),
        # A mode with one range for every codec keeps the tool's.
        "nsz_default": ("nsz_compress", None),
    })

    assert data["wia_default"] is None
    assert data["wia_none"] is None
    assert data["gcz"] is None
    assert data["rvz_default"] == {"min": 1, "max": 22, "default": 19}
    assert data["rvz_lzma2"] == {"min": 1, "max": 9, "default": 5}
    assert data["wia_lzma2"] == {"min": 1, "max": 9, "default": 5}
    assert data["nsz_default"] == {"min": 1, "max": 22, "default": 18}


def test_both_level_controls_show_only_for_a_codec_that_takes_a_level():
    """A console's settings and the convert panel share one picker, which gates
    the level box on that range and takes its bounds from it."""
    advanced = (_SRC / "components" / "library" / "RuleAdvanced.svelte").read_text(
        encoding="utf-8",
    )
    picker = (_SRC / "components" / "panels" / "CompressionPicker.svelte").read_text(
        encoding="utf-8",
    )
    store = (_SRC / "stores" / "conversion.svelte.js").read_text(encoding="utf-8")

    assert "<CompressionPicker" in advanced and "value={fromRule(rule)}" in advanced
    assert "onchange={(v) => onupdate(toRule(rule.mode, v))}" in advanced
    assert "const levelRange = $derived(rangeOf(mode, value));" in picker
    assert "{#if levelRange}" in picker
    assert "return registry.levelRangeFor(this.mode, this.singleCodec);" in store
