"""Frontend rules for the Patch dialog (#428), evaluated with Node.

* `canPatch` (patchDialog.js): the row action is offered for a loose file, a
  .zip/.7z, or a member inside one, and never for a folder, a RAR or a disc
  Image (the registry's `discExts`, mirrored from the backend's
  ``disc_extensions``; parity is asserted in test_frontend_parity_186.py).
* `patchedName`: the name the dialog promises, the one the backend writes.
* `patchRequest`: exactly one patch source in the request.

The wiring (row action, modal, endpoints) is asserted on the source.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_parity_186 import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"
_DIALOG = _SRC / "util" / "patchDialog.js"
_MENU = _SRC / "components" / "panels" / "RowActionsMenu.svelte"
_MODAL = _SRC / "components" / "modals" / "PatchModal.svelte"
_ENDPOINTS = _SRC / "api" / "endpoints.js"
_LEDGER = _SRC / "components" / "views" / "LedgerView.svelte"


def _run(tmp_path: Path, expr: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "patch_eval.mjs"
    script.write_text(
        f"import {{ canPatch, patchedName, patchRequest, isPatchFile }} "
        f"from {json.dumps(_DIALOG.as_uri())};\n"
        "const isDisc = (p) => /\\.(iso|cue|chd|rvz|cso)$/i.test(p);\n"
        f"process.stdout.write(JSON.stringify({expr}));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    ("entry", "offered"),
    [
        ("{ type: 'file', path: '/v/Game.sfc' }", True),
        ("{ type: 'archive', path: '/v/Game.zip' }", True),
        ("{ type: 'archive', path: '/v/Game.7z' }", True),
        ("{ type: 'file', path: '/v/Pack.zip::Hack/Base.nes' }", True),
        ("{ type: 'archive', path: '/v/Game.rar' }", False),
        ("{ type: 'file', path: '/v/Pack.rar::Base.nes' }", False),
        ("{ type: 'directory', path: '/v/roms' }", False),
        ("{ type: 'file', path: '/v/Disc.iso' }", False),
        ("{ type: 'file', path: '/v/Disc.zip::Disc.cue' }", False),
        ("null", False),
    ],
)
def test_the_patch_action_is_offered_for_cartridge_images_only(tmp_path, entry, offered):
    assert _run(tmp_path, f"canPatch({entry}, isDisc)") is offered


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/v/Game.sfc", "Game (Patched).7z"),
        ("/v/Pack.zip::Hack/Base.nes", "Base (Patched).7z"),
        # A whole archive is named after the ROM inside, which the server reads.
        ("/v/Game.zip", None),
    ],
)
def test_the_promised_name_is_the_one_the_backend_writes(tmp_path, path, name):
    assert _run(tmp_path, f"patchedName({json.dumps(path)})") == name


def test_a_request_names_exactly_one_patch(tmp_path):
    data = _run(tmp_path, """[
      patchRequest('/v/Game.sfc', { source: 'volume', patchPath: ' /v/Fix.ips ' }),
      patchRequest('/v/Game.sfc', { source: 'upload', uploadId: 'abc-Fix.bps', patchPath: '/x' }),
    ]""")
    assert data == [
        {"image_path": "/v/Game.sfc", "patch_path": "/v/Fix.ips"},
        {"image_path": "/v/Game.sfc", "upload_id": "abc-Fix.bps"},
    ]


def test_only_ips_and_bps_files_are_patches(tmp_path):
    assert _run(tmp_path, "['a.IPS', 'b.bps', 'c.xdelta', 'd.ips.txt', ''].map(isPatchFile)") == [
        True, True, False, False, False,
    ]


def test_the_row_action_modal_and_endpoints_are_wired():
    menu = _MENU.read_text(encoding="utf-8")
    assert "canPatch(" in menu and "ui.patchTarget = entry" in menu
    modal = _MODAL.read_text(encoding="utf-8")
    assert "api.uploadPatch(" in modal and "api.startPatch(" in modal
    endpoints = _ENDPOINTS.read_text(encoding="utf-8")
    assert "/patch/upload" in endpoints and "`${API_BASE}/patch`" in endpoints


def test_the_ledger_view_names_a_rows_patch_file():
    assert "extra_inputs" in _LEDGER.read_text(encoding="utf-8")
