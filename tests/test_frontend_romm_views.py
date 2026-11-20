"""Guards for the RomM views' save and warning wiring.

Three things these screens must not do, each of which costs the operator
something the UI had just told them was safe:

* the Metadata card's Save must not submit the connection buffers — the
  backend reads a changed URL or library root as a move to another instance,
  which clears the conversion history and retires every pending snapshot;
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
_AUTOMATION = _SRC / "lib" / "components" / "views" / "RuleEditor.svelte"


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
const __paired = [];
let __testReply = {
  reachable: true, authorized: true, library_root_mounted: true, version: '5.4.1', error: null,
};
const toast = { success() {}, error() {}, info() {}, warning() {} };
const romm = {
  settings: {
    url: 'http://romm:8080', library_root: '/library',
    repin_enabled: true, repin_on_load: true, repin_abandon_days: 7,
    swap_keep_rom_id: false, env_defaults: [],
  },
  settingsSaving: false,
  testResult: null,
  async saveSettings(patch) { __saved.push(patch); return this.settings; },
  async testConnection(patch) {
    __tested.push(patch);
    return typeof __testReply === 'function' ? __testReply(patch) : __testReply;
  },
  async pair(url, code) {
    __paired.push([url, code]);
    return { settings: this.settings, missing_scopes: [] };
  },
  async discover() { return null; },
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


_REPORT = """
process.stdout.write(JSON.stringify({
  saved: __saved, tested: __tested, paired: __paired, problem,
  credential, clear_after: clearToken, library_root: libraryRoot,
}));
"""


def test_save_tests_first_and_saves_nothing_that_fails(tmp_path):
    """Test on Save (#532): a connection that doesn't work is not saved, and
    the problem names the field to fix."""
    data = _run(tmp_path, _SETTINGS, """
url = 'http://typo:8080';
__testReply = { reachable: false, authorized: null, library_root_mounted: null,
                field: 'url', error: 'Could not reach RomM.' };
await handleSave();
""" + _REPORT)

    assert data["tested"][0]["url"] == "http://typo:8080", data
    assert data["saved"] == [], data
    assert data["problem"]["field"] == "url", data


def test_save_anyway_skips_the_test(tmp_path):
    data = _run(tmp_path, _SETTINGS, """
__testReply = { reachable: false, field: 'url', error: 'down' };
await handleSave(true);
""" + _REPORT)
    assert data["tested"] == [], data
    assert len(data["saved"]) == 1, data


def test_one_save_sends_the_whole_form(tmp_path):
    """One form, one Save: connection and metadata go together."""
    data = _run(tmp_path, _SETTINGS, """
repinOnLoad = false;
credential = 'rmm_new_credential';
await handleSave();
""" + _REPORT)

    sent = data["saved"][0]
    assert sent["url"] == "http://romm:8080", sent
    assert sent["token"] == "rmm_new_credential", sent
    assert sent["repin_on_load"] is False, sent
    # The swap switch didn't change, so it isn't sent (it probes the token).
    assert "swap_keep_rom_id" not in sent, sent
    # Once submitted, the token doesn't linger in the field.
    assert data["credential"] == "", data


def test_a_cleared_token_is_sent_and_the_tick_reset(tmp_path):
    data = _run(tmp_path, _SETTINGS, "clearToken = true;\nawait handleSave();\n" + _REPORT)
    assert data["saved"][0]["clear_token"] is True, data
    assert data["clear_after"] is False, data


def test_a_pairing_code_is_traded_for_a_token_not_saved_as_one(tmp_path):
    """RomM's pairing code (#532) goes to the pair call, never into `token`."""
    data = _run(tmp_path, _SETTINGS, "credential = 'abcd-2345';\nawait handleSave();\n" + _REPORT)

    assert data["paired"] == [["http://romm:8080", "abcd-2345"]], data
    assert all("token" not in sent for sent in data["saved"]), data
    assert data["credential"] == "", data


def test_an_empty_library_path_is_filled_from_romm(tmp_path):
    data = _run(tmp_path, _SETTINGS, """
libraryRoot = '';
__testReply = (patch) => patch.library_root
  ? { reachable: true, authorized: true, library_root_mounted: true, error: null }
  : { reachable: true, authorized: true, library_root_mounted: null,
      library_root_suggestion: '/data/romm/library', field: 'library_root', error: 'Fill in' };
await handleSave();
""" + _REPORT)

    assert data["library_root"] == "/data/romm/library", data
    assert data["saved"][0]["library_root"] == "/data/romm/library", data


def test_the_keep_rom_id_switch_is_saved_on_its_own(tmp_path):
    """Turning it on probes the token; a refusal must not undo the rest."""
    data = _run(tmp_path, _SETTINGS, "swapKeepRomId = true;\nawait handleSave();\n" + _REPORT)
    assert data["saved"][-1] == {"swap_keep_rom_id": True}, data
    assert "swap_keep_rom_id" not in data["saved"][0], data


def test_the_one_save_button_waits_for_the_settings_to_load():
    """Empty buffers submitted as a connection read as a move to nowhere.

    Until the saved settings arrive the edit buffers are `''`. Saving then
    blanks the URL and library root, which is an identity change — the one
    that retires pending snapshots.
    """
    src = _SETTINGS.read_text(encoding="utf-8")
    submits = re.findall(r'<Button\b[^>]*?type="submit"(.*?)>', src, re.DOTALL)
    assert len(submits) == 1, submits
    assert "disabled={!loaded}" in submits[0], submits[0]
    assert "handleSaveMetadata" not in src and "handleSaveSwap" not in src


def test_enabling_automation_commits_pending_rule_edits():
    """The scheduler reads the server's rules, not the editor's.

    Preview and Run now both save first. The master switch did not, so turning
    it on with unsaved edits started unattended runs against the previous
    configuration — which may still have the platform enabled, or
    delete-on-verify on, while the editor showed the safer one just typed.
    """
    src = _AUTOMATION.read_text(encoding="utf-8")
    body = re.search(
        r"async function toggleAuto\(enabled\) \{(.*?)\n  \}", src, re.DOTALL,
    )
    assert body, "toggleAuto is no longer where this guard can find it"
    assert "commitPendingEdits()" in body.group(1), (
        "toggleAuto enables the scheduler without saving pending rule edits; "
        "Preview and Run now both commit first"
    )
    # Guarded on `enabled`: switching automation OFF must stay immediate, and
    # must not commit edits the operator never saved.
    assert re.search(r"if \(enabled && !\(await commitPendingEdits\(\)\)\) return;",
                     body.group(1)), body.group(1)


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
    assert "ui.navigate('workspace')" in body.group(1), (
        "navigate() drops RomM mode without leaving the RomM view, which has "
        "no breadcrumbs to get back with"
    )


def test_an_action_refuses_when_its_rule_save_was_superseded():
    """The editor stays live while Preview / Run now / the toggle are saving.

    `saveRules()` deliberately keeps an edit that landed mid-save and leaves it
    dirty. Returning success anyway ran the action against the *older*
    server-side snapshot while the editor showed the newer rule — including a
    platform just switched off, or a delete-after-verify just cleared.
    """
    src = _AUTOMATION.read_text(encoding="utf-8")
    body = re.search(
        r"async function commitPendingEdits\(\) \{(.*?)\n  \}\n", src, re.DOTALL,
    )
    assert body, "commitPendingEdits is no longer where this guard looks"
    assert "source.rulesDirty" in body.group(1), (
        "commitPendingEdits reports success without checking whether the save "
        "was superseded by an edit made while it was in flight"
    )
    # ...and it does not simply proceed: the failure path returns false.
    assert re.search(r"if \(source\.rulesDirty\)", body.group(1)), body.group(1)
    assert "return false;" in body.group(1)


def test_automation_offers_neither_unpacking_nor_self_multiplying_modes():
    """Two exclusions, from two places, and both have to stay.

    `kind === 'extract'` is the editor's own choice (automation converts, it
    does not unpack). Whether a mode multiplies its own output is the
    *backend's* answer, because deriving it needs the tool's `output_path`.
    """
    src = _AUTOMATION.read_text(encoding="utf-8")
    assert "m.kind === 'extract'" in src
    assert "source.modeIsAutomatable(m.mode)" in src, (
        "the editor offers modes the backend refuses to run unattended"
    )


def test_the_repin_promise_is_qualified_for_split_builds():
    """A split build has no single file for RomM to hash.

    Past 4 GB the tool writes `Game.iso.0`, `.1`, … and the backend retires the
    re-pin row saying exactly that. The warning promised the metadata would be
    restored regardless, so an unattended conversion finished with no recovery
    and no warning that there would not be one.
    """
    src = _AUTOMATION.read_text(encoding="utf-8")
    block = re.search(
        r"\{#if source\.losesDatMatch\(spec\)\}(.*?)\{/if\}", src, re.DOTALL,
    )
    assert block, "the lost-DAT-match warning is no longer where this guard can find it"
    warning = block.group(1)
    assert "restores it for you" in warning, warning
    assert "rule.split" in warning, (
        "the warning promises metadata restoration unconditionally; a split "
        "build's numbered parts cannot be hash-matched at all"
    )


_IMPORT_TARGETS = """
const said = [];
toast.success = (m) => said.push(['success', m]);
toast.info = (m) => said.push(['info', m]);
romm.importConverterTargets = async () => ({
  created: 2,
  skipped: [{ platform_id: '9', name: 'Xbox 360', slug: 'xbox360', target: 'zar' }],
});
await handleImport();
romm.importConverterTargets = async () => ({ created: 0, skipped: [] });
await handleImport();
process.stdout.write(JSON.stringify({ said, importing }));
"""


def test_importing_romms_targets_says_how_many_rules_it_made(tmp_path):
    """The banner's import names the count, and the Platforms it had to skip (#385)."""
    data = _run(tmp_path, _SETTINGS, _IMPORT_TARGETS)

    (tone, first), (tone2, second) = data["said"]
    assert tone == "success", data
    assert "Imported 2 Rules" in first, first
    assert "Xbox 360 (ZAR)" in first, first
    assert tone2 == "info" and "No Rules to import" in second, second
    assert data["importing"] is False, data
