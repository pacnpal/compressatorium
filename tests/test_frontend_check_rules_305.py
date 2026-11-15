"""Guards for the check-Rule form in the rule editor and the not-Intact Finding (#305).

The editor's choice between "off", a conversion and a check is a pure helper
(``src/lib/rules/checkRule.js``), asserted behaviourally with Node. The wiring
between components is asserted against the source, as the other Rules view
tests do.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_rules_view import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src"
_HELPER = _SRC / "lib" / "rules" / "checkRule.js"
_EDITOR = _SRC / "lib" / "components" / "views" / "RuleEditor.svelte"
_FINDINGS = _SRC / "lib" / "components" / "views" / "FindingsView.svelte"
_JOB_ROW = _SRC / "lib" / "components" / "panels" / "JobRow.svelte"
_SHOTS = Path(__file__).resolve().parents[1] / "shots.yml"


def _run(script: str):
    node = _find_node()
    if node is None:
        pytest.skip("node is not installed")
    src = _HELPER.read_text(encoding="utf-8").replace("export const ", "const ")
    src = src.replace("export function ", "function ")
    out = subprocess.run(
        [node, "-e", f"{src}\nconsole.log(JSON.stringify(({script})()));"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_a_stored_rule_with_no_mode_shows_as_a_check_rule() -> None:
    got = _run("""() => [
      selectValue({ mode: '' }, true),
      selectValue({ mode: '' }, false),
      selectValue({ mode: 'createcd' }, true),
      isCheckRule({ mode: '' }, true),
      isCheckRule({ mode: '' }, false),
    ]""")
    # Configured with no target: a check Rule. Not configured: off.
    assert got == [":check", "", "createcd", True, False]


def test_choosing_check_keeps_the_schedule_and_drops_the_conversion() -> None:
    got = _run("""() => checkRuleFrom(
      { mode: 'createcd', compression: 'zstd', compression_level: 19, split: true,
        window_start: '01:00', window_end: '05:00', days: [1], enabled: true,
        check_budget_gb: 50, delete_on_verify: true },
    )""")
    assert got["mode"] == ""
    assert (got["window_start"], got["window_end"], got["days"]) == ("01:00", "05:00", [1])
    assert got["check_budget_gb"] == 50
    assert got["compression"] is None and got["compression_level"] is None
    assert got["delete_on_verify"] is False


def test_the_summary_says_the_budget() -> None:
    assert _run("() => checkSummary({ check_budget_gb: 50 })") == "50 GB a night"


def test_the_editor_offers_the_check_rule_form() -> None:
    src = _EDITOR.read_text(encoding="utf-8")
    assert "CHECK_RULE" in src
    assert "check_budget_gb" in src
    # Budget, window and weekdays: the window and weekdays come from the one
    # snippet both forms render.
    assert "{@render schedule(" in src
    assert src.count("{@render schedule(") >= 2
    # "Off" removes the Rule; a check Rule has no mode and must not be removed.
    assert "if (!rule.mode) source.removeRule" not in src


def test_a_not_intact_finding_has_a_label_and_no_set_platform() -> None:
    src = _FINDINGS.read_text(encoding="utf-8")
    assert "not_intact: 'Not Intact'" in src
    assert "row.kind !== 'not_intact'" in src


def test_a_check_job_shows_its_name_and_result() -> None:
    src = _JOB_ROW.read_text(encoding="utf-8")
    assert "EXTERNAL_MODE_LABELS" in src


def test_shots_capture_the_check_rule_form() -> None:
    assert "rule-editor-check" in _SHOTS.read_text(encoding="utf-8")
