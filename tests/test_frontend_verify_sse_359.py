"""The single-file verify stream's answer for a file that could not be checked (#359).

``verifyEventSource`` (``src/lib/api/sse.js``) is what every tool's Verify in
the file menu goes through (``tool.verify`` -> ``verification.verifyOne``). A
``verify_error`` with no verdict, ``uncheckable`` (refused here) or
``inconclusive`` (the file could not be opened), is an
answer, not an error: it resolves with its flags and message, so the menu
warns instead of reporting a failure. A real failure still rejects.

Evaluated with Node, with the module's ``export`` dropped and a fake
``EventSource`` that delivers one event.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SSE_JS = Path(__file__).resolve().parents[1] / "src" / "lib" / "api" / "sse.js"
_MENU = (
    Path(__file__).resolve().parents[1]
    / "src" / "lib" / "components" / "panels" / "RowActionsMenu.svelte"
)


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and Path(cand).exists():
            return cand
    return None


_FAKE = """
let __es;
class EventSource {
  constructor(url) { this.url = url; this.listeners = {}; __es = this; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() {}
  emit(name, data) { this.listeners[name]({ data: JSON.stringify(data) }); }
}
"""


def _answer(tmp_path: Path, event: str, data: dict) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate sse.js")
    body = _SSE_JS.read_text(encoding="utf-8").replace("export function", "function")
    scenario = f"""
const answer = verifyEventSource('/api/verify?path=x', null);
__es.emit({json.dumps(event)}, {json.dumps(data)});
answer.then(
  (value) => console.log(JSON.stringify({{ resolved: value }})),
  (error) => console.log(JSON.stringify({{ rejected: error.message }})),
);
"""
    script = tmp_path / "sse_eval.mjs"
    script.write_text(_FAKE + body + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate sse.js:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_an_inconclusive_error_resolves_with_its_flags(tmp_path):
    message = "Could not open Game.chd to check it: [Errno 13] Permission denied"

    out = _answer(tmp_path, "verify_error", {
        "type": "error", "valid": False, "inconclusive": True, "message": message,
    })

    assert out == {"resolved": {"valid": False, "inconclusive": True, "message": message}}


def test_an_uncheckable_error_still_resolves_as_before(tmp_path):
    out = _answer(tmp_path, "verify_error", {
        "type": "error", "valid": False, "uncheckable": True, "message": "Refused",
    })

    assert out == {"resolved": {"valid": False, "uncheckable": True, "message": "Refused"}}


def test_a_real_failure_still_rejects(tmp_path):
    out = _answer(tmp_path, "verify_error", {
        "type": "error", "valid": False, "message": "Integrity check failed",
    })

    assert out == {"rejected": "Integrity check failed"}


def test_the_menu_warns_on_either_no_verdict_answer():
    """The file menu's Verify takes the warning branch for both flags."""
    menu = _MENU.read_text(encoding="utf-8")

    assert "result?.uncheckable || result?.inconclusive" in menu
