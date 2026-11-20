"""Wiring guards for the Rule check level in the console table (#302).

The backend decides what a check level does (tests/test_rules_check_level_302.py).
These check that a console's settings offer it next to delete-original, and
that the table names the proof each mode uses. The one-time upgrade notice
and the per-run report are gone (#541).
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_LIBRARY = _SRC / "lib" / "components" / "library"
_TABLE = _LIBRARY / "RuleTable.svelte"
_ADVANCED = _LIBRARY / "RuleAdvanced.svelte"
_SAFETY = _SRC / "lib" / "util" / "safetyOptions.js"
_LEVELS = _SRC / "lib" / "util" / "checkLevels.js"


def test_the_check_level_sits_next_to_delete_original():
    src = _ADVANCED.read_text(encoding="utf-8")
    # Offered only where it decides something, as in the convert panel: the
    # shared rule says delete on, a Round-trip mode, not a Recognized one.
    assert "deleteOriginal: !!rule.delete_on_verify" in src
    assert "{#if quick}" in src
    assert "rule.check_level === CHECK_LEVEL.containerOnly" in src
    assert "check_level: v ? CHECK_LEVEL.containerOnly : CHECK_LEVEL.roundTrip" in src
    safety = _SAFETY.read_text(encoding="utf-8")
    assert "showQuick: deleteOriginal && list.some((s) => s.supportsRoundTrip && !s.roundTripRequired)" in safety
    # The delete column names the proof: the convert panel's helper
    # (deleteCheck.js), which also knows a Recognized mode (#423).
    table = _TABLE.read_text(encoding="utf-8")
    assert "DELETE_CHECK_HINTS[ruleDeleteCheck(spec, rule)]" in table
    assert "containerOnly: rule.check_level === CHECK_LEVEL.containerOnly" in table


def test_the_editor_names_no_level_string_itself():
    """One JS copy of the two levels, held to the backend's by the test below."""
    for path in (_TABLE, _ADVANCED):
        src = path.read_text(encoding="utf-8")
        assert "'round_trip'" not in src and "'container_only'" not in src
        assert "from '$lib/util/checkLevels.js'" in src


def test_the_js_levels_match_the_backend():
    from services.rules.rules import CHECK_LEVELS

    js = _LEVELS.read_text(encoding="utf-8")
    found = re.search(
        r"roundTrip: '([a-z_]+)',\s*containerOnly: '([a-z_]+)'", js,
    )
    assert found, js
    assert found.groups() == CHECK_LEVELS
