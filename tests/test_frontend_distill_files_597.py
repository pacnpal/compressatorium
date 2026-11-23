"""Files counts games, offers what is there, and drops its nested boxes (#597).

- A disc set, or a playlist and its discs, is one game: the Convert panel's
  count, its Convert button and the delete plan agree.
- The type filter lists the extensions in the open folder, not the registry's.
- Pick console puts the folder's best guess first.
- A skipped line offers, in one click, a mode that reads those games but is
  not an Auto target (the eXoDOS repack for DOS zips), from the registry's
  ``autoTarget`` flag.
- No bordered box sits inside another, and Files' headings go h1 → h2.

The pure helpers run under Node; skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.frontend_tools_harness import run_tools

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"
_GAMES = _LIB / "util" / "gameCount.js"
_GUESS = _LIB / "util" / "consoleGuess.js"
_EXTS = _LIB / "util" / "presentExts.js"


def _read(rel: str) -> str:
    return (_LIB / rel).read_text(encoding="utf-8")


def _css(rel: str) -> str:
    return _read(rel).split("<style>", 1)[1]


def _rule(css: str, selector: str) -> str:
    body = css[css.index(selector + " {"):]
    return body[:body.index("}")]


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


# ── Counting games ─────────────────────────────────────────────────────────


def test_a_disc_set_is_one_game(tmp_path):
    out = _eval(tmp_path, _GAMES, "countGames", """
process.stdout.write(JSON.stringify([
  countGames(['/v/Star Saga (Disc 1).cue', '/v/Star Saga (Disc 2).cue', '/v/Star Saga (Disc 3).cue']),
  countGames(['/v/Final Quest (Disc 1).cue', '/v/Final Quest (Disc 2).cue', '/v/Other.iso']),
  countGames(['/v/Game (USA) (CD1).bin', '/v/Game (USA) (CD2).bin', '/v/Game [Disk 3 of 3].bin']),
  countGames(['/a/Game (Disc 1).cue', '/b/Game (Disc 1).cue']),
  countGames(['/v/One.iso', '/v/Two.iso']),
  countGames([]),
]));
""")
    assert out == [1, 2, 2, 2, 2, 0]


def test_a_playlists_discs_count_once_whatever_their_names(tmp_path):
    out = _eval(tmp_path, _GAMES, "countGames", """
const playlist = { path: '/v/FQ.m3u', parts: [{ path: '/v/fq-a.cue' }, { path: '/v/fq-b.cue' }] };
process.stdout.write(JSON.stringify([
  countGames(['/v/fq-a.cue', '/v/fq-b.cue'], [playlist]),
  countGames(['/v/fq-a.cue', '/v/fq-b.cue', '/v/Other.iso'], [playlist]),
]));
""")
    assert out == [1, 2]


def test_the_panel_and_the_delete_plan_count_games():
    panel = _read("components/panels/ConvertPanel.svelte")
    assert "countGames(" in panel
    head = panel[panel.index('<header class="head">'):panel.index("</header>")]
    assert "selectedGames" in head
    assert "fileBrowser.selectedFiles.size} ${" not in head
    plan = _read("components/modals/DeletePlanModal.svelte")
    assert "countGames(" in plan
    assert "across {games}" in plan


# ── The type filter ────────────────────────────────────────────────────────


def test_the_filter_offers_only_what_the_folder_has(tmp_path):
    out = _eval(tmp_path, _EXTS, "presentExts", """
