"""Files says it plainly where a converter is missing and in Info (#591).

- The plan put the server's not-ready reason on every game ("CHDMAN isn't
  ready: chdman was not found at /usr/bin/chdman (set CHDMAN_PATH …)"). Now
  the games that need a missing converter are one plain line with a See
  Converters button, and the raw reason sits once behind the line's fold.
- Info dumped every field a tool returned ("Size display", "Compressed
  false"). It now opens on a short card, says Yes/No, drops ``*_display``
  duplicates and keeps the rest behind More details.
- A disabled Info in the row menu says why.

The helpers are pure (``src/lib/util/planLines.js``, ``fileInfo.js``), so they
run under Node here; skipped when Node is unavailable.
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

_ROOT = Path(__file__).resolve().parents[1]
_LIB = _ROOT / "src" / "lib"
_PLAN_LINES = _LIB / "util" / "planLines.js"
_FILE_INFO = _LIB / "util" / "fileInfo.js"

_NOT_FOUND = (
    "CHDMAN isn't ready: chdman was not found at /usr/bin/chdman "
    "(set CHDMAN_PATH to where it is)"
)
_NOT_EXECUTABLE = (
    "rom-converto isn't ready: rom-converto at /usr/local/bin/rom-converto "
    "is not executable"
)


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


# ── A missing converter ────────────────────────────────────────────────────


def test_a_not_ready_reason_becomes_one_missing_converter_line(tmp_path):
    out = _eval(tmp_path, _PLAN_LINES, "skipLine", f"""
const a = skipLine({json.dumps(_NOT_FOUND)});
const b = skipLine({json.dumps(_NOT_FOUND)});
const c = skipLine({json.dumps(_NOT_EXECUTABLE)});
process.stdout.write(JSON.stringify({{ a, b, c }}));
""")
    assert out["a"]["kind"] == "missing"
    assert out["a"]["tool"] == "CHDMAN"
    # The same converter groups into one line, whatever games it is for.
    assert out["a"]["key"] == out["b"]["key"]
    # The raw reason is kept, for the fold.
    assert out["a"]["why"] == _NOT_FOUND
    assert out["a"]["installed"] is False
    # A converter that is there but can't run isn't called "not installed".
    assert out["c"]["kind"] == "missing"
    assert out["c"]["installed"] is True


def test_the_missing_line_reads_plainly(tmp_path):
    out = _eval(tmp_path, _PLAN_LINES, "missingLabel", """
process.stdout.write(JSON.stringify([
  missingLabel(3, 'CHDMAN', 'CHD', false),
  missingLabel(1, 'CHDMAN', 'CHD', false),
  missingLabel(2, 'rom-converto', null, true),
]));
""")
    assert out == [
        "3 games need CHDMAN to become CHD, which isn't installed",
        "1 game needs CHDMAN to become CHD, which isn't installed",
        "2 games need rom-converto, which can't run here",
    ]
    for line in out:
        assert "_PATH" not in line and "/usr/" not in line


def test_other_reasons_keep_their_lines(tmp_path):
    out = _eval(tmp_path, _PLAN_LINES, "skipLine", """
process.stdout.write(JSON.stringify([
  skipLine('Already CHD'), skipLine('Unknown console'), skipLine('Console unclear: x'),
  skipLine('Game.chd already exists'),
]));
""")
    assert [line["kind"] for line in out] == ["done", "pick", "pick", "skip"]


def test_the_plan_offers_see_converters_and_folds_the_reason():
    plan = _read("components/panels/ConvertPlan.svelte")
    assert "from '$lib/util/planLines.js'" in plan
    assert "function skipLine" not in plan
    assert "See Converters" in plan
    assert "ui.navigate('settings', { tab: 'converters' })" in plan
    # The tool and its format come from the registry, not a hand-kept list.
    assert "missingFormat" in plan
    assert "registry.all()" in _read("tools/convertTargets.js")
    # A missing line's fold names the files and says the reason once, not
    # once per game.
    missing = plan[plan.index("{#if line.kind === 'missing'}"):]
    assert "line.why" in missing[:missing.index("{/if}")]


# ── Info ───────────────────────────────────────────────────────────────────


def test_info_rows_say_yes_and_no_and_show_a_size_once(tmp_path):
    out = _eval(tmp_path, _FILE_INFO, "infoRows", """
const r = infoRows({
  file: '/v/Game.cso', size: 409600, size_display: '400 KB', compressed: false,
  data_size: 1024, data_size_display: '1 KB', hunk_size: 19584,
}, '/v/Game.cso');
process.stdout.write(JSON.stringify(r));
""")
    rows = {row["label"]: row["value"] for row in out["rows"]}
    assert rows["Compressed"] == "No"
    assert rows["Size"] == "400 KB"
    assert rows["Data size"] == "1 KB"
    assert not any("display" in label.lower() for label in rows)
    assert rows["Hunk size"] == 19584


def test_info_rows_leave_out_what_the_card_shows(tmp_path):
    out = _eval(tmp_path, _FILE_INFO, "infoRows", """
const r = infoRows(
  { format: 'CSO', size: 409600, size_display: '400 KB', version: 2 },
  '/v/Game.cso', { omit: ['format', 'size'] },
);
process.stdout.write(JSON.stringify(r));
""")
    assert [row["label"] for row in out["rows"]] == ["Version"]


def test_the_info_card_is_short_and_plain(tmp_path):
    out = _eval(tmp_path, _FILE_INFO, "infoCard", """
