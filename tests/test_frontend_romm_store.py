"""Behavioural guards for the RomM store's async bookkeeping.

``src/lib/stores/romm.svelte.js`` drives the Library tab, and its loads
overlap: a settings save re-derives the catalog while the first load of the
session may still be in flight. Which answer wins is not cosmetic -- the
selected platform is what the Convert panel narrows to and what the submit
path sends -- so it is asserted here rather than left to review.

Evaluated with Node, the same engine the app uses, with the runes reduced to
plain fields (``$state(x)`` is just ``x`` for a single-shot run) and the
sibling stores stubbed. Skipped when Node is unavailable.
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
_ROMM_STORE_JS = _SRC / "lib" / "stores" / "romm.svelte.js"
_RULES_SOURCE_JS = _SRC / "lib" / "rules" / "rulesSource.svelte.js"
_SAVE_QUEUE_JS = _SRC / "lib" / "stores" / "latestSaveQueue.js"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _plain_js() -> str:
    """The store as plain ESM: imports dropped, runes reduced to their value.

    ``$state(x)`` is a reactive box around ``x``; outside a component nothing
    subscribes, so the initialiser alone is the whole behaviour under test.

    The store extends ``RulesSource`` (shared with folder Libraries, #303),
    so that file goes first; it has no imports.
    """
    base = _RULES_SOURCE_JS.read_text(encoding="utf-8").replace("export class ", "class ")
    queue = _SAVE_QUEUE_JS.read_text(encoding="utf-8").replace("export class ", "class ")
    src = queue + "\n" + base + "\n" + _ROMM_STORE_JS.read_text(encoding="utf-8")
    kept = [
        line for line in src.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    body = "\n".join(kept)
    body = re.sub(r"\$state\.raw\(", "(", body)
    body = re.sub(r"\$state\(", "(", body)
    leftover = [
        rune for rune in ("$state(", "$derived(", "$derived.by(", "$effect(")
        if rune in body
    ]
    assert not leftover, (
        f"romm.svelte.js grew runes this harness does not reduce: {leftover}"
    )
    return body


_STUBS = """
const __calls = [];
const __defer = () => new Promise((resolve, reject) => {
  __calls.push({ resolve, reject });
});
const __reloads = [];
const api = {
  getRommPlatforms: (...a) => { __reloads.push('platforms'); return __defer(...a); },
  saveLibraryRules: __defer,
  getRommStatus: (...a) => { __reloads.push('status'); return __defer(...a); },
  saveRommSettings: async (patch) => (
    globalThis.__saveSettings ? globalThis.__saveSettings(patch) : { ...patch, saved: true }
  ),
  getRommSettings: (...a) => globalThis.__getSettings(...a),
};
const fileBrowser = { rommPlatformId: null, exitRomm() {}, async enterRomm(id) {
  this.rommPlatformId = id;
} };
const conversion = {
  mode: 'createcd', primaryTool: 'chdman',
  setPrimaryTool(id) { this.primaryTool = id; }, setMode(m) { this.mode = m; },
};
const ui = { workspaceTool: 'chdman' };
const registry = { all: () => [], forTool: () => null };
const tick = () => new Promise((resolve) => setImmediate(resolve));
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the RomM store")
    script = tmp_path / "romm_store_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the RomM store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


_OVERLAPPING_LOADS = """
const store = new RommStore();

// The load already in flight when the connection details change. It is asking
// the OLD instance.
const first = store.loadPlatforms();
// The save handler's forced reload, asking the new one.
const second = store.loadPlatforms({ force: true });

await tick();
// The new instance answers first...
__calls[1]?.resolve([{ id: 9, name: 'New', tool_ids: null, mode_ids: null }]);
await second;
// ...and the old one afterwards, which is the whole point: its answer is late,
// and it describes a server nobody is pointed at any more.
__calls[0].resolve([{ id: 1, name: 'Old', tool_ids: null, mode_ids: null }]);
await first;
await tick();

process.stdout.write(JSON.stringify({
  requests: __calls.length,
  platforms: store.platforms.map((p) => p.name),
  selected: store.selectedPlatformId,
  loading: store.platformsLoading,
  error: store.platformsError,
}));
"""


def test_a_superseded_platform_load_cannot_select_the_old_instance(tmp_path):
    """The forced reload after a settings save must win, whenever it lands.

    `loadPlatforms` bailed while another load was in flight, so the reload that
    runs when the URL/token changes did nothing and the in-flight request --
    aimed at the previous instance -- populated the catalog afterwards, then
    selected one of its platforms. The Convert panel narrows to that selection
    and the submit path sends it, so the user could queue conversions against a
    platform belonging to a server they had just navigated away from.
    """
    data = _run(tmp_path, _OVERLAPPING_LOADS)

    # The forced load actually asked, rather than deferring to the stale one.
    assert data["requests"] == 2, data
    assert data["platforms"] == ["New"], data
    assert data["selected"] == 9, data
    # And the late answer does not leave the spinner or an error behind.
    assert data["loading"] is False, data
    assert data["error"] is None, data


_UNFORCED_LOAD = """
const store = new RommStore();
const first = store.loadPlatforms();
const second = store.loadPlatforms();
await tick();
__calls[0].resolve([{ id: 1, name: 'Old', tool_ids: null, mode_ids: null }]);
await first;
await second;
process.stdout.write(JSON.stringify({ requests: __calls.length }));
"""


def test_an_unforced_platform_load_still_defers_to_the_one_in_flight(tmp_path):
    """Only `force` supersedes. The plain de-duplication has to survive.

    Several surfaces call `loadPlatforms()` on mount; turning every one of them
    into its own request would put a burst of full catalog listings on a large
    instance for one screen.
    """
    assert _run(tmp_path, _UNFORCED_LOAD)["requests"] == 1


_ABANDONED_LOAD = """
const store = new RommStore();
const load = store.loadPlatforms();
await tick();

// The view unmounts while the request is outstanding.
store.cancelPlatformLoad();
fileBrowser.rommPlatformId = null;   // what exitRomm() leaves behind

// ...and only now does RomM answer.
__calls[0].resolve([{ id: 9, name: 'New', tool_ids: null, mode_ids: null }]);
await load;
await tick();
// Sampled here, before anything legitimate re-enters: this is the value the
// workspace would open with.
const entered_after_cancel = fileBrowser.rommPlatformId;

// A later visit must still be able to load.
const again = store.loadPlatforms();
await tick();
__calls[1]?.resolve([{ id: 9, name: 'New', tool_ids: null, mode_ids: null }]);
await again;

process.stdout.write(JSON.stringify({
  entered_after_cancel,
  requests: __calls.length,
  entered_after_revisit: fileBrowser.rommPlatformId,
  platforms: store.platforms.map((p) => p.name),
}));
"""


def test_an_abandoned_platform_load_cannot_re_enter_romm_mode(tmp_path):
    """Leaving the RomM view has to invalidate the load still in flight.

    A load ends by selecting a platform, which calls `enterRomm()`. The view's
    cleanup restores the ordinary directory listing — and the outstanding
    request then put the catalog straight back into the browser, so the
    workspace opened showing RomM rows under a directory heading. The view's
    own `alive` flag cannot cover it: the side effect happens inside the store.
    """
    data = _run(tmp_path, _ABANDONED_LOAD)

    # Nothing re-entered RomM mode on the abandoned load's behalf.
    assert data["entered_after_cancel"] is None, data
    # The next deliberate visit still works — the cancel must not leave the
    # store wedged as "a load is already running".
    assert data["requests"] == 2, data
    assert data["entered_after_revisit"] == 9, data
    assert data["platforms"] == ["New"], data


_SAVE_RACE = """
const store = new RommStore();
store.rules = { '7': { mode: 'dolphin_rvz', enabled: true } };

// The operator saves...
const saving = store.saveRules();
await tick();
// ...and keeps editing while the request is in flight. This one turns a
// platform OFF, which is exactly the edit that must not be lost.
store.setRule('9', { mode: 'dolphin_rvz', enabled: false });

// The server answers with its normalized copy of what was SUBMITTED.
__calls[0].resolve({ rules: { '7': { mode: 'dolphin_rvz', enabled: true } } });
await saving;

process.stdout.write(JSON.stringify({
  rules: store.rules,
  dirty: store.rulesDirty,
}));
"""


def test_a_rule_edited_during_a_save_is_not_silently_discarded(tmp_path):
    """The editor stays live while the save is in flight.

    The response carries the server's copy of what was *submitted*, and
    adopting it replaced anything typed since — then cleared the dirty flag, so
    the Save bar stopped asking and the edit was gone with no warning. The one
    that matters most is the edit that makes a rule safer.
    """
    data = _run(tmp_path, _SAVE_RACE)

    assert "9" in data["rules"], data
    assert data["rules"]["9"]["enabled"] is False, data
    # Still dirty, so the next save normalizes it rather than losing it.
    assert data["dirty"] is True, data


_QUIET_SAVE = """
const store = new RommStore();
store.rules = { '7': { mode: 'dolphin_rvz', enabled: true } };
const saving = store.saveRules();
await tick();
__calls[0].resolve({ rules: { '7': { mode: 'dolphin_rvz', enabled: true, order: 'largest' } } });
await saving;
process.stdout.write(JSON.stringify({ rules: store.rules, dirty: store.rulesDirty }));
"""


def test_an_undisturbed_save_still_adopts_the_server_copy(tmp_path):
    """The server clamps numbers and drops stale modes; the editor must show it."""
    data = _run(tmp_path, _QUIET_SAVE)
    assert data["rules"]["7"]["order"] == "largest", data
    assert data["dirty"] is False, data


_BADGE = """
const store = new RommStore();
store.status = { pendingRepins: 3 };
// A retry records a row that SUPERSEDES the existing one, so the backend's
// count is unchanged. Adding the newly recorded row inflated the badge.
store.setPendingRepins(3);
const afterRetry = store.status.pendingRepins;
store.setPendingRepins(0);
const afterSettle = store.status.pendingRepins;
// Nothing to report leaves it alone rather than zeroing a real backlog.
store.setPendingRepins(undefined);
process.stdout.write(JSON.stringify({
  afterRetry, afterSettle, afterUnknown: store.status.pendingRepins,
}));
"""


def test_the_repin_badge_is_set_from_the_backend_count(tmp_path):
    """Recording is not addition.

    `record()` supersedes the pending row for a destination rather than
    stacking one, so re-recording a retried or redirected conversion leaves the
    total unchanged — and adding each recorded row inflated the badge on every
    retry, staying wrong until a status reload or a settle pass.
    """
    data = _run(tmp_path, _BADGE)
    assert data["afterRetry"] == 3, data
    assert data["afterSettle"] == 0, data
    assert data["afterUnknown"] == 0, data


_STALE_STATUS = """
const store = new RommStore();

// The first status read is still waiting on the old, unreachable URL.
const first = store.loadStatus();
await tick();
// The operator saves a new connection, which reads status again.
const second = store.loadStatus();
await tick();

// The new instance answers first: reachable, library mounted.
__calls[1].resolve({ configured: true, connected: true, library_root: '/new',
                     library_root_mounted: true });
await second;
// ...and the old one answers afterwards, reporting the failure that made the
// operator change it in the first place.
__calls[0].resolve({ configured: true, connected: false, library_root: '/old',
                     library_root_mounted: false, error: 'unreachable' });
await first;
await tick();

process.stdout.write(JSON.stringify({
  requests: __calls.length,
  libraryRoot: store.status?.libraryRoot,
  connected: store.status?.connected,
  error: store.status?.error,
  loading: store.statusLoading,
}));
"""


def test_a_stale_status_answer_cannot_overwrite_the_new_connection(tmp_path):
    """The read that is still waiting is asking the server being replaced.

    A slow or unreachable URL is exactly why the operator is changing it, so
    that request is the one most likely to be outstanding when the save runs.
    Letting its answer land afterwards left the view reporting the new instance
    as unusable while its platforms and catalog had already loaded.
    """
    data = _run(tmp_path, _STALE_STATUS)

    assert data["requests"] == 2, data
    assert data["libraryRoot"] == "/new", data
    assert data["connected"] is True, data
    assert data["error"] is None, data
    assert data["loading"] is False, data


_POLICY_SAVE = """
const store = new RommStore();
const before = __reloads.length;
// Toggling automation, or saving a metadata preference. Neither says anything
// about which server or which files are being described.
await store.saveSettings({ auto_convert: true });
await store.saveSettings({ repin_enabled: false, repin_abandon_days: 14 });
const afterPolicy = __reloads.length - before;

// ...whereas any of these is a different instance, or a different view of it.
await Promise.race([store.saveSettings({ url: 'http://other:8080' }), tick()]);
const afterUrl = __reloads.length - before - afterPolicy;

process.stdout.write(JSON.stringify({ afterPolicy, afterUrl, reloads: __reloads }));
"""


_UNCHANGED_CONNECTION = """
const store = new RommStore();
store.settings = { url: 'http://romm:8080', library_root: '/library' };
const before = __reloads.length;
// The one form sends the connection on every Save, unchanged here.
await store.saveSettings({
  url: 'http://romm:8080', library_root: '/library', repin_enabled: false,
});
const unchanged = __reloads.length - before;
await Promise.race([
  store.saveSettings({ url: 'http://romm:8080', library_root: '/other' }), tick(),
]);
const moved = __reloads.length - before - unchanged;
process.stdout.write(JSON.stringify({ unchanged, moved }));
"""


def test_an_unchanged_connection_in_the_form_does_not_rescan_the_catalog(tmp_path):
    """A Save carrying the stored URL and library path is no identity change
    (CodeRabbit PRRT_kwDOQ9nULM66RLwI); a different value still is."""
    data = _run(tmp_path, _UNCHANGED_CONNECTION)

    assert data["unchanged"] == 0, data
    assert data["moved"] > 0, data


def test_a_policy_only_save_does_not_rescan_the_catalog(tmp_path):
    """Only an identity change invalidates what is on screen.

    `saveSettings` re-read the status and force-reloaded the platform list
    after *every* save, and reloading re-enters the selected platform, whose
    catalog load stats every ROM in it. So toggling automation on a large
    remote library held the Save action for the catalog scan's multi-minute
    bound and discarded a listing that was still correct.

    Keyed off what the patch carried rather than a diff of the saved settings,
    because the token is never returned to the browser: a credential change is
    only ever visible here as a submitted field.
    """
    data = _run(tmp_path, _POLICY_SAVE)

    assert data["afterPolicy"] == 0, data
    # The connection save still re-derives everything downstream.
    assert data["afterUrl"] > 0, data
    assert "status" in data["reloads"], data


# --- Beside RomM's converter (#385) ---------------------------------------

_CONVERTER_STATUS = """
const store = new RommStore();
const answer = store.loadStatus();
await tick();
__calls[0].resolve({
  configured: true, connected: true,
  converter: {
    enabled: true,
    platform_formats: { ngc: 'rvz', psx: 'chd' },
    library_targets: { ngc: ['iso', 'rvz'], psx: ['chd'], wii: ['iso', 'rvz', 'wbfs'] },
    modes: { ngc: ['dolphin_rvz', 'nkit_to_rvz'], psx: ['createcd'] },
    error: null,
  },
});
await answer;
const ngc = { id: 7, slug: 'ngc' };
process.stdout.write(JSON.stringify({
  active: store.activeConverter !== null,
  agrees: store.converterNoteFor(ngc, { mode: 'dolphin_rvz' }),
  differs: store.converterNoteFor(ngc, { mode: 'dolphin_wia' }),
  noRule: store.converterNoteFor(ngc, null),
  cannot: store.converterNoteFor({ id: 11, slug: 'gba' }, null),
  canButDoesNot: store.converterNoteFor({ id: 10, slug: 'wii' }, null),
}));
"""


def test_the_converter_notes_come_from_the_one_status_read(tmp_path):
    """The Rule flag carries both targets; the mark names what RomM cannot convert."""
    data = _run(tmp_path, _CONVERTER_STATUS)

    assert data["active"] is True, data
    assert data["agrees"] == {
        "kind": "converts", "target": "rvz",
        "modes": ["dolphin_rvz", "nkit_to_rvz"], "agrees": True,
    }, data
    assert data["differs"]["agrees"] is False, data
    assert data["noRule"]["agrees"] is None, data
    assert data["cannot"] == {"kind": "cannot"}, data
    # RomM can convert Wii, it just isn't told to: nothing to say.
    assert data["canButDoesNot"] is None, data


_CONVERTER_OFF = """
const notes = [];
for (const converter of [null, { enabled: false, platform_formats: {}, library_targets: null,
  modes: {}, error: null }]) {
  const store = new RommStore();
  const answer = store.loadStatus();
  await tick();
  __calls[__calls.length - 1].resolve({ configured: true, connected: true, converter });
  await answer;
  notes.push({
    active: store.activeConverter,
    note: store.converterNoteFor({ id: 11, slug: 'gba' }, null),
  });
}
process.stdout.write(JSON.stringify(notes));
"""


def test_no_banner_and_no_flags_without_a_converter_switched_on(tmp_path):
    """A RomM without the converter, or with it off, gets no warnings at all."""
    data = _run(tmp_path, _CONVERTER_OFF)
    assert data == [{"active": None, "note": None}, {"active": None, "note": None}], data


_IMPORT = """
const store = new RommStore();
store.ruleDefaults = { enabled: false, mode: '', timezone: 'UTC', interval_minutes: 60 };
const saved = [];
api.getLibraryRules = async () => ({
  rules: { '10': { mode: 'dolphin_wia', enabled: true } },
  check_rules: {}, defaults: store.ruleDefaults, state: {},
});
api.getRommConverterImportPlan = async () => ({
  enabled: true,
  rules: {
    '7': { mode: 'dolphin_rvz', target: 'rvz' },
    '8': { mode: 'createcd', target: 'chd' },
    '10': { mode: 'dolphin_rvz', target: 'rvz' },
  },
  skipped: [{ platform_id: '9', name: 'Xbox 360', slug: 'xbox360', target: 'zar' }],
  existing: [],
});
api.saveLibraryRules = async (id, rules, checkRules) => {
  saved.push(rules);
  if (id !== 'romm') throw new Error(`saved to ${id}`);
  return { rules, check_rules: checkRules };
};
const result = await store.importConverterTargets();
process.stdout.write(JSON.stringify({ result, saved, rules: store.rules }));
"""


def test_import_saves_new_rules_through_the_rules_api_only_where_none_exists(tmp_path):
    """Imported Rules go out through PUT /api/libraries/romm/rules, and a Rule is never replaced."""
    data = _run(tmp_path, _IMPORT)

    assert data["result"]["created"] == 2, data
    assert data["result"]["skipped"][0]["name"] == "Xbox 360", data
    assert len(data["saved"]) == 1, data
    sent = data["saved"][0]
    assert sent["7"]["mode"] == "dolphin_rvz", sent
    assert sent["8"]["mode"] == "createcd", sent
    # Off until the operator turns it on.
    assert sent["7"]["enabled"] is False, sent
    # The Wii Rule the operator already had is left exactly as it was.
    assert sent["10"] == {"mode": "dolphin_wia", "enabled": True}, sent


_SETTINGS_SAVES = """
const sent = [];
const answers = [];
globalThis.__saveSettings = (patch) => new Promise((resolve, reject) => {
  sent.push(patch);
  answers.push({ resolve, reject });
});
globalThis.__getSettings = async () => ({
  auto_convert: true, es_frontend: false, readBack: true,
});
const store = new RommStore();
"""


def test_overlapping_settings_saves_keep_the_newest_settings(tmp_path):
    """Issue 408: the older save answering last must not win.

    Two saves go out together. The second is not sent until the first has
    answered (the backend applies them in click order), and the screen ends on
    the second save's answer, however the answers are timed.
    """
    data = _run(tmp_path, _SETTINGS_SAVES + """
const first = store.saveSettings({ auto_convert: true });
await tick();
const second = store.saveSettings({ es_frontend: true });
await tick();
const sentBeforeFirstAnswered = sent.length;
answers[0].resolve({ auto_convert: true, es_frontend: false });
await first;
await tick();
const afterFirst = JSON.stringify(store.settings);
answers[1].resolve({ auto_convert: true, es_frontend: true });
await second;
process.stdout.write(JSON.stringify({
  sentBeforeFirstAnswered, sent, afterFirst, settings: store.settings,
  saving: store.settingsSaving,
}));
""")
    assert data["sentBeforeFirstAnswered"] == 1, data
    assert data["sent"] == [{"auto_convert": True}, {"es_frontend": True}], data
    # The superseded first answer is not adopted while a newer save is queued.
    assert data["afterFirst"] == "null", data
    assert data["settings"] == {"auto_convert": True, "es_frontend": True}, data
    assert data["saving"] is False, data


def test_a_failing_earlier_settings_save_does_not_overwrite_a_later_success(tmp_path):
    """The earlier save's error reaches its caller; the later save's answer stands."""
    data = _run(tmp_path, _SETTINGS_SAVES + """
const first = store.saveSettings({ auto_convert: true }).catch((e) => e.message);
await tick();
const second = store.saveSettings({ es_frontend: true });
await tick();
const sentBeforeFirstSettled = sent.length;
answers[0].reject(new Error('first failed'));
const error = await first;
await tick();
answers[1].resolve({ auto_convert: false, es_frontend: true });
await second;
process.stdout.write(JSON.stringify({
  error, settings: store.settings, sent, sentBeforeFirstSettled,
}));
""")
    assert data["sentBeforeFirstSettled"] == 1, data
    assert data["error"] == "first failed", data
    assert data["settings"] == {"auto_convert": False, "es_frontend": True}, data
    assert len(data["sent"]) == 2, data


def test_a_failed_latest_settings_save_reads_the_settings_back(tmp_path):
    """An earlier save landed unadopted, so after the latest fails, the screen
    shows what the server holds rather than the last answer it happened to see.
    """
    data = _run(tmp_path, _SETTINGS_SAVES + """
const first = store.saveSettings({ auto_convert: true });
await tick();
const second = store.saveSettings({ es_frontend: true }).catch((e) => e.message);
await tick();
answers[0].resolve({ auto_convert: true, es_frontend: false });
await first;
await tick();
answers[1].reject(new Error('second failed'));
const error = await second;
process.stdout.write(JSON.stringify({ error, settings: store.settings }));
""")
    assert data["error"] == "second failed", data
    assert data["settings"] == {
        "auto_convert": True, "es_frontend": False, "readBack": True,
    }, data


def test_a_connection_save_whose_newer_save_fails_still_refreshes(tmp_path):
    """Codex PRRT_kwDOQ9nULM64RHI5: the backend already uses the new connection.

    A url save lands while a newer save is queued, so it skips the refresh. The
    newer save then fails; the status and platforms are still re-read, or they
    would keep describing the old connection.
    """
    data = _run(tmp_path, _SETTINGS_SAVES + """
const first = store.saveSettings({ url: 'http://new:8080' });
await tick();
const second = store.saveSettings({ es_frontend: true }).catch((e) => e.message);
await tick();
answers[0].resolve({ url: 'http://new:8080' });
await first;
await tick();
const reloadsBefore = __reloads.length;
answers[1].reject(new Error('second failed'));
await tick();
await tick();
process.stdout.write(JSON.stringify({ reloadsBefore, reloads: __reloads }));
""")
    assert data["reloadsBefore"] == 0, data
    assert "status" in data["reloads"], data


def test_a_slow_catalog_refresh_does_not_hold_later_settings_saves(tmp_path):
    """Codex PRRT_kwDOQ9nULM64RHI_: only the PUT and adopting it hold the queue."""
    data = _run(tmp_path, _SETTINGS_SAVES + """
const first = store.saveSettings({ url: 'http://new:8080' });
await tick();
answers[0].resolve({ url: 'http://new:8080' });
await tick();
await tick();
const refreshing = __reloads.includes('status');
const second = store.saveSettings({ auto_convert: false });
await tick();
await tick();
process.stdout.write(JSON.stringify({ refreshing, sent }));
""")
    assert data["refreshing"] is True, data
    # The second PUT went out while the status read was still waiting.
    assert data["sent"] == [{"url": "http://new:8080"}, {"auto_convert": False}], data


def test_a_refresh_superseded_by_a_newer_connection_change_is_dropped(tmp_path):
    """Only the latest connection change's refresh applies its result."""
    data = _run(tmp_path, _SETTINGS_SAVES + """
const first = store.saveSettings({ url: 'http://one:8080' });
await tick();
answers[0].resolve({ url: 'http://one:8080' });
await tick(); await tick();
const second = store.saveSettings({ url: 'http://two:8080' });
await tick();
answers[1].resolve({ url: 'http://two:8080' });
await tick(); await tick();
const statusCalls = __reloads.filter((r) => r === 'status').length;
// The older refresh's status answer arrives last and says "usable".
const usable = { configured: true, connected: true, library_root_mounted: true };
__calls[0].resolve(usable);
await tick(); await tick();
process.stdout.write(JSON.stringify({ statusCalls, reloads: __reloads }));
""")
    assert data["statusCalls"] == 2, data
    # The superseded refresh never goes on to list platforms.
    assert "platforms" not in data["reloads"], data


_FAILING_REFRESH = _SETTINGS_SAVES + """
// The refresh's cleanup throws once the status says nothing is usable.
fileBrowser.exitRomm = () => { throw new Error('refresh blew up'); };
const first = store.saveSettings({ url: 'http://new:8080' });
await tick();
"""


def test_a_failed_save_keeps_its_own_error_when_the_refresh_also_fails(tmp_path):
    """CodeRabbit 4595283279: the caller gets the save's error, not the refresh's."""
    data = _run(tmp_path, _FAILING_REFRESH + """
const second = store.saveSettings({ es_frontend: true }).catch((e) => e.message);
await tick();
answers[0].resolve({ url: 'http://new:8080' });
await first;
await tick();
answers[1].reject(new Error('second failed'));
await tick();
__calls[0].resolve({ configured: true, connected: false });
const error = await second;
process.stdout.write(JSON.stringify({ error }));
""")
    assert data["error"] == "second failed", data


def test_a_failing_refresh_is_reported_when_the_save_succeeded(tmp_path):
    """With no save error to keep, the refresh's own error reaches the caller."""
    data = _run(tmp_path, _FAILING_REFRESH + """
const outcome = first.catch((e) => e.message);
answers[0].resolve({ url: 'http://new:8080' });
await tick();
__calls[0].resolve({ configured: true, connected: false });
const error = await outcome;
process.stdout.write(JSON.stringify({ error, settings: store.settings }));
""")
    assert data["error"] == "refresh blew up", data
    # The new settings were adopted before the refresh failed.
    assert data["settings"] == {"url": "http://new:8080"}, data
