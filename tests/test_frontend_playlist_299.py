"""Frontend logic for Create .m3u (#299), evaluated with Node.

* `canCreate` (playlistDialog.js): Create is enabled only when the name the
  user sees is the name that was previewed, nothing is pending and there is no
  error, so Create can never write a stale name.
* `registry.discImages` (registry.js): the file-list button appears only for
  two or more disc Images, using the per-tool `discExts` mirrored from the
  backend (parity is asserted in test_frontend_parity_186.py).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_parity_186 import _eval_registry, _find_node

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"
_DIALOG = _SRC / "util" / "playlistDialog.js"


def _run_dialog(tmp_path: Path, expr: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "dialog_eval.mjs"
    script.write_text(
        f"import {{ canCreate, classifyPreviewError }} from {json.dumps(_DIALOG.as_uri())};\n"
        f"process.stdout.write(JSON.stringify({expr}));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_READY = (
    "{ preview: {}, title: 'Game', previewedTitle: 'Game',"
    " loading: false, busy: false, titleError: '' }"
)


def test_create_is_enabled_when_the_shown_name_is_the_previewed_one(tmp_path):
    assert _run_dialog(tmp_path, f"canCreate({_READY})") is True


@pytest.mark.parametrize(
    "override",
    [
        "title: 'My Game'",            # typed, preview not caught up (the debounce window)
        "loading: true",
        "busy: true",
        "titleError: 'The playlist name can\\'t be empty'",
        "preview: null",
    ],
)
def test_create_is_disabled_for_a_stale_pending_or_invalid_name(tmp_path, override):
    assert _run_dialog(tmp_path, f"canCreate({{ ...{_READY}, {override} }})") is False


def test_disc_images_filters_by_the_mirrored_disc_extensions(tmp_path):
    dump = (
        "\nprocess.stdout.write(JSON.stringify(registry.discImages("
        "['/a/x.cue', '/a/x.bin', '/a/y.nsz', '/a/Z.CHD', '/a/w.rvz'])));\n"
    )
    assert _eval_registry(tmp_path, dump, "disc_images_eval.mjs") == [
        "/a/x.cue", "/a/Z.CHD", "/a/w.rvz",
    ]


def test_client_uses_optional_chaining_for_the_error_body():
    src = (_SRC / "api" / "client.js").read_text(encoding="utf-8")
    assert "body && body.detail" not in src
    assert "body && typeof body.detail" not in src


def test_a_name_error_keeps_the_input_and_prefills_the_derived_title(tmp_path):
    err = (
        "{ message: 'The playlist name is too long',"
        " detail: { code: 'invalid_name', title: 'LONG' } }"
    )
    assert _run_dialog(tmp_path, f"classifyPreviewError({err}, null)") == {
        "kind": "name", "message": "The playlist name is too long", "title": "LONG",
    }
    # A typed name that is refused keeps what the user typed.
    assert _run_dialog(tmp_path, f"classifyPreviewError({err}, 'typed')")["title"] == "typed"


def test_other_errors_are_selection_errors(tmp_path):
    assert _run_dialog(
        tmp_path, "classifyPreviewError({ message: 'Select two or more discs' }, null)",
    ) == {"kind": "selection", "message": "Select two or more discs", "title": None}
