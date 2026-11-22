"""Frontend logic for Create .m3u (#299), evaluated with Node.

* `canCreate` (playlistDialog.js): Create is enabled only when the name the
  user sees is the name that was previewed, nothing is pending and there is no
  error, so Create can never write a stale name.
* `registry.discImages` (registry.js): the file-list button appears only for
  two or more disc Images, using the per-tool `discExts` mirrored from the
  backend (the registry loads them from /api/tools).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_registry_derives_186 import _find_node, eval_registry

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"
_DIALOG = _SRC / "util" / "playlistDialog.js"


def _run_dialog(tmp_path: Path, expr: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "dialog_eval.mjs"
    script.write_text(
        f"import {{ canCreate, classifyPreviewError, playlistPaths, parentDir }} from {json.dumps(_DIALOG.as_uri())};\n"
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
    assert eval_registry(tmp_path, dump, "disc_images_eval.mjs") == [
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


def test_parent_dir_splits_on_slash_only(tmp_path):
    # Paths come from a POSIX server: a backslash is a filename character.
    assert _run_dialog(tmp_path, "parentDir('/vol/Game\\\\ (Disc 2).iso')") == "/vol"
    assert _run_dialog(tmp_path, "parentDir('/vol/a/b.iso')") == "/vol/a"


def test_a_backslash_in_a_file_name_does_not_change_its_folder(tmp_path):
    # /vol/Game (Disc 1).iso and /vol/Game\ (Disc 2).iso share /vol.
    expr = (
        "playlistPaths(['/vol/Game (Disc 1).iso', '/vol/Game\\\\ (Disc 2).iso'])"
    )
    assert _run_dialog(tmp_path, expr) == ["/vol/Game (Disc 1).iso", "/vol/Game\\ (Disc 2).iso"]


def test_discs_in_different_real_folders_hide_the_button(tmp_path):
    assert _run_dialog(tmp_path, "playlistPaths(['/vol/a/x.iso', '/vol/b/y.iso'])") == []
    assert _run_dialog(tmp_path, "playlistPaths(['/vol/a/x.iso'])") == []
