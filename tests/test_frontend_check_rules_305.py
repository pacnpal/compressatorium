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
from .test_frontend_verification_store_298 import _run as _run_verification

_SRC = Path(__file__).resolve().parents[1] / "src"
_HELPER = _SRC / "lib" / "rules" / "checkRule.js"
# The console table (#541): a console's re-check sits in its More settings.
_TABLE = _SRC / "lib" / "components" / "library" / "RuleTable.svelte"
_ADVANCED = _SRC / "lib" / "components" / "library" / "RuleAdvanced.svelte"
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


def test_editing_the_window_drops_a_stale_invalid_window_flag(tmp_path) -> None:
    """4577383258: the server owns invalid_window. The store drops it once the window
    or Enabled changes, and never sends it back."""
    data = _run_store(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
const loading = source.loadRules();
__calls[0].resolve({{
  rules: {{ ps2: {{ mode: 'createdvd', enabled: false, window_start: '03:00',
    window_end: '03:00', invalid_window: true }} }},
  check_rules: {{ ps2: {{ mode: '', enabled: false, check_budget_gb: 50,
    window_start: '01:00', window_end: null, invalid_window: true }} }},
  state: {{}}, check_state: {{}}, defaults: {{ mode: '' }}, options: {{}},
}});
await loading;
source.setCheckRule('ps2', {{ ...source.checkRuleFor('ps2'), window_end: '05:00',
  enabled: true }});
source.setRule('ps2', {{ ...source.ruleFor('ps2'), enabled: true }});
const edited = {{ check: source.checkRuleFor('ps2'), rule: source.ruleFor('ps2') }};
const saving = source.saveRules();
await tick();
__calls[1].resolve({{ rules: {{}}, check_rules: {{}} }});
await saving;
process.stdout.write(JSON.stringify({{ edited, call: __log[1] }}));
""")
    assert "invalid_window" not in data["edited"]["check"]
    assert "invalid_window" not in data["edited"]["rule"]
    _name, _library_id, rules, checks = data["call"]
    assert "invalid_window" not in rules["ps2"]
    assert "invalid_window" not in checks["ps2"]
    assert checks["ps2"]["window_end"] == "05:00"


def test_an_unrelated_edit_keeps_the_flag_on_screen_but_not_in_the_save(tmp_path) -> None:
    """A budget change leaves the server's error showing until the next save answers."""
    data = _run_store(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
const loading = source.loadRules();
__calls[0].resolve({{
  rules: {{}},
  check_rules: {{ ps2: {{ mode: '', enabled: false, check_budget_gb: 50,
    window_start: '01:00', window_end: null, invalid_window: true }} }},
  state: {{}}, check_state: {{}}, defaults: {{ mode: '' }}, options: {{}},
}});
await loading;
source.setCheckRule('ps2', {{ ...source.checkRuleFor('ps2'), check_budget_gb: 80 }});
const edited = source.checkRuleFor('ps2');
const saving = source.saveRules();
await tick();
__calls[1].resolve({{ rules: {{}}, check_rules: {{}} }});
await saving;
process.stdout.write(JSON.stringify({{ edited, call: __log[1] }}));
""")
    assert data["edited"]["invalid_window"] is True
    assert "invalid_window" not in data["call"][3]["ps2"]


def test_a_check_runs_events_update_the_badges_in_order(tmp_path) -> None:
    """Codex 4578116084, 4578154653: each Image's new state arrives as it
    settles. One that failed loses its Intact badge; a newer pass that lands
    after it (a repair and re-verify) is not undone by the run's later events,
    and a conversion's own flags still apply as before."""
    data = _run_verification(tmp_path, """
const store = new VerificationStore();
store.markChecked('/v/bolt.chd', 'round_tripped');
store.markChecked('/v/dune.chd');
const run = { id: 'check1', mode: 'check' };
// The run fails Bolt...
store.applyJobEvent({ type: 'progress', job: run,
  verification_changes: { '/v/bolt.chd': null } });
const afterFail = store.checkFor('/v/bolt.chd');
// ...Bolt is repaired and re-verified (a manual Verify)...
store.markChecked('/v/bolt.chd');
// ...then the run's later events land: Cove passes, and the run ends.
store.applyJobEvent({ type: 'progress', job: run,
  verification_changes: { '/v/cove.chd': 'intact' } });
store.applyJobEvent({ type: 'complete', job: run });
// A conversion's complete still applies its own flags.
store.applyJobEvent({
  type: 'complete', verified: true, check_passed: 'round_tripped',
  job: { output_path: '/v/new.rvz', file_path: '/v/new.iso' },
});
process.stdout.write(JSON.stringify({
  afterFail,
  bolt: store.checkFor('/v/bolt.chd'),
  cove: store.checkFor('/v/cove.chd'),
  dune: store.checkFor('/v/dune.chd'),
  fresh: store.checkFor('/v/new.rvz'),
}));
""")
    assert data == {
        "afterFail": None, "bolt": "intact", "cove": "intact", "dune": "intact",
        "fresh": "round_tripped",
    }


def test_a_failure_during_a_reload_survives_the_older_snapshot(tmp_path) -> None:
    """Codex 4578598280: a reload (page load, SSE reconnect) is in flight when
    a check run says X is no longer Intact. The older snapshot still lists X;
    replaying it must not bring the badge back. Additions and changes made
    meanwhile survive the same way."""
    data = _run_verification(tmp_path, """
const store = new VerificationStore();
store.markChecked('/v/x.chd');
store.markChecked('/v/y.chd');
const load = store.loadVerified();
store.applyJobEvent({ type: 'progress', job: { id: 'check1' },
  verification_changes: { '/v/x.chd': null } });
store.markChecked('/v/z.rvz', 'round_tripped');
store.markChecked('/v/y.chd', 'round_tripped');
// The server's answer predates all three.
__resolve({
  verified: ['/v/x.chd', '/v/y.chd', '/v/w.chd'],
  checks: { '/v/x.chd': 'intact', '/v/y.chd': 'intact', '/v/w.chd': 'intact' },
});
await load;
process.stdout.write(JSON.stringify({
  x: store.checkFor('/v/x.chd'),
  y: store.checkFor('/v/y.chd'),
  z: store.checkFor('/v/z.rvz'),
  w: store.checkFor('/v/w.chd'),
}));
""")
    assert data == {"x": None, "y": "round_tripped", "z": "round_tripped", "w": "intact"}


def test_a_run_whose_progress_events_were_dropped_is_caught_up_at_its_end(tmp_path) -> None:
    """Codex 4578664511: every per-Image event is lost (a full stream queue).
    The terminal event lists the paths the run settled; the store re-reads
    just those from the database, and the failed Image loses its badge, while
    a live event that lands during the re-read still wins."""
    data = _run_verification(tmp_path, """
let __asked = null;
let __answer;
api.getVerifiedFor = (paths) => {
  __asked = paths;
  return new Promise((resolve) => { __answer = resolve; });
};
const store = new VerificationStore();
store.markChecked('/v/bolt.chd');
store.markChecked('/v/cove.chd');
store.markChecked('/v/keep.chd');
// The run's progress events never arrive; its end does.
store.applyJobEvent({ type: 'complete', job: { id: 'check1' },
  verification_paths: ['/v/bolt.chd', '/v/cove.chd', '/v/new.chd'] });
// While the re-read is out, a newer pass on Cove lands live.
store.markChecked('/v/cove.chd', 'round_tripped');
// The database says: Bolt failed, Cove passed (an older state), New passed.
__answer({ verified: ['/v/cove.chd', '/v/new.chd'],
           checks: { '/v/cove.chd': 'intact', '/v/new.chd': 'intact' } });
await store.pendingLookups();
process.stdout.write(JSON.stringify({
  asked: __asked,
  bolt: store.checkFor('/v/bolt.chd'),
  cove: store.checkFor('/v/cove.chd'),
  fresh: store.checkFor('/v/new.chd'),
  keep: store.checkFor('/v/keep.chd'),
}));
""")
    assert data == {
        "asked": ["/v/bolt.chd", "/v/cove.chd", "/v/new.chd"],
        "bolt": None, "cove": "round_tripped", "fresh": "intact", "keep": "intact",
    }


def test_an_older_full_load_never_undoes_a_newer_lookup(tmp_path) -> None:
    """Codex 4579128146: a full reload is still out when a check run's end
    re-reads its paths. The lookup removes a failed Image and lands first;
    the older full answer, which still lists it, must not bring it back.
    Responses only replace state older than their own request."""
    data = _run_verification(tmp_path, """
let __answerLookup;
api.getVerifiedFor = () => new Promise((resolve) => { __answerLookup = resolve; });
const store = new VerificationStore();
store.markChecked('/v/bolt.chd');
store.markChecked('/v/keep.chd');
const full = store.loadVerified();              // the older request
store.applyJobEvent({ type: 'complete', job: { id: 'check1' },
  verification_paths: ['/v/bolt.chd'] });       // the newer one
__answerLookup({ verified: [], checks: {} });   // Bolt failed: lands first
await store.pendingLookups();
const afterLookup = store.checkFor('/v/bolt.chd');
__resolve({ verified: ['/v/bolt.chd', '/v/keep.chd', '/v/new.chd'],
            checks: { '/v/bolt.chd': 'intact', '/v/keep.chd': 'intact',
                      '/v/new.chd': 'intact' } });
await full;
process.stdout.write(JSON.stringify({
  afterLookup,
  bolt: store.checkFor('/v/bolt.chd'),
  keep: store.checkFor('/v/keep.chd'),
  fresh: store.checkFor('/v/new.chd'),
}));
""")
    assert data == {"afterLookup": None, "bolt": None, "keep": "intact", "fresh": "intact"}


def test_an_older_lookup_never_undoes_a_newer_full_load(tmp_path) -> None:
    """Codex 4579151542, the reverse order: a lookup is still out when a newer
    full reload answers first and says the path has no check. A full answer
    covers every path, even one it never names, so the older lookup that
    lands after it cannot bring the badge back. A live write after the full
    answer still applies."""
    data = _run_verification(tmp_path, """
let __answerLookup;
api.getVerifiedFor = () => new Promise((resolve) => { __answerLookup = resolve; });
const store = new VerificationStore();
store.markChecked('/v/bolt.chd');
store.forget('/v/bolt.chd');
store.applyJobEvent({ type: 'complete', job: { id: 'check1' },
  verification_paths: ['/v/bolt.chd'] });       // the older request
const full = store.loadVerified();              // the newer one
__resolve({ verified: [], checks: {} });        // lands first: no checks
await full;
__answerLookup({ verified: ['/v/bolt.chd'], checks: { '/v/bolt.chd': 'intact' } });
await store.pendingLookups();
const bolt = store.checkFor('/v/bolt.chd');
store.markChecked('/v/later.rvz', 'round_tripped');  // live, after the full answer
process.stdout.write(JSON.stringify({ bolt, later: store.checkFor('/v/later.rvz') }));
""")
    assert data == {"bolt": None, "later": "round_tripped"}


def test_the_jobs_stream_hands_its_job_events_to_the_verification_store() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "lib" / "stores"
           / "jobs.svelte.js").read_text(encoding="utf-8")
    # progress (a check run's per-Image changes); complete; error/cancelled.
    assert src.count("verification.applyJobEvent(data)") == 3


def test_the_endpoints_send_the_check_rules() -> None:
    src = _ENDPOINTS.read_text(encoding="utf-8")
    assert src.count("check_rules: checkRules") == 2


def test_the_editor_offers_a_check_rule_beside_the_conversion_rule() -> None:
    table = _TABLE.read_text(encoding="utf-8")
    advanced = _ADVANCED.read_text(encoding="utf-8")
    assert "source.setCheckRule(" in table
    assert "source.removeCheckRule(" in table
    assert "check_budget_gb" in advanced
    # The window and weekdays come from the one snippet both forms render.
    assert advanced.count("{@render schedule(") >= 2
    # A check Rule is no target format of the conversion Rule any more.
    assert "CHECK_RULE" not in table + advanced


def _check_rules_section() -> str:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    return readme.split("### Check Rules", 1)[1].split("\n### ", 1)[0]


def test_the_readme_says_how_the_budget_and_schedule_work() -> None:
    """The table drops the long hints (#541); the README keeps the rulings."""
    section = _check_rules_section()
    # Ruling 3: stalest first, stop at the first that does not fit, an
    # oversized Image alone. Ruling 4: daily, and Run now spends another budget.
    assert "does not fit" in section
    assert "larger than the whole budget" in section
    assert "once a day" in section
    assert "Run now" in section and "another budget" in section


def test_the_editor_and_readme_say_an_equal_window_is_all_day() -> None:
    """r2 M1: 03:00-03:00 is the whole day for a check Rule, and both places say so."""
    assert "same start and end" in _ADVANCED.read_text(encoding="utf-8")
    assert "same start and end" in _check_rules_section()


def test_only_the_check_form_offers_an_equal_window_as_all_day() -> None:
    """r3 I1: the shared schedule fields say "all day" only for a check Rule; a
    conversion Rule with an equal pair is paused and says why."""
    src = _ADVANCED.read_text(encoding="utf-8")
    assert "{#snippet schedule(target, change, equalIsAllDay)}" in src
    assert "{@render schedule(check, oncheck, true)}" in src
    assert "{@render schedule(rule, onupdate, false)}" in src
    assert "starts and ends at the same time" in _TABLE.read_text(encoding="utf-8")


def test_a_not_intact_finding_has_a_label_and_no_set_platform() -> None:
    src = _FINDINGS.read_text(encoding="utf-8")
    assert "not_intact: 'Not Intact'" in src
    assert "row.kind !== 'not_intact'" in src


def test_a_check_job_shows_its_name_and_result() -> None:
    src = _JOB_ROW.read_text(encoding="utf-8")
    assert "EXTERNAL_MODE_LABELS" in src
    # #306's detail lines: a completed job with no output shows its message.
    detail = (_SRC / "lib" / "util" / "jobDetail.js").read_text(encoding="utf-8")
    assert "if (!lines.length && job?.message)" in detail


def test_shots_capture_the_check_rule_form() -> None:
    assert "library-automation-advanced" in _SHOTS.read_text(encoding="utf-8")


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
