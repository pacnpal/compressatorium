"""The console table (#541, plan item 12), evaluated with Node.

``src/lib/rules/ruleTable.js`` is how one edit moves a console's Rule;
``RulesSource`` saves each edit on its own and applies the recommended
formats. Both are loaded as they ship (runes reduced), as
``test_frontend_rules_view`` does for the Libraries store.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from .test_frontend_rules_view import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"
_TABLE = _SRC / "rules" / "ruleTable.js"
_SOURCE = _SRC / "rules" / "rulesSource.svelte.js"
_COMPONENTS = _SRC / "components" / "library"


def _node(script: str, tmp_path: Path) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    path = tmp_path / "eval.mjs"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _plain(path: Path) -> str:
    body = re.sub(r"\$state\(", "(", path.read_text(encoding="utf-8"))
    return body.replace("export function ", "function ").replace("export class ", "class ")


def test_picking_a_target_turns_the_console_on_and_drops_the_old_codecs(tmp_path):
    data = _node(_plain(_TABLE) + """
const saved = { mode: 'cso_compress', enabled: false, compression: 'max', timezone: 'Europe/Berlin' };
const retargeted = ruleUpdate(saved, { mode: 'createdvd' });
const same = ruleUpdate(saved, { mode: 'cso_compress' });
const fresh = ruleUpdate({ timezone: 'UTC' }, { mode: 'createcd' }, { isNew: true, zone: 'Asia/Tokyo' });
process.stdout.write(JSON.stringify({ retargeted, same, fresh }));
""", tmp_path)
    assert data["retargeted"]["enabled"] is True
    assert data["retargeted"]["compression"] is None
    assert data["retargeted"]["timezone"] == "Europe/Berlin"
    # The same target again changes nothing: a console saved off stays off.
    assert data["same"]["enabled"] is False
    assert data["same"]["compression"] == "max"
    # A new console takes the browser's zone, not the default's "UTC".
    assert data["fresh"]["timezone"] == "Asia/Tokyo"


def test_delete_original_forces_the_check_on(tmp_path):
    data = _node(_plain(_TABLE) + """
const rule = { mode: 'createcd', verify_after: false, delete_on_verify: false };
process.stdout.write(JSON.stringify(ruleUpdate(rule, { delete_on_verify: true })));
""", tmp_path)
    assert data["delete_on_verify"] is True
    assert data["verify_after"] is True


def test_a_new_console_starts_from_the_saved_defaults(tmp_path):
    data = _node(_plain(_TABLE) + """
const canDelete = { supportsDeleteOnVerify: true };
const cannot = { supportsDeleteOnVerify: false };
process.stdout.write(JSON.stringify({
  keep: newRuleSafety(canDelete, { verify_after: true, delete_on_verify: false }),
  del: newRuleSafety(canDelete, { verify_after: false, delete_on_verify: true }),
  never: newRuleSafety(cannot, { verify_after: false, delete_on_verify: true }),
}));
""", tmp_path)
    assert data["keep"] == {"verify_after": True, "delete_on_verify": False}
    # Deleting always checks first.
    assert data["del"] == {"verify_after": True, "delete_on_verify": True}
    # A format that can't delete safely never starts with delete on.
    assert data["never"] == {"verify_after": False, "delete_on_verify": False}


def test_the_nightly_switch_reads_mixed_when_consoles_differ(tmp_path):
    data = _node(_plain(_TABLE) + """
