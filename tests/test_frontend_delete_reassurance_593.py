"""Delete dialogs reassure, and the weaker-check escapes are opt-outs (#593).

- Bulk delete names each file in body text, its folder muted and truncated
  from the start, and the space the delete frees.
- Single delete says when a checked copy keeps the game in the library.
- "Quick check only" and "I understand and want to delete anyway" left the
  delete flow. They are opt-outs in Settings → Defaults, off by default. With
  them off, delete always uses the full check and refuses an original whose
  new copy hasn't passed one; with them on, the flow is what it was.

The rules are in ``src/lib/util/deleteSafety.js`` and ``safetyOptions.js``
(pure), so they run under Node here; skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routes import preferences as preferences_routes
from app.services.preferences_store import PreferencesStore

_ROOT = Path(__file__).resolve().parents[1]
_LIB = _ROOT / "src" / "lib"
_SAFETY = _LIB / "util" / "deleteSafety.js"
_OPTIONS = _LIB / "util" / "safetyOptions.js"


def _read(rel: str) -> str:
    return (_LIB / rel).read_text(encoding="utf-8")


def _node() -> str:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    pytest.skip("node not available")
    return ""


def _eval(tmp_path, helper: Path, names: str, body: str):
    node = _node()
    assert helper.is_file(), f"{helper} is missing"
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ {names} }} from {json.dumps(helper.as_uri())};\n" + body,
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ── The opt-outs are off unless asked for ──────────────────────────────────


@pytest.fixture(name="store")
def _store(tmp_path: Path, monkeypatch) -> PreferencesStore:
    store = PreferencesStore(str(tmp_path / "defaults.db"))
    monkeypatch.setattr(preferences_routes, "preferences_store", store)
    return store


@pytest.mark.asyncio
async def test_the_server_starts_with_both_opt_outs_off(store) -> None:
    got = await preferences_routes.get_defaults_preferences()
    assert got["allow_quick_check"] is False
    assert got["allow_unchecked_delete"] is False


@pytest.mark.asyncio
async def test_the_opt_outs_round_trip(store) -> None:
    prefs = preferences_routes.DefaultsPreferences(
        allow_quick_check=True, allow_unchecked_delete=True,
    )
    saved = await preferences_routes.put_defaults_preferences(prefs)
    assert saved["allow_quick_check"] is True
    assert saved["allow_unchecked_delete"] is True
    assert await preferences_routes.get_defaults_preferences() == saved


def test_the_app_starts_with_both_opt_outs_off():
    store = _read("stores/defaults.svelte.js")
    assert "allow_quick_check: false," in store
    assert "allow_unchecked_delete: false," in store


# ── Quick check only ───────────────────────────────────────────────────────

_ROUND_TRIP = "{ supportsDeleteOnVerify: true, supportsRoundTrip: true, supportsVerify: true }"


def test_quick_check_is_not_offered_by_default(tmp_path):
    out = _eval(tmp_path, _OPTIONS, "safetyControls", f"""
const spec = {_ROUND_TRIP};
process.stdout.write(JSON.stringify([
  safetyControls([spec], {{ deleteOriginal: true }}).showQuick,
  safetyControls([spec], {{ deleteOriginal: true, allowQuick: false }}).showQuick,
  safetyControls([spec], {{ deleteOriginal: true, allowQuick: true }}).showQuick,
]));
""")
    assert out == [False, False, True]


def test_a_run_takes_the_quick_check_only_when_it_is_allowed():
    panel = _read("components/panels/ConvertPanel.svelte")
    assert re.search(
        r"containerOnly: choice\.deleteOriginal && choice\.quickCheck\s*"
        r"&& defaults\.values\.allow_quick_check", panel,
    )
    store = _read("stores/conversion.svelte.js")
    assert "quickCheck: !!values?.container_only && !!values?.allow_quick_check" in store
    rules = _read("components/library/RuleTable.svelte")
    assert "defaults.values.container_only && defaults.values.allow_quick_check" in rules


def test_the_convert_panel_and_defaults_pass_the_opt_out_to_the_options():
    assert "allowQuick={defaults.values.allow_quick_check}" in _read(
        "components/panels/ConvertPanel.svelte")
    assert "allowQuick={defaults.values.allow_quick_check}" in _read(
        "components/settings/Defaults.svelte")
    assert "allowQuick" in _read("components/panels/SafetyOptions.svelte")


# ── Delete anyway ──────────────────────────────────────────────────────────


def test_an_unchecked_copy_refuses_the_delete_by_default(tmp_path):
    out = _eval(tmp_path, _SAFETY, "deleteGate", """
