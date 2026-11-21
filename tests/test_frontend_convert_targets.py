"""The "Convert to" choices and the safety choices (restructure PR 1, spec §3.3, §3.4).

``src/lib/tools/convertTargets.js`` groups every mode by what it does and
narrows the list to what the games at hand, a console or the ready tools allow.
``src/lib/util/safetyOptions.js`` decides which safety controls a set of modes
offers. Both are evaluated with Node.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.frontend_tools_harness import run_tools
from tests.test_frontend_registry_derives_186 import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


def _targets(tmp_path, body):
    return run_tools(tmp_path, ["convertTargets.js"], body)


def test_every_mode_lands_in_exactly_one_group(tmp_path):
    got = _targets(tmp_path, """
const groups = convertTargets({ perMode: true });
const listed = groups.flatMap((g) => g.options.map((o) => o.value)).filter(Boolean);
const modes = registry.all().flatMap((t) => t.modes.map((m) => m.mode));
process.stdout.write(JSON.stringify({ listed, modes, names: groups.map((g) => g.group) }));
""")
    assert sorted(got["listed"]) == sorted(got["modes"])
    assert len(got["listed"]) == len(set(got["listed"]))
    assert got["names"] == ["Recommended", "Make smaller", "Unpack", "Other"]


def test_make_smaller_is_one_option_per_format(tmp_path):
    got = _targets(tmp_path, """
const groups = convertTargets();
const by = Object.fromEntries(groups.map((g) => [g.group, g.options]));
process.stdout.write(JSON.stringify(by));
""")
    smaller = {o["value"]: o for o in got["Make smaller"]}
    assert smaller[".chd"]["label"] == "CHD"
    # CHD is made by chdman and by the CSO tool's one-step mode.
    assert "CHDMAN" in smaller[".chd"]["hint"]
    assert smaller[".rvz"]["label"] == "RVZ"
    # A mode whose output follows its input names itself.
    assert smaller["z3ds_compress"]["label"] == "Z3DS"
    assert got["Recommended"] == [{"value": "", "label": "Best format for each game"}]
    unpack = {o["value"]: o["label"] for o in got["Unpack"]}
    assert unpack["extractcd"] == "BIN/CUE from CHD"
    assert unpack["dolphin_iso"] == "ISO from RVZ/WIA/GCZ"
    other = {o["value"] for o in got["Other"]}
    assert {"copy", "pbp_to_iso", "cia_to_cci", "cci_to_cia", "folder_to_iso",
            "exodos_repack"} <= other
    assert not other & set(smaller)


def test_narrowing_by_games_console_and_ready_tools(tmp_path):
    got = _targets(tmp_path, """
const values = (opts) => convertTargets(opts).flatMap((g) => g.options.map((o) => o.value));
process.stdout.write(JSON.stringify({
  cue: values({ entries: [{ type: 'file', path: '/v/Game.cue' }] }),
  chd: values({ entries: [{ type: 'file', path: '/v/Game.chd' }] }),
  ps3: values({ entries: [
    { type: 'directory', path: '/v/BLUS', convertible_by: ['makeps3iso'] },
  ] }),
  folder: values({ entries: [{ type: 'directory', path: '/v/stuff', convertible_by: [] }] }),
  console: values({ modeIds: ['createcd', 'cso_compress'] }),
  ready: values({ ready: ['dolphin'] }),
  member: values({ entries: [{ type: 'file', path: '/v/a.zip::Game.gba' }] }),
}));
""")
    assert ".chd" in got["cue"] and "extractcd" not in got["cue"]
    assert {"extractcd", "copy"} <= set(got["chd"]) and ".chd" not in got["chd"]
    assert "folder_to_iso" in got["ps3"]
    assert got["folder"] == [""]
    assert set(got["console"]) == {"", ".chd", ".cso"}
    assert set(got["ready"]) == {"", ".rvz", ".wia", ".gcz", "dolphin_iso"}
    assert ".7z" in got["member"]


def test_target_label(tmp_path):
    got = _targets(tmp_path, """