const ids = ['ps2', 'gc', 'gba'];
const rules = { ps2: { mode: 'createdvd' }, gc: { mode: 'dolphin_rvz' } };
process.stdout.write(JSON.stringify({
  none: nightlyCheckState(ids, rules, {}),
  some: nightlyCheckState(ids, rules, { ps2: { enabled: true } }),
  all: nightlyCheckState(ids, rules, { ps2: { enabled: true }, gc: { enabled: true } }),
}));
""", tmp_path)
    assert data == {"none": False, "some": "mixed", "all": True}


_SOURCE_STUBS = """
const __saves = [];
let __release = [];
const endpoints = {
  getRules: async () => ({ rules: {}, check_rules: {} }),
  saveRules: (rules, checkRules) => new Promise((resolve) => {
    __saves.push(JSON.parse(JSON.stringify(rules)));
    __release.push(() => resolve({ rules, check_rules: checkRules }));
  }),
  forgetConverted: async () => ({}),
  preview: async () => ({}),
  run: async () => ({}),
  recommended: async () => ({
    rules: { psx: { mode: 'createcd', enabled: true }, ps2: { mode: 'createdvd', enabled: true } },
    skipped: [{ platform_id: 'gba', name: 'Game Boy Advance', reason: 'not ready' }],
  }),
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const drain = async () => {
  for (let i = 0; i < 20; i += 1) {
    await tick();
    while (__release.length) __release.shift()();
  }
};
"""


def test_apply_recommended_never_overwrites_a_choice(tmp_path):
    data = _node(_SOURCE_STUBS + _plain(_SOURCE) + """
const source = new RulesSource(endpoints);
await source.loadRules();
source.ruleDefaults = { interval_minutes: 60, enabled: false };
source.rules = { ps2: { mode: 'cso_compress', enabled: false } };
const applying = source.applyRecommended(() => ({ verify_after: true, delete_on_verify: false }));
await drain();
const result = await applying;
process.stdout.write(JSON.stringify({ result, rules: source.rules, saves: __saves.length }));
""", tmp_path)
    assert data["result"]["added"] == ["psx"]
    assert [s["name"] for s in data["result"]["skipped"]] == ["Game Boy Advance"]
    assert data["rules"]["ps2"] == {"mode": "cso_compress", "enabled": False}
    psx = data["rules"]["psx"]
    assert psx["mode"] == "createcd" and psx["enabled"] is True
    assert psx["verify_after"] is True and psx["delete_on_verify"] is False
    assert data["saves"] == 1


def test_autosave_sends_edits_one_at_a_time_in_order(tmp_path):
    data = _node(_SOURCE_STUBS + _plain(_SOURCE) + """
const source = new RulesSource(endpoints);
source.setRule('ps2', { mode: 'createdvd' });
source.scheduleSave(0);
await tick();
// The first save is out; two more edits land before it answers.
source.setRule('gc', { mode: 'dolphin_rvz' });
source.scheduleSave(0);
await tick();
source.setRule('gba', { mode: 'romz_7z' });
source.scheduleSave(0);
await tick();
const inFlight = __saves.length;
await drain();
process.stdout.write(JSON.stringify({
  inFlight, saves: __saves.map((r) => Object.keys(r).sort()), state: source.saveState,
  dirty: source.rulesDirty,
}));
""", tmp_path)
    # One save at a time: the second waits for the first to answer.
    assert data["inFlight"] == 1
    assert data["saves"][0] == ["ps2"]
    assert data["saves"][-1] == ["gba", "gc", "ps2"]
    assert data["state"] == "saved"
    assert data["dirty"] is False


def test_a_text_edit_saves_once_typing_stops(tmp_path):
    data = _node(_SOURCE_STUBS + _plain(_SOURCE) + """
const source = new RulesSource(endpoints);
for (const c of ['(', '(U', '(US', '(USA)']) {
  source.setRule('ps2', { mode: 'createdvd', include_pattern: c });
  source.scheduleSave(50);
}
await new Promise((r) => setTimeout(r, 120));
await drain();
process.stdout.write(JSON.stringify({ saves: __saves }));
""", tmp_path)
    assert len(data["saves"]) == 1
    assert data["saves"][0]["ps2"]["include_pattern"] == "(USA)"


def test_flush_sends_a_waiting_edit_before_a_run(tmp_path):
    data = _node(_SOURCE_STUBS + _plain(_SOURCE) + """
const source = new RulesSource(endpoints);
source.setRule('ps2', { mode: 'createdvd' });
source.scheduleSave(5000);
const flushing = source.flush();
await drain();
process.stdout.write(JSON.stringify({ ok: await flushing, saves: __saves.length }));
""", tmp_path)
    assert data == {"ok": True, "saves": 1}


def test_the_table_saves_every_change_and_has_no_save_button():
    table = (_COMPONENTS / "RuleTable.svelte").read_text(encoding="utf-8")
    automation = (_COMPONENTS / "LibraryAutomation.svelte").read_text(encoding="utf-8")
    assert "source.scheduleSave(delay)" in table
    assert "Save rules" not in table + automation
    for word in ("Saving…", "Saved", "Couldn't save"):
        assert word in automation
    # Preview, Run now and turning automation on save first.
    assert automation.count("await committed()") >= 3
    assert "if (enabled && !(await committed())) return;" in automation


def test_a_paused_console_can_be_turned_back_on(tmp_path):
    """Codex 4652664852: picking the same format isn't a retarget, so the row
    has its own On switch, and it resumes a console paused any way."""
    data = _node(_plain(_TABLE) + """
const paused = { mode: 'createcd', enabled: false };
process.stdout.write(JSON.stringify(ruleUpdate(paused, { enabled: true })));
""", tmp_path)
    assert data["enabled"] is True
    table = (_COMPONENTS / "RuleTable.svelte").read_text(encoding="utf-8")
    assert "update(platform.id, { enabled: e.currentTarget.checked })" in table


def test_an_unfinished_pattern_is_never_saved():
    """Codex 4652664855: autosave of `(` would be refused by the backend,
    which nulls it and pauses the console. The server is asked first, and
    only an answer for the text still in the box saves it
    (tests/test_rule_pattern_check_541.py checks the server's side)."""
    advanced = (_COMPONENTS / "RuleAdvanced.svelte").read_text(encoding="utf-8")
    body = advanced[advanced.index("async function checkPattern("):]
    body = body[: body.index("\n  }\n")]
    assert "api.checkRulePattern(text)" in body
    assert body.index("if (drafts[field] !== text) return;") < body.index("onupdate(")
    assert "if (!error) onupdate({ [field]: text });" in body


def test_fixing_what_validation_paused_resumes_the_console(tmp_path):
    """A fixed pattern, folder or window drops the server's flag and turns
    the console back on, unless another refusal still holds it."""
    data = _node(_plain(_TABLE) + """
const bad = { mode: 'createcd', enabled: false, invalid_pattern: true };
const both = { ...bad, invalid_output_dir: '/gone' };
process.stdout.write(JSON.stringify({
  fixed: ruleUpdate(bad, { include_pattern: 'USA' }),
  still: ruleUpdate(both, { include_pattern: 'USA' }),
  folder: ruleUpdate({ mode: 'createcd', enabled: false, invalid_output_dir: '/gone' },
    { output_dir: null }),
}));
""", tmp_path)
    assert data["fixed"]["enabled"] is True and "invalid_pattern" not in data["fixed"]
    assert data["still"]["enabled"] is False and "invalid_pattern" not in data["still"]
    assert data["folder"]["enabled"] is True and "invalid_output_dir" not in data["folder"]


def test_a_blank_number_saves_nothing(tmp_path):
    data = _node(_plain(_TABLE) + """
process.stdout.write(JSON.stringify([numberValue(''), numberValue('abc'), numberValue('12')]));
""", tmp_path)
    assert data == [None, None, 12]


def test_browse_opens_where_each_consoles_games_are():
    """Codex 4652664869: not the placement for new arrivals, which after a
    layout change is an empty folder."""
    browse = (_COMPONENTS / "LibraryBrowse.svelte").read_text(encoding="utf-8")
    assert "platform.folder" in browse
    assert "getLibraryPlacements" not in browse


def test_the_console_table_says_when_the_consoles_cannot_be_listed():
    """Codex 4652664862: a listing still running or one that failed is not
    "no consoles found yet". Each has its own line and a way to try again,
    as the Rules view had."""
    src = (_COMPONENTS / "LibraryAutomation.svelte").read_text(encoding="utf-8")
    busy = src.index("{:else if source.platformsBusy}")
    error = src.index("{:else if source.platformsError}")
    empty = src.index("No consoles found yet")
    assert busy < empty and error < empty
    assert src.count("source.loadPlatforms()") >= 2
    assert "setTimeout(() => source.loadPlatforms(), RETRY_MS)" in src


def test_the_library_page_keeps_what_the_rules_and_romm_views_showed():
    """Codex round 1 on #547: what the deleted RulesView, RuleEditor and
    RommView told the person is still told somewhere on the Library page."""
    advanced = (_COMPONENTS / "RuleAdvanced.svelte").read_text(encoding="utf-8")
    # Each console's last run and its re-check's, and a paused re-check window.
    assert "source.lastRunFor(platform.id)" in advanced
    assert "source.lastCheckFor(platform.id)" in advanced
    assert "check.invalid_window" in advanced
    table = (_COMPONENTS / "RuleTable.svelte").read_text(encoding="utf-8")
    assert "note?.kind === 'cannot'" in table
    browse = (_COMPONENTS / "LibraryBrowse.svelte").read_text(encoding="utf-8")
    assert "source.counts?.unreadable" in browse
    view = (_COMPONENTS.parent / "views" / "LibraryView.svelte").read_text(encoding="utf-8")
    assert "{#if libraries.error && !libraries.loading}" in view


def test_apply_recommended_waits_for_the_rules_and_reports_a_failed_save(tmp_path):
    """CodeRabbit 4652729322 / 4652729388: before the Library's Rules have
    loaded, Apply would save its few consoles over Rules it never read; and a
    save that fails must not be reported as done."""
    data = _node(_SOURCE_STUBS + _plain(_SOURCE) + """
const out = {};
const early = new RulesSource(endpoints);
try { await early.applyRecommended(); out.early = 'ran'; } catch (e) { out.early = e.message; }
const failing = new RulesSource({
  ...endpoints,
  saveRules: async () => { throw new Error('disk full'); },
});
await failing.loadRules();
try { await failing.applyRecommended(); out.failed = 'ran'; } catch (e) { out.failed = e.message; }
out.dirty = failing.rulesDirty;
out.kept = Object.keys(failing.rules).sort();
process.stdout.write(JSON.stringify(out));
""", tmp_path)
    assert data["early"] != "ran"
    assert "disk full" in data["failed"]
    # The draft stays for the Try again control.
    assert data["dirty"] is True and data["kept"] == ["ps2", "psx"]
    automation = (_COMPONENTS / "LibraryAutomation.svelte").read_text(encoding="utf-8")
    assert "source.rulesLoaded && !source.rulesError && unset.length > 0" in automation


def test_apply_recommended_keeps_its_promise(tmp_path):
    """CodeRabbit 4652729316: "checks every new file, and keeps originals",
    whatever the saved defaults say."""
    automation = (_COMPONENTS / "LibraryAutomation.svelte").read_text(encoding="utf-8")
    body = automation[automation.index("async function applyRecommended()"):]
    body = body[: body.index("\n  }\n")]
    assert "() => ({ verify_after: true, delete_on_verify: false })" in body


def test_a_re_check_can_be_reached_without_a_format(tmp_path):
    """CodeRabbit 4652729352: "Don't convert" leaves the console's re-check;
    its settings and the nightly switch still reach it."""
    data = _node(_plain(_TABLE) + """
const ids = ['ps2', 'gc'];
process.stdout.write(JSON.stringify({
  onlyChecks: nightlyCheckState(ids, {}, { ps2: { enabled: true } }),
  none: nightlyCheckState(ids, {}, {}),
}));
""", tmp_path)
    assert data == {"onlyChecks": True, "none": False}
    table = (_COMPONENTS / "RuleTable.svelte").read_text(encoding="utf-8")
    assert "{#if configured || check}" in table
    assert "{#if (configured || check) && open.has(platform.id)}" in table
    advanced = (_COMPONENTS / "RuleAdvanced.svelte").read_text(encoding="utf-8")
    assert "{#if rule.mode}" in advanced
    automation = (_COMPONENTS / "LibraryAutomation.svelte").read_text(encoding="utf-8")
    assert "disabled={converting.length === 0 && Object.keys(source.checkRules).length === 0}" in automation


def test_a_late_folder_save_never_undoes_a_later_choice():
    """CodeRabbit 4652729306: picking None while ES-DE's folder is saving
    must stay None."""
    src = (_COMPONENTS / "FrontendSelect.svelte").read_text(encoding="utf-8")
    body = src[src.index("async function useFolder("):]
    body = body[: body.index("\n  }\n")]
    assert "const mine = ++choice;" in body
    assert body.index("if (mine !== choice) return;") < body.index("setEsFrontend(true)")
    assert "choice += 1;" in src[src.index("async function choose("):]


def test_browse_names_games_without_a_console_even_with_no_consoles():
    """CodeRabbit 4652729331: a Library of unsorted games still shows the
    notice and its Fix in Activity link."""
    src = (_COMPONENTS / "LibraryBrowse.svelte").read_text(encoding="utf-8")
    empty = src.index("No games found here yet")
    assert src.index("{#snippet unsortedNotice()}") < empty
    assert src.count("{@render unsortedNotice()}") == 2


def test_the_library_page_leaves_romm_mode_however_it_was_entered():
    """CodeRabbit 4652729369: a Connection save can enter RomM mode; leaving
    the page always cancels and exits it, and a save that lands after the
    page is gone puts the browser back too."""
    view = (_COMPONENTS.parent / "views" / "LibraryView.svelte").read_text(encoding="utf-8")
    assert "if (!enteredRomm) return;" not in view
    body = view[view.index("function afterSave()"):]
    body = body[: body.index("\n  }\n")]
    assert "leaveRomm()" in body


def test_an_unknown_library_id_stops_loading_once_the_list_is_in():
    """CodeRabbit 4652729380: with no Libraries at all, the page says the
    Library isn't here instead of loading for ever."""
    view = (_COMPONENTS.parent / "views" / "LibraryView.svelte").read_text(encoding="utf-8")
    assert "{:else if libraries.loading || !libraries.loaded}" in view
    store = (_COMPONENTS.parent.parent / "stores" / "libraries.svelte.js").read_text(encoding="utf-8")
    assert "this.loaded = true;" in store
