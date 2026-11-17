"""Guards for the multi-file rename-to-DAT-name UI (#426), asserted on the
source as ``test_frontend_dat_rename_425`` does."""
from __future__ import annotations

from pathlib import Path

_MODAL = (
    Path(__file__).resolve().parents[1]
    / "src" / "lib" / "components" / "modals" / "DatRenameModal.svelte"
)


def test_the_preview_lists_every_file_the_folder_and_the_sheet_rewrite():
    modal = _MODAL.read_text()
    assert "otherFiles(item)" in modal
    assert "item.folder" in modal
    assert "item.sheet_rewrite" in modal


def test_the_run_sends_each_files_new_name_as_the_preview_showed_it():
    modal = _MODAL.read_text()
    assert "items.map(confirmed)" in modal
    confirmed = modal.split("function confirmed(i)", 1)[1].split("\n  }\n", 1)[0]
    assert "path: i.path, new_path: i.new_path ?? null" in confirmed
    assert "files:" in confirmed and "f.new_path" in confirmed
