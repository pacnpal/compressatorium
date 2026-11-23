"""Counts read "1 game", not "1 games" (#575).

Game identification wrote ``{dat.file_count} games`` and RomM's Browse
summary ``{summary.total} games`` for every count. One ``plural`` helper in
``src/lib/api/format.js`` serves them and the Libraries cards, which kept their
own. (``rules/sweepSummary.js`` keeps its copy: its tests run it standalone.)
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_delete_check_298 import _find_node

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"


def test_plural_counts_one_and_many(tmp_path):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ plural }} from {json.dumps((_LIB / 'api' / 'format.js').as_uri())};\n"
        "process.stdout.write(JSON.stringify([0, 1, 2].map((n) => plural(n, 'game', 'games'))));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["0 games", "1 game", "2 games"]


@pytest.mark.parametrize("rel", [
    "components/settings/Identification.svelte",
    "components/library/RommBrowse.svelte",
    "components/views/LibrariesView.svelte",
])
def test_every_count_goes_through_the_shared_helper(rel):
    source = (_LIB / rel).read_text(encoding="utf-8")
    assert not re.search(r"\}\s+games\b", source), rel
    assert "function plural" not in source and "const plural" not in source, rel
