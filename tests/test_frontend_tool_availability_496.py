"""The tool list is drawn only once the backend has said which tools can run (#496).

``ui.hiddenTools`` used to start empty, so after a reload the sidebar and the
dashboard's quick tools card listed every tool and then dropped the unavailable
ones when ``GET /api/tools`` answered. The store now owns one "availability
known yet" flag and a ``visibleTools`` list that is empty until it is set.

``src/lib/stores/ui.svelte.js`` is evaluated with Node: imports dropped, runes
reduced to their initial values, siblings stubbed. Skipped without Node.
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
_STORE_JS = _SRC / "lib" / "stores" / "ui.svelte.js"
# The store reads its routes from this pure file; the harness inlines it.
_ROUTES_JS = _SRC / "lib" / "router" / "routes.js"

_STUBS = """
const toast = { dismiss() {}, success() {}, warning() {} };
const STORAGE_KEYS = { PRIMARY_TOOL: 'p', SIDEBAR_COLLAPSED: 's' };
const readBool = (_k, fallback) => fallback;
const writeBool = () => {};
const readString = (_k, fallback) => fallback;
const writeString = () => {};
const api = {};
const TOOLS = ['chdman', 'dolphin', 'nsz'].map((id) => ({ id, label: id }));
const registry = {
  all: () => TOOLS,
  ids: () => new Set(TOOLS.map((t) => t.id)),
};
"""


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _plain_js() -> str:
    src = _STORE_JS.read_text(encoding="utf-8")
    body = re.sub(r"^import[\s\S]*?from\s+'[^']+';\s*$", "", src, flags=re.MULTILINE)
    body = re.sub(r"^export \{[^}]*\};\s*$", "", body, flags=re.MULTILINE)
    routes = _ROUTES_JS.read_text(encoding="utf-8").replace("export ", "")
    body = routes + body
    body = re.sub(r"\$state\(", "(", body)
    leftover = [r for r in ("$state.raw(", "$derived(", "$effect(") if r in body]
    assert not leftover, f"ui.svelte.js grew runes this harness does not reduce: {leftover}"
    return body


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the ui store")
    script = tmp_path / "ui_store_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the ui store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def _ids(list_name: str) -> str:
    return f"{list_name}.map((t) => t.id)"


def test_no_tool_is_drawn_before_availability_is_known(tmp_path):
    data = _run(tmp_path, f"""
const store = new UIStore();
const before = {_ids('store.visibleTools')};
const knownBefore = store.toolsKnown;
store.applyToolAvailability(['chdman', 'dolphin']);   // nsz reported unavailable
process.stdout.write(JSON.stringify({{
  before, knownBefore, after: {_ids('store.visibleTools')}, knownAfter: store.toolsKnown,
}}));
""")
    assert data["before"] == [], data
    assert data["knownBefore"] is False, data
    assert data["after"] == ["chdman", "dolphin"], data
    assert data["knownAfter"] is True, data


def test_a_failed_fetch_shows_every_tool(tmp_path):
    data = _run(tmp_path, f"""
const store = new UIStore();
store.applyToolAvailability(undefined);
process.stdout.write(JSON.stringify({{ shown: {_ids('store.visibleTools')} }}));
""")
    assert data["shown"] == ["chdman", "dolphin", "nsz"], data


def test_a_hidden_convert_tool_falls_back_to_a_visible_one():
    """The Convert panel's tool follows availability: App moves it off a tool
    the backend can't run, onto the first one it can."""
    app = (_SRC / "App.svelte").read_text(encoding="utf-8")
    assert "const tools = ui.visibleTools;" in app
    assert "conversion.setPrimaryTool(tools[0].id)" in app


def test_the_consumers_read_the_store_and_a_failed_fetch_is_retried():
    for rel in ("components/panels/ModeSelect.svelte", "components/settings/Converters.svelte"):
        text = (_SRC / "lib" / rel).read_text(encoding="utf-8")
        assert "ui.visibleTools" in text or "ui.toolsKnown" in text, rel
        assert "hiddenTools" not in text, rel
    # The registry itself comes from /api/tools (#539), so a failed fetch is
    # retried rather than shown as "every tool": with no payload there is none.
    app = (_SRC / "App.svelte").read_text(encoding="utf-8")
    assert "registry.load(t)" in app
    assert "ui.applyToolAvailability(t?.available)" in app
    assert "setTimeout(() => loadTools(" in app