const all = ['.iso', '.cue', '.chd', '.nkit.iso', '.zip', '.rvz'];
const rows = [
  { type: 'file', name: 'A.iso', path: '/v/A.iso' },
  { type: 'file', name: 'B.nkit.iso', path: '/v/B.nkit.iso' },
  { type: 'file', name: 'Set.cue', path: '/v/Set.cue', parts: [{ type: 'file', name: 'Set.bin', path: '/v/Set.bin' }] },
  { type: 'directory', name: 'more.chd', path: '/v/more.chd' },
  { type: 'archive', name: 'Pack.zip', path: '/v/Pack.zip' },
];
process.stdout.write(JSON.stringify([
  presentExts(rows, all),
  presentExts(rows, all, '.rvz'),
]));
""")
    # In the registry's order; a folder's name says nothing.
    assert out[0] == [".iso", ".cue", ".nkit.iso", ".zip"]
    # A filter already picked stays offered, so it can be seen and cleared.
    assert out[1] == [".iso", ".cue", ".nkit.iso", ".zip", ".rvz"]


def test_the_file_list_builds_its_filter_from_the_folder():
    fl = _read("components/panels/FileList.svelte")
    assert "presentExts(fileBrowser.rows, registry.allFilterableExts(), filter)" in fl
    assert "const filterableExts = $derived(registry.allFilterableExts());" not in fl


# ── Pick console ───────────────────────────────────────────────────────────

_PLATFORMS = json.dumps([
    {"slug": "psx", "name": "PlayStation"},
    {"slug": "saturn", "name": "Saturn"},
    {"slug": "dos", "name": "DOS"},
])


def test_the_folders_name_is_the_best_guess(tmp_path):
    out = _eval(tmp_path, _GUESS, "guessConsole", f"""
const platforms = {_PLATFORMS};
process.stdout.write(JSON.stringify([
  guessConsole({{ paths: ['/data/PlayStation/Game.cue'], plan: [], platforms }}),
  guessConsole({{ paths: ['/data/roms/psx/Game.cue'], plan: [], platforms }}),
  guessConsole({{ paths: ['/data/misc/Game.cue'], plan: [], platforms }}),
]));
""")
    assert out == ["psx", "psx", None]


def test_the_folders_other_games_give_the_guess(tmp_path):
    out = _eval(tmp_path, _GUESS, "guessConsole", f"""
const platforms = {_PLATFORMS};
const plan = [
  {{ file_path: '/data/misc/A.cue', platform: 'saturn' }},
  {{ file_path: '/data/misc/B.cue', platform: 'saturn' }},
  {{ file_path: '/data/misc/C.cue', platform: 'psx' }},
  {{ file_path: '/data/elsewhere/D.cue', platform: 'psx' }},
  {{ file_path: '/data/misc/Game.cue', platform: null }},
];
process.stdout.write(JSON.stringify(
  guessConsole({{ paths: ['/data/misc/Game.cue'], plan, platforms }}),
));
""")
    assert out == "saturn"


def test_the_picker_lists_the_guess_first():
    plan = _read("components/panels/ConvertPlan.svelte")
    assert "guessConsole(" in plan
    picker = plan[plan.index('aria-label="Pick the console"'):]
    picker = picker[:picker.index("</select>")]
    assert picker.index("(best guess)") < picker.index("{#each consoles")


# ── One click instead of a skip ────────────────────────────────────────────


def test_a_mode_auto_leaves_alone_is_offered_from_the_registry(tmp_path):
    out = run_tools(tmp_path, ["convertTargets.js"], """
