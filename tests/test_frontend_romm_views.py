"""Guards for the RomM views' save and warning wiring.

Three things these screens must not do, each of which costs the operator
something the UI had just told them was safe:

* the Connection tab's one Save must not submit an address or games folder
  it didn't change — the backend reads a changed URL or library root as a
  move to another instance, which clears the conversion history and retires
  every pending snapshot;
* enabling the master automation switch must commit pending rule edits first,
  or the scheduler runs the *server's* rules while the editor shows the
  operator's;
* the "we save your metadata and restore it" promise must not be made for a
  split build, whose numbered parts RomM cannot hash-match at all.

The first is asserted behaviourally by evaluating the component's `<script>`
with Node (runes reduced, sibling stores stubbed). The other two are wiring,
and are asserted against the source.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_SETTINGS = _SRC / "lib" / "components" / "views" / "RommSettings.svelte"
_LIBRARY = _SRC / "lib" / "components" / "library"
_AUTOMATION = _LIBRARY / "LibraryAutomation.svelte"
_TABLE = _LIBRARY / "RuleTable.svelte"
_RULES_SOURCE = _SRC / "lib" / "rules" / "rulesSource.svelte.js"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _script_body(src: str) -> str | None:
    """The text between the first `<script ...>` and its `</script ...>`.

    Located by index rather than by regular expression on purpose. A regex tag
    filter has to enumerate the spellings a tag can take — `<script lang="ts">`,
    `<SCRIPT>`, `</script\\t\\n foo>` — and the ones it forgets are the ones it
    silently mismatches. Here that would mean reading an *empty* component and
    passing. Two case-insensitive searches have no such gaps, and CodeQL flags
    the regex form (`py/bad-tag-filter`) for exactly this reason.
    """
    lowered = src.lower()
    start = lowered.find("<script")
    if start == -1:
        return None
    opened = src.find(">", start)
    if opened == -1:
        return None
    end = lowered.find("</script", opened)
    if end == -1:
        return None
    return src[opened + 1:end]


def _script_of(component: Path) -> str:
    """The component's `<script>` body as plain ESM.

    Runes reduce to their plain meaning for a single-shot run: `$state(x)` and
    `$derived(x)` are just `x` with nothing subscribed, `$props()` is an empty
    prop bag, and `$effect(fn)` runs once — which is what fills the edit
    buffers from the loaded settings.
    """
    body = _script_body(component.read_text(encoding="utf-8"))
    assert body, f"{component.name} has no <script> block"
    kept = [
        line for line in body.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    text = "\n".join(kept)
    text = text.replace("$props()", "({})")
    text = text.replace("$effect(", "((__fn) => __fn())(")
    text = re.sub(r"\$state\.raw\(", "(", text)
    text = re.sub(r"\$state\(", "(", text)
    text = re.sub(r"\$derived\(", "(", text)
    leftover = [r for r in ("$state(", "$derived(", "$effect(", "$props(") if r in text]
    assert not leftover, f"{component.name} grew runes this harness cannot reduce: {leftover}"
    return text


_STUBS = """
const __saved = [];
const __tested = [];
const toast = { success() {}, error() {}, info() {}, warning() {} };
const romm = {
  settings: {
    url: 'http://romm:8080', library_root: '/library',
    repin_enabled: true, repin_on_load: true, repin_abandon_days: 7,
    swap_keep_rom_id: false, env_defaults: [],
  },
  status: null,
  settingsSaving: false,
  testResult: null,
  async saveSettings(patch) { __saved.push(patch); return this.settings; },
  async testConnection(patch) { __tested.push(patch); return {}; },
  async loadStatus() {},
};
"""


def _run(tmp_path: Path, component: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the RomM views")
    script = tmp_path / "romm_view_eval.mjs"
    script.write_text(_STUBS + _script_of(component) + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate {component.name} via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


_MORE_SAVE = """
// The person only toggles something under More, and saves.
repinOnLoad = false;
await handleSave();
process.stdout.write(JSON.stringify({ submitted: __saved, tested: __tested }));
"""


_CONNECTION_SAVE = """
token = 'rmm_new_credential';
await handleSave();
process.stdout.write(JSON.stringify({
  submitted: __saved, token_after: token, clear_after: clearToken,
}));
"""


_CLEAR_TOKEN_SAVE = """
clearToken = true;
await handleSave();
process.stdout.write(JSON.stringify({
  submitted: __saved, token_after: token, clear_after: clearToken,
}));
"""


_MOVED_SAVE = """
url = 'http://new-romm';
await handleSave();
process.stdout.write(JSON.stringify({ submitted: __saved }));
"""


_SWAP_REFUSED = """
romm.saveSettings = async (patch) => {
  __saved.push(patch);
  if ('swap_keep_rom_id' in patch) throw new Error('The token is missing tasks.run');
  return romm.settings;
};
swapKeepRomId = true;
await handleSave();
process.stdout.write(JSON.stringify({
  submitted: __saved, swap: swapKeepRomId, refusal: swapRefusal, tested: __tested.length,
}));
"""


def test_saving_does_not_submit_an_unchanged_address_or_games_folder(tmp_path):
    """A toggle under More must not read as a move to another RomM.

    A URL or root that differs from the saved one is an identity change on
    the backend: it clears the conversion history and retires every pending
    re-pin snapshot. The one Save sends them only when they changed.
    """
    data = _run(tmp_path, _SETTINGS, _MORE_SAVE)

    assert len(data["submitted"]) == 1, data
    sent = data["submitted"][0]
    assert sorted(sent) == [
        "hasheous_write_ids", "repin_abandon_days", "repin_enabled", "repin_on_load",
    ], sent
    assert sent["repin_on_load"] is False, sent
    # Then the connection is tested, as typed.
    assert data["tested"][0]["url"] == "http://romm:8080", data


def test_a_changed_address_is_sent(tmp_path):
    data = _run(tmp_path, _SETTINGS, _MOVED_SAVE)
    assert data["submitted"][0]["url"] == "http://new-romm", data
    assert "library_root" not in data["submitted"][0], data


def test_a_save_without_a_token_sends_none(tmp_path):
    data = _run(tmp_path, _SETTINGS, _MORE_SAVE)
    sent = data["submitted"][0]
    assert "token" not in sent and "clear_token" not in sent, sent


def test_a_connection_save_still_clears_the_credential_buffers(tmp_path):
    """Once submitted, the token must not linger in the component.

    The reset is what stops a saved credential sitting in a field for the rest
    of the session and being re-submitted by the next save.
    """
    saved = _run(tmp_path, _SETTINGS, _CONNECTION_SAVE)
    assert saved["submitted"][0]["token"] == "rmm_new_credential", saved
    assert saved["token_after"] == "", saved

    cleared = _run(tmp_path, _SETTINGS, _CLEAR_TOKEN_SAVE)
    assert cleared["submitted"][0]["clear_token"] is True, cleared
    assert cleared["clear_after"] is False, cleared


def test_a_refused_keep_the_entry_does_not_undo_the_rest(tmp_path):
    """RomM checks the token for it (#390): a refusal is said, not a lost save."""
    data = _run(tmp_path, _SETTINGS, _SWAP_REFUSED)
    assert "swap_keep_rom_id" not in data["submitted"][0], data
    assert data["submitted"][1] == {"swap_keep_rom_id": True}, data
    assert data["swap"] is False, data
    assert "tasks.run" in data["refusal"], data
    assert data["tested"] == 1, data


def test_the_save_button_waits_for_the_settings_to_load():
    """Empty buffers submitted as a connection read as a move to nowhere."""
    src = _SETTINGS.read_text(encoding="utf-8")
    buttons = re.findall(r"<Button\b[^>]*?onclick=\{(handleSave\w*)\}(.*?)>", src, re.DOTALL)
    assert [name for name, _ in buttons] == ["handleSave"], buttons
    assert "disabled={!loaded}" in buttons[0][1]


def test_enabling_automation_commits_pending_rule_edits():
    """The scheduler reads the server's rules, not the table's.

    Preview and Run now both save first. Turning automation on with an edit
    still waiting would start unattended runs against the previous
    configuration — which may still have delete-on-verify on.
    """
    src = _AUTOMATION.read_text(encoding="utf-8")
    body = re.search(r"async function setAuto\(enabled\) \{(.*?)\n  \}", src, re.DOTALL)
    assert body, "setAuto is no longer where this guard can find it"
    # Guarded on `enabled`: switching automation OFF stays immediate.
    assert "if (enabled && !(await committed())) return;" in body.group(1), body.group(1)
    committed = re.search(r"async function committed\(\) \{(.*?)\n  \}", src, re.DOTALL)
    assert "await source.flush()" in committed.group(1)


def test_navigating_into_a_catalog_folder_leaves_the_romm_view():
    """Dropping catalog mode has to drop the screen that renders it.

    A RomM row can resolve to a directory (a decrypted PS3 game). Clicking it
    exits catalog mode and loads the folder, but the view stayed on `romm` --
    so the platform toolbar sat above unrelated directory entries, and that
    shell renders no breadcrumbs and no parent link. The folder was a one-way
    trip, with no route back to the catalog short of leaving and reopening the
    whole view.
    """
    src = (_SRC / "lib" / "stores" / "fileBrowser.svelte.js").read_text(encoding="utf-8")
    body = re.search(
        r"if \(this\.rommPlatformId !== null\) \{(.*?)\n    \}", src, re.DOTALL,
    )
    assert body, "the exit-RomM branch of navigate() is no longer where this guard looks"
    assert "ui.navigate('files')" in body.group(1), (
        "navigate() drops RomM mode without leaving the RomM view, which has "
        "no breadcrumbs to get back with"
    )


def test_an_action_refuses_when_its_rule_save_was_superseded():
    """The table stays live while Preview / Run now / the switch are saving.

    `saveRules()` deliberately keeps an edit that landed mid-save and leaves it
    dirty. `flush()` reports success only once the server holds what the
    table shows, so an action never runs on an older snapshot.
    """
    src = _RULES_SOURCE.read_text(encoding="utf-8")
    body = re.search(r"async flush\(\) \{(.*?)\n  \}\n", src, re.DOTALL)
    assert body, "flush is no longer where this guard looks"
    assert "this.rulesDirty" in body.group(1)
    assert "return !this.rulesDirty;" in body.group(1)


def test_automation_offers_neither_unpacking_nor_self_multiplying_modes():
    """Two exclusions, from two places, and both have to stay.

    Unpacking is the table's own choice (automation converts, it does not
    unpack). Whether a mode multiplies its own output is the *backend's*
    answer, because deriving it needs the tool's `output_path`.
    """
    src = _TABLE.read_text(encoding="utf-8")
    assert "g.group !== 'Unpack'" in src
    assert "source.modeIsAutomatable(o.value)" in src, (
        "the table offers modes the backend refuses to run unattended"
    )


def test_the_repin_promise_is_qualified_for_split_builds():
    """A split build has no single file for RomM to hash.

    Past 4 GB the tool writes `Game.iso.0`, `.1`, … and the backend retires the
    re-pin row saying exactly that, so the promise must say so.
    """
    src = _TABLE.read_text(encoding="utf-8")
    block = re.search(
        r"if \(source\.losesDatMatch\(spec\)\) \{(.*?)\n    \}", src, re.DOTALL,
    )
    assert block, "the lost-match note is no longer where this guard can find it"
    warning = block.group(1)
    assert "puts them back" in warning, warning
    assert "rule.split" in warning, warning


def test_using_romms_choices_reads_its_plan():
    """The converter banner's import (#385) lives on the Automation tab now."""
    src = _AUTOMATION.read_text(encoding="utf-8")
    body = re.search(r"async function useRommChoices\(\) \{(.*?)\n  \}", src, re.DOTALL)
    assert body, "useRommChoices moved"
    assert "source.importConverterTargets()" in body.group(1)
    assert "Use RomM's choices" in src
