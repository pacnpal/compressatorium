"""Guards for check Rules in the rule editor and the not-Intact Finding (#305).

A Platform can have a conversion Rule and a check Rule at once. The rules
source keeps both, and saves both, asserted behaviourally with Node through
the Libraries store harness. The check-Rule helper is asserted with Node too;
the wiring between components is asserted against the source.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_rules_view import _FOLDER, _find_node
from .test_frontend_rules_view import _run as _run_store

_SRC = Path(__file__).resolve().parents[1] / "src"
_HELPER = _SRC / "lib" / "rules" / "checkRule.js"
_EDITOR = _SRC / "lib" / "components" / "views" / "RuleEditor.svelte"
_FINDINGS = _SRC / "lib" / "components" / "views" / "FindingsView.svelte"
_JOB_ROW = _SRC / "lib" / "components" / "panels" / "JobRow.svelte"
_ENDPOINTS = _SRC / "lib" / "api" / "endpoints.js"
_SHOTS = Path(__file__).resolve().parents[1] / "shots.yml"


def _run_helper(script: str):
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


def test_a_new_check_rule_keeps_the_schedule_defaults_and_drops_the_conversion() -> None:
    got = _run_helper("""() => newCheckRule(
      { mode: '', compression: null, window_start: null, window_end: null,
        days: [0, 1, 2, 3, 4, 5, 6], enabled: false, check_budget_gb: 50,
        delete_on_verify: true },
    )""")
    assert got["mode"] == ""
    assert got["enabled"] is True
    assert got["check_budget_gb"] == 50
    assert got["days"] == [0, 1, 2, 3, 4, 5, 6]
    assert got["delete_on_verify"] is False


def test_the_summary_says_the_budget() -> None:
    assert _run_helper("() => checkSummary({ check_budget_gb: 50 })") == "50 GB a night"


def test_a_platform_keeps_its_conversion_rule_and_its_check_rule(tmp_path) -> None:
    """Both kinds load, edit and save together; neither replaces the other."""
    data = _run_store(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
const loading = source.loadRules();
__calls[0].resolve({{
  rules: {{ ps2: {{ mode: 'createdvd', enabled: true }} }},
  check_rules: {{ ps2: {{ mode: '', enabled: true, check_budget_gb: 50 }} }},
  state: {{}}, check_state: {{ ps2: {{ last_checks: 4 }} }},
  defaults: {{ mode: '', check_budget_gb: 50 }}, options: {{}},
}});
await loading;
const loaded = {{ rule: source.ruleFor('ps2').mode, check: source.checkRuleFor('ps2'),
  last: source.lastCheckFor('ps2') }};
source.setCheckRule('ps2', {{ ...source.checkRuleFor('ps2'), check_budget_gb: 80 }});
const saving = source.saveRules();
await tick();
__calls[1].resolve({{
  rules: {{ ps2: {{ mode: 'createdvd', enabled: true }} }},
  check_rules: {{ ps2: {{ mode: '', enabled: true, check_budget_gb: 80 }} }},
}});
await saving;
source.removeCheckRule('ps2');
process.stdout.write(JSON.stringify({{
  loaded, call: __log[1], after: source.checkRules, rules: source.rules,
  dirty: source.rulesDirty,
}}));
""")
    assert data["loaded"]["rule"] == "createdvd"
    assert data["loaded"]["check"]["check_budget_gb"] == 50
    assert data["loaded"]["last"] == {"last_checks": 4}
    # Saved with both kinds.
    name, library_id, rules, checks = data["call"]
    assert (name, library_id) == ("saveLibraryRules", "folder:3")
    assert rules["ps2"]["mode"] == "createdvd"
    assert checks["ps2"]["check_budget_gb"] == 80
    # Removing the check Rule leaves the conversion Rule.
    assert data["after"] == {}
    assert data["rules"]["ps2"]["mode"] == "createdvd"
    assert data["dirty"] is True


def test_the_endpoints_send_the_check_rules() -> None:
    src = _ENDPOINTS.read_text(encoding="utf-8")
    assert src.count("check_rules: checkRules") == 2


def test_the_editor_offers_a_check_rule_beside_the_conversion_rule() -> None:
    src = _EDITOR.read_text(encoding="utf-8")
    assert "source.setCheckRule(" in src
    assert "source.removeCheckRule(" in src
    assert "check_budget_gb" in src
    # The window and weekdays come from the one snippet both forms render.
    assert src.count("{@render schedule(") >= 2
    # A check Rule is no target format of the conversion Rule any more.
    assert "CHECK_RULE" not in src


def test_the_editor_says_how_the_budget_and_schedule_work() -> None:
    src = _EDITOR.read_text(encoding="utf-8")
    # Ruling 3: stalest first, stop at the first that does not fit, an
    # oversized Image alone. Ruling 4: daily, and Run now spends another budget.
    assert "does not fit" in src
    assert "larger than the whole budget" in src
    assert "once a day" in src
    assert "Run now" in src and "another budget" in src


def test_a_not_intact_finding_has_a_label_and_no_set_platform() -> None:
    src = _FINDINGS.read_text(encoding="utf-8")
    assert "not_intact: 'Not Intact'" in src
    assert "row.kind !== 'not_intact'" in src


def test_a_check_job_shows_its_name_and_result() -> None:
    src = _JOB_ROW.read_text(encoding="utf-8")
    assert "EXTERNAL_MODE_LABELS" in src


def test_shots_capture_the_check_rule_form() -> None:
    assert "rule-editor-check" in _SHOTS.read_text(encoding="utf-8")


def test_shots_capture_a_not_intact_finding_and_a_check_job() -> None:
    """M10: both surfaces are captured and shown where the README describes check Rules."""
    shots = _SHOTS.read_text(encoding="utf-8")
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Check Rules", 1)[1].split("\n### ", 1)[0]
    for name in ("findings-not-intact", "jobs-check"):
        for theme in ("light", "dark"):
            image = f"docs/screenshots/{name}-{theme}.png"
            assert image in shots, image
            assert image in section, image