const values = ['', '.chd', '.xiso.iso', 'extractcd', 'dolphin_rvz'];
process.stdout.write(JSON.stringify(values.map(targetLabel)));
""")
    assert got == ["Best format for each game", "CHD", "XISO", "BIN/CUE from CHD", "Dolphin RVZ"]


def _safety(tmp_path, body):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "safety_eval.mjs"
    mod = (_SRC / "util" / "safetyOptions.js").as_uri()
    script.write_text(f"import * as s from {json.dumps(mod)};\n{body}", encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_safety_controls_follow_the_modes(tmp_path):
    got = _safety(tmp_path, """
const chd = { supportsDeleteOnVerify: true, supportsRoundTrip: true };
const zar = {
  supportsDeleteOnVerify: false, sourceKeptReason: 'a ZAR holds files, not the disc', games: 2,
};
const required = { supportsDeleteOnVerify: true, supportsRoundTrip: true, roundTripRequired: true };
const extract = { supportsDeleteOnVerify: false };
process.stdout.write(JSON.stringify({
  chd_off: s.safetyControls([chd], { deleteOriginal: false }),
  chd_on: s.safetyControls([chd], { deleteOriginal: true }),
  mixed: s.safetyControls([chd, zar], { deleteOriginal: true }),
  required: s.safetyControls([required], { deleteOriginal: true }),
  extract: s.safetyControls([extract]),
  on: s.applySafety(
    { checkAfter: false, deleteOriginal: false, quickCheck: false }, { deleteOriginal: true },
  ),
  off: s.applySafety(
    { checkAfter: true, deleteOriginal: true, quickCheck: true }, { deleteOriginal: false },
  ),
}));
""")
    assert got["chd_off"] == {
        "showCheck": True, "showDelete": True, "kept": None, "showQuick": False,
        "checkLocked": False,
    }
    assert got["chd_on"]["showQuick"] is True and got["chd_on"]["checkLocked"] is True
    assert got["mixed"]["kept"] == {"games": 2, "reason": "a ZAR holds files, not the disc"}
    assert got["required"]["showQuick"] is False
    assert got["extract"]["showCheck"] is False and got["extract"]["showDelete"] is False
    assert got["on"] == {"checkAfter": True, "deleteOriginal": True, "quickCheck": False}
    assert got["off"] == {"checkAfter": True, "deleteOriginal": False, "quickCheck": False}


def test_the_mode_list_keeps_to_a_romm_platforms_formats():
    """Codex 4651687770: on a RomM platform the shared Convert-to list offered
    every ready tool (CHDMAN Create DVD on GameCube). While the panel shows a
    RomM catalog it is narrowed to that platform's tool_ids and mode_ids."""
    src = (_SRC / "components" / "panels" / "ModeSelect.svelte").read_text(encoding="utf-8")
    assert "fileBrowser.rommPlatformId === null" in src
    assert "romm.platforms.find((p) => p.id === romm.selectedPlatformId)" in src
    assert "modeIds: platform?.mode_ids ?? null" in src
    assert "toolIds: platform?.tool_ids ?? null" in src


def test_narrowing_to_a_gamecube_platform(tmp_path):
    got = _targets(tmp_path, """
const values = convertTargets({
  perMode: true, toolIds: ['dolphin'], modeIds: ['dolphin_rvz', 'dolphin_iso'],
}).flatMap((g) => g.options.map((o) => o.value));
process.stdout.write(JSON.stringify(values));
""")
    assert got == ["", "dolphin_rvz", "dolphin_iso"]


def test_mode_select_lists_every_ready_tools_formats():
    src = (_SRC / "components" / "panels" / "ModeSelect.svelte").read_text(encoding="utf-8")
    assert "convertTargets({" in src and "perMode: true" in src
    assert "conversion.chooseTarget(" in src