const card = infoCard({
  info: { title: 'Ignored when a match names it', size: 409600, size_display: '400 KB',
          compressed: true, format: 'CSO' },
  match: { matched: true, game_name: 'Final Quest (USA)', platform: 'Sony - PlayStation Portable' },
  format: 'CSO',
  best: 'Yes',
});
process.stdout.write(JSON.stringify(card));
""")
    assert out["rows"] == [
        {"key": "game", "label": "Game", "value": "Final Quest (USA)"},
        {"key": "console", "label": "Console", "value": "Sony - PlayStation Portable"},
        {"key": "format", "label": "Format", "value": "CSO"},
        {"key": "best", "label": "In its best format", "value": "Yes"},
        {"key": "size", "label": "Size", "value": "400 KB"},
    ]
    # The fields the card took, so More details doesn't repeat them.
    assert {"title", "format", "size"} <= set(out["used"])


def test_the_info_card_skips_what_it_does_not_know(tmp_path):
    out = _eval(tmp_path, _FILE_INFO, "infoCard", """
process.stdout.write(JSON.stringify(infoCard({
  info: { name: 'Game.dosz', size: '2 KB' }, match: null, format: 'DOSZ', best: null,
})));
""")
    assert [row["label"] for row in out["rows"]] == ["Format", "Size"]


def test_the_modal_opens_on_the_card_and_folds_the_rest():
    modal = _read("components/modals/FileInfoModal.svelte")
    assert "infoCard(" in modal
    assert "datMatching.matchFor(" in modal
    assert "likelyFormat(" in modal
    assert "<summary>More details</summary>" in modal
    assert "omit: card.used" in modal


# ── A disabled Info says why ───────────────────────────────────────────────


def test_a_disabled_info_says_why(tmp_path):
    out = _eval(tmp_path, _FILE_INFO, "infoUnavailable", """
process.stdout.write(JSON.stringify([
  infoUnavailable({ name: 'Game.dosz', type: 'file', extension: '.dosz' }),
  infoUnavailable({ name: 'Disc 1', type: 'directory' }),
  infoUnavailable({ name: 'disc.iso', type: 'file', extension: '.iso' }, { inArchive: true }),
  infoUnavailable({ name: 'README', type: 'file' }),
]));
""")
    assert out == [
        "no reader for .dosz",
        "not for folders",
        "not inside an archive",
        "no reader for this file",
    ]


def test_the_row_menu_shows_the_reason_on_the_item():
    menu = _read("components/panels/RowActionsMenu.svelte")
    assert "infoUnavailable(" in menu
    assert "`Info (${" in menu


# ── Review follow-ups ──────────────────────────────────────────────────────


def _tools(tmp_path, body: str):
    return run_tools(tmp_path, ["convertTargets.js"], body)


def test_a_file_already_in_its_best_format_has_nothing_to_become(tmp_path):
    """Auto skips an RVZ as "Already RVZ", though Dolphin can recompress it:
    the row's hint and Info must not say Convert makes it RVZ."""
    out = _tools(tmp_path, """
process.stdout.write(JSON.stringify([
  likelyFormat({ type: 'file', path: '/v/Game.rvz' }),
  likelyFormat({ type: 'file', path: '/v/Game.iso' }),
  likelyFormat({ type: 'file', path: '/v/Game.cue' }),
]));
""")
    assert out[0] is None
    assert out[1] is not None
    assert out[2] == "CHD"


def test_the_readers_own_format_wins_over_the_file_name(tmp_path):
    out = _eval(tmp_path, _FILE_INFO, "infoCard", """
process.stdout.write(JSON.stringify([
  infoCard({ info: { format: 'NKit v01 (GameCube)' }, format: 'ISO' }).rows,
  infoCard({ info: { format: '' }, format: 'ISO' }).rows,
]));
""")
    assert out[0] == [{"key": "format", "label": "Format", "value": "NKit v01 (GameCube)"}]
    assert out[1] == [{"key": "format", "label": "Format", "value": "ISO"}]


def test_the_missing_line_names_the_format_the_user_picked(tmp_path):
    out = _tools(tmp_path, """
const dolphin = registry.forTool('dolphin').label;
const chdman = registry.forTool('chdman').label;
const iso = ['/v/Game.iso'];
const wiaMode = registry.forTool('dolphin').modes.find((m) => m.outputExt === '.wia').mode;
process.stdout.write(JSON.stringify({
  best: missingFormat({ tool: dolphin, paths: iso, target: '' }),
  wia: missingFormat({ tool: dolphin, paths: iso, target: '.wia' }),
  wiaMode: missingFormat({ tool: dolphin, paths: iso, target: wiaMode }),
  other: missingFormat({ tool: chdman, paths: iso, target: '.rvz' }),
  unknown: missingFormat({ tool: 'Nobody', paths: iso, target: '' }),
}));
""")
    assert out["best"] == "RVZ"
    assert out["wia"] == "WIA"
    assert out["wiaMode"] == "WIA"
    # A target this converter doesn't make: say nothing rather than contradict.
    assert out["other"] is None
    assert out["unknown"] is None


def test_the_plan_is_told_the_picked_target():
    plan = _read("components/panels/ConvertPlan.svelte")
    assert "missingFormat({" in plan
    assert re.search(r"missingFormat\(\{[^}]*target", plan)
    panel = _read("components/panels/ConvertPanel.svelte")
    assert panel.count("target={conversion.target}") == 2


def test_the_card_builds_rows_without_a_closure():
    """Codacy (Biome) flags a row-adding arrow inside infoCard."""
    src = _FILE_INFO.read_text(encoding="utf-8")
    card = src[src.index("export function infoCard"):]
    card = card[:card.index("\n}\n")]
    assert "=>" not in card