process.stdout.write(JSON.stringify({
  zip: offeredInstead([{ type: 'file', path: '/data/dos/Game (1993).zip' }]),
  cue: offeredInstead([{ type: 'file', path: '/data/psx/Game.cue' }]),
  unready: offeredInstead([{ type: 'file', path: '/data/dos/Game.zip' }], ['chdman']),
  autoOff: registry.all().flatMap((t) => t.modes)
    .filter((m) => m.autoTarget === false).flatMap((m) => [m.mode, m.outputExt]),
}));
""")
    assert "exodos_repack" in out["autoOff"]
    zip_modes = [o["value"] for o in out["zip"]]
    assert "exodos_repack" in zip_modes
    # Only what a mode Auto doesn't take makes (its id, or its format as the
    # picker lists it, #598).
    assert all(o["value"] in out["autoOff"] for o in out["cue"])
    assert out["unready"] == []
    assert all(o["label"] for o in out["zip"])


def test_the_skip_line_offers_it_without_naming_a_tool():
    plan = _read("components/panels/ConvertPlan.svelte")
    assert "offeredInstead(" in plan
    assert "onchoose" in plan
    assert "exodos" not in plan.lower()
    panel = _read("components/panels/ConvertPanel.svelte")
    assert "onchoose={(value) => conversion.chooseTarget(value)}" in panel
    targets = _read("tools/convertTargets.js")
    offered = targets[targets.index("export function offeredInstead"):]
    offered = offered[:offered.index("\n}\n")]
    assert re.search(r"autoTarget [!=]== false", offered)
    assert "exodos" not in offered.lower()


# ── Nested boxes and headings ──────────────────────────────────────────────


def test_no_bordered_box_inside_another():
    fl = _rule(_css("components/panels/FileList.svelte"), ".table-wrap")
    assert "border:" not in fl
    ask = _rule(_css("components/settings/HasheousQuestion.svelte"), ".ask")
    assert "border:" not in ask
    picker = _rule(_css("components/panels/CompressionPicker.svelte"), ".picker")
    assert "border: 1px" not in picker


def test_files_headings_go_h1_then_h2():
    vl = _read("components/panels/VolumeList.svelte")
    assert re.search(r"<h2 class=\"header\">", vl)
    assert "<h3" not in vl


# ── Review follow-up (#598) ────────────────────────────────────────────────


def test_no_offer_for_a_mode_the_backends_probe_did_not_claim(tmp_path):
    """A content-refined mode (ZAR, Xbox) is offered only where the backend's
    probe claimed the file (``convertible_by``). Entries rebuilt from a path
    alone lose that, and so offered ZAR for a non-Xbox .iso."""
    out = run_tools(tmp_path, ["convertTargets.js"], """
const refined = registry.all().filter((t) => t.contentRefined);
const refinedModes = refined.flatMap((t) => t.modes.map((m) => m.mode));
const iso = { type: 'file', path: '/v/Game.iso' };
process.stdout.write(JSON.stringify({
  refinedModes,
  unclaimed: offeredInstead([{ ...iso, convertible_by: ['chdman'] }]).map((o) => o.value),
  claimed: offeredInstead([{ ...iso, convertible_by: refined.map((t) => t.id) }])
    .map((o) => o.value),
}));
""")
    assert out["refinedModes"], "the registry has a content-refined tool"
    assert not set(out["unclaimed"]) & set(out["refinedModes"])


def test_the_plan_offers_from_the_real_entries():
    plan = _read("components/panels/ConvertPlan.svelte")
    body = plan[plan.index("function instead(line)"):]
    body = body[:body.index("\n  }\n")]
    # The rows' own entries, with convertible_by, not a path and a type.
    assert "type: 'file'" not in body
    assert "entryFor(" in body
    panel = _read("components/panels/ConvertPanel.svelte")
    assert panel.count("entries={selected}") == 2


def test_an_offered_choice_survives_the_panels_validation(tmp_path):
    """The offer's value must be one the Convert-to picker lists for the same
    games, or ConvertPanel's check resets it to Best: a Make-smaller mode is
    listed by its format (``.zar``), not its mode id."""
    out = run_tools(tmp_path, ["convertTargets.js"], """
const refined = registry.all().filter((t) => t.contentRefined).map((t) => t.id);
const cases = [
  [{ type: 'file', path: '/v/Game.iso', convertible_by: refined }],
  [{ type: 'file', path: '/v/Game.iso' }],
  [{ type: 'file', path: '/data/dos/Game (1993).zip' }],
  [{ type: 'file', path: '/v/Game.cue' }],
];
const result = cases.map((entries) => {
  const listed = convertTargets({ entries }).flatMap((g) => g.options.map((o) => o.value));
  const offered = offeredInstead(entries).map((o) => o.value);
  return { offered, missing: offered.filter((v) => !listed.includes(v)) };
});
process.stdout.write(JSON.stringify(result));
""")
    assert any(case["offered"] for case in out), out
    for case in out:
        assert case["missing"] == [], case
    # The panel keeps a choice only while the picker lists it.
    panel = _read("components/panels/ConvertPanel.svelte")
    assert "!offered.includes(conversion.target)" in panel