process.stdout.write(JSON.stringify([
  deleteGate({ unchecked: 0 }),
  deleteGate({ unchecked: 2 }),
  deleteGate({ unchecked: 2, allowUnchecked: false, acknowledged: true }),
  deleteGate({ unchecked: 2, allowUnchecked: true }),
  deleteGate({ unchecked: 2, allowUnchecked: true, acknowledged: true }),
]));
""")
    assert out == [
        {"state": "clear", "canDelete": True},
        {"state": "refuse", "canDelete": False},
        # An acknowledgement left over can't open a refused delete.
        {"state": "refuse", "canDelete": False},
        {"state": "ask", "canDelete": False},
        {"state": "ask", "canDelete": True},
    ]


def test_unchecked_copies_are_the_existing_outputs_with_no_check(tmp_path):
    out = _eval(tmp_path, _SAFETY, "uncheckedCopies", """
const checked = new Set(['/v/b.chd']);
process.stdout.write(JSON.stringify(uncheckedCopies([
  { path: '/v/a.cue', outputs: [{ exists: true, path: '/v/a.chd' }] },
  { path: '/v/b.cue', outputs: [{ exists: true, path: '/v/b.chd' }] },
  { path: '/v/c.cue', outputs: [{ exists: false, path: '/v/c.chd' }] },
  '/v/d.iso',
], (p) => checked.has(p))));
""")
    assert out == [{"source": "/v/a.cue", "outputs": ["/v/a.chd"]}]


def test_both_delete_dialogs_use_the_gate():
    for rel in ("components/modals/BulkDeleteModal.svelte", "components/modals/DeleteModal.svelte"):
        src = _read(rel)
        assert "deleteGate(" in src, rel
        assert "defaults.values.allow_unchecked_delete" in src, rel
        # The acknowledgement shows only when the opt-out is on.
        ack = src.index("I understand and want to delete anyway")
        assert "gate.state === 'ask'" in src[:ack], rel
        # Refused, the dialog offers to check the copies instead.
        assert "Check the copies" in src, rel


# ── What the dialogs say ───────────────────────────────────────────────────


def test_a_path_splits_into_its_name_and_folder(tmp_path):
    out = _eval(tmp_path, _SAFETY, "splitPath", """
process.stdout.write(JSON.stringify([
  splitPath('/data/roms/psx/Final Quest (Disc 1).cue'), splitPath('game.iso'),
]));
""")
    assert out == [
        {"name": "Final Quest (Disc 1).cue", "folder": "/data/roms/psx"},
        {"name": "game.iso", "folder": ""},
    ]


def test_the_space_freed_adds_the_known_sizes(tmp_path):
    out = _eval(tmp_path, _SAFETY, "freedBytes", """
process.stdout.write(JSON.stringify(freedBytes([
  { path: '/a', size: 1024 }, { path: '/b', size: 2048 }, { path: '/c' }, '/d',
])));
""")
    assert out == 3072


def test_a_checked_copy_is_named(tmp_path):
    out = _eval(tmp_path, _SAFETY, "checkedCopyLine", """
process.stdout.write(JSON.stringify([
  checkedCopyLine({ format: 'DOSZ', size: 400 }),
  checkedCopyLine({ format: 'CHD', size: null }),
  checkedCopyLine(null),
]));
""")
    assert out == [
        "Its DOSZ copy checked out (400 B), so the game stays in your library.",
        "Its CHD copy checked out, so the game stays in your library.",
        None,
    ]


def test_bulk_delete_names_files_in_body_text_with_the_folder_under_them():
    src = _read("components/modals/BulkDeleteModal.svelte")
    assert "splitPath(" in src
    assert "freedBytes(" in src
    assert "Frees " in src
    css = src.split("<style>", 1)[1]
    folder = css[css.index(".bd-folder {"):]
    folder = folder[:folder.index("}")]
    # Truncated from the start, so the end of the path stays.
    assert "direction: rtl;" in folder
    assert "text-overflow: ellipsis;" in folder
    name = css[css.index(".bd-name {"):]
    name = name[:name.index("}")]
    assert "font-family: var(--font-mono)" not in name


def test_single_delete_says_when_a_checked_copy_keeps_the_game():
    src = _read("components/modals/DeleteModal.svelte")
    assert "checkedCopyLine(" in src
    assert "verification.checkFor(" in src


def test_the_settings_document_both_opt_outs():
    src = _read("components/settings/Defaults.svelte")
    assert "Offer a quick check before deleting" in src
    assert "Allow deleting originals whose new copy isn't checked" in src
    # Turning the quick check off also drops a saved quick check.
    assert "container_only: false" in src
