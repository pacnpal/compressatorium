"""Compatible-with presets (#429): one YAML table, served by one route.

The table maps each emulator target to the modes and codecs it reads, every
entry citing where that came from. The frontend holds no copy. A codec the
target does not read is warned about, never blocked, so nothing here refuses
a job; these tests pin the data against the tool registry so a renamed mode or
codec breaks a test instead of the picker.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import pytest

from .test_frontend_registry_derives_186 import _REGISTRY_JS, _find_node, _strip_imports


def _table() -> dict:
    from services import compat_presets

    return compat_presets.load_presets()


def _frontend_codecs() -> dict[str, list[str]]:
    """Every mode's codec values as the shipped registry.js declares them."""
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend registry")
    src = "const api = {};\n" + _strip_imports(_REGISTRY_JS.read_text(encoding="utf-8"))
    src += (
        "\nprocess.stdout.write(JSON.stringify(Object.fromEntries("
        "TOOLS.flatMap((t) => t.modes.map((m) => [m.mode, "
        "registry.codecsForMode(m.mode).map((c) => c.value)])))));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "codecs.mjs"
        script.write_text(src, encoding="utf-8")
        proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate registry.js via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_the_table_covers_the_targets_with_a_source_for_each_entry():
    table = _table()
    targets = {t["id"]: t for t in table["targets"]}

    assert {"nethersx2", "pcsx2", "duckstation", "ppsspp", "dolphin"} <= set(targets)
    for target in targets.values():
        assert target["label"] and target["entries"]
        for entry in target["entries"]:
            parsed = urlparse(entry["source"])
            assert parsed.scheme == "https" and parsed.netloc, (target["id"], entry)


def test_every_mode_and_codec_in_the_table_exists_in_the_tool_registry():
    from services.tools import registry

    frontend = _frontend_codecs()
    known_modes = {spec.mode for spec in registry.mode_specs()}
    for target in _table()["targets"]:
        for entry in target["entries"]:
            mode = entry["mode"]
            assert mode in known_modes, f"{target['id']}: unknown mode {mode}"
            spec = registry.spec(mode)
            offered = set(frontend[mode])
            assert entry["codecs"], (target["id"], mode)
            for codec in entry["codecs"] + entry["preferred"]:
                assert codec in offered, f"{target['id']}/{mode}: {codec} is not offered"
                if spec.codecs:
                    assert codec in spec.codecs, f"{target['id']}/{mode}: {codec}"
            # The preferred choice is itself something the target reads.
            assert set(entry["preferred"]) <= set(entry["codecs"]), (target["id"], mode)


def test_nethersx2_reads_only_zlib_chds():
    nether = next(t for t in _table()["targets"] if t["id"] == "nethersx2")

    assert nether["entries"]
    for entry in nether["entries"]:
        assert entry["codecs"] == ["zlib"] and entry["preferred"] == ["zlib"]


def test_a_malformed_table_is_refused_not_served(tmp_path):
    from services import compat_presets

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "targets:\n  - id: x\n    label: X\n    entries:\n"
        "      - {mode: createcd, codecs: [zlib], preferred: [zlib]}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source"):
        compat_presets.load_presets(bad)


@pytest.mark.asyncio
async def test_the_route_serves_the_table_and_is_mounted():
    import main
    from routes import compat

    body = await compat.get_compat_presets()

    assert body["targets"] == _table()["targets"]
    assert "/api/compat-presets" in main.app.openapi()["paths"]


def test_both_pickers_read_the_served_table_and_never_block():
    src = Path(__file__).resolve().parents[1] / "src" / "lib"
    picker = (src / "components/panels/CompressionPicker.svelte").read_text(encoding="utf-8")
    editor = (src / "components/views/RuleEditor.svelte").read_text(encoding="utf-8")
    endpoints = (src / "api/endpoints.js").read_text(encoding="utf-8")

    assert "/compat-presets" in endpoints
    for page in (picker, editor):
        assert "Compatible with" in page
        assert "compatWarning" in page
    # The warning is advisory: no disabled state hangs off it.
    assert "disabled={compat" not in picker and "disabled={compat" not in editor


def test_the_warning_names_unread_codecs_and_ignores_none_and_unknown_modes(tmp_path):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    src = (Path(__file__).resolve().parents[1] / "src/lib/tools/compat.js").read_text("utf-8")
    script = tmp_path / "compat_eval.mjs"
    script.write_text(
        src
        + "\nconst t = {label: 'NetherSX2', entries: [{mode: 'createdvd', codecs: ['zlib']}]};\n"
        + "process.stdout.write(JSON.stringify([\n"
        + "  compatWarning(t, 'createdvd', ['zstd']),\n"
        + "  compatWarning(t, 'createdvd', ['zlib']),\n"
        + "  compatWarning(t, 'createdvd', ['none']),\n"
        + "  compatWarning(t, 'dolphin_rvz', ['zstd']),\n"
        + "  compatWarning(null, 'createdvd', ['zstd']),\n"
        + "]));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    warn, ok, none, other_mode, no_target = json.loads(proc.stdout)

    assert "zstd" in warn and "NetherSX2" in warn
    assert ok is None and none is None and other_mode is None and no_target is None


def test_the_server_names_the_codecs_a_tool_uses_when_none_is_chosen():
    """chdman sends no `-c` for an empty selection and uses its own defaults.
    The mode declares them once; the served table carries them to the pickers."""
    from services import compat_presets
    from services.tools import registry

    implicit = compat_presets.served_presets()["implicit_codecs"]

    assert implicit["createcd"] == ["cdlz", "cdzl", "cdfl"]
    assert implicit["createcd"] == list(registry.spec("createcd").implicit_codecs)
    # Only modes that declare some are listed.
    assert all(registry.spec(mode).implicit_codecs for mode in implicit)
    assert "dolphin_rvz" not in implicit


def test_an_empty_selection_is_checked_against_the_tools_implicit_codecs(tmp_path):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    src = (Path(__file__).resolve().parents[1] / "src/lib/tools/compat.js").read_text("utf-8")
    # The shared logic holds no list of its own.
    assert "cdlz" not in src
    script = tmp_path / "compat_empty_eval.mjs"
    script.write_text(
        src
        + "\nconst t = {label: 'NetherSX2', entries: [{mode: 'createcd', codecs: ['zlib']}]};\n"
        + "const implicit = {createcd: ['cdlz', 'cdzl', 'cdfl']};\n"
        + "process.stdout.write(JSON.stringify([\n"
        + "  compatWarning(t, 'createcd', [], implicit),\n"
        + "  compatWarning(t, 'createcd', ['none'], implicit),\n"
        + "  compatWarning(t, 'createcd', ['zlib'], implicit),\n"
        + "  compatWarning(t, 'createcd', [], {}),\n"
        + "]));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    empty, none, zlib, no_defaults = json.loads(proc.stdout)

    assert "cdlz" in empty and "NetherSX2" in empty and "no codec" in empty.lower()
    assert none is None and zlib is None and no_defaults is None


def test_both_views_pass_the_served_implicit_codecs_to_the_warning():
    src = Path(__file__).resolve().parents[1] / "src" / "lib"
    for rel in ("components/panels/CompressionPicker.svelte", "components/views/RuleEditor.svelte"):
        assert "implicit_codecs" in (src / rel).read_text(encoding="utf-8"), rel
