"""Guards for the rename-to-DAT-name UI (#425), asserted on the source as
``test_frontend_duplicate_delete_389`` asserts its wiring."""
from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_MODAL = _SRC / "lib" / "components" / "modals" / "DatRenameModal.svelte"


def _read(*parts: str) -> str:
    return (_SRC.joinpath(*parts)).read_text()


def test_the_selection_bar_offers_rename_to_dat_name_on_filesystem_paths():
    file_list = _read("lib", "components", "panels", "FileList.svelte")
    assert "Rename to DAT name" in file_list
    assert "ui.datRenamePaths =" in file_list
    # The same filesystem-path filter as Delete: no archive members, no split sets.
    opener = file_list.split("function openDatRename()", 1)[1].split("}\n", 1)[0]
    assert "!e.path.includes('::')" in opener and "e.split_parts == null" in opener


def test_the_run_sends_the_rename_confirmation_header_and_the_preview_as_shown():
    endpoints = _read("lib", "api", "endpoints.js")
    run = endpoints.split("datRename: (items) =>", 1)[1].split("),\n", 1)[0]
    assert "/files/dat-rename`" in run
    assert "CONFIRM.RENAME_FILE" in run
    assert "/files/dat-rename/preview" in endpoints
    modal = _MODAL.read_text()
    # Every preview item goes back, skipped ones with no new path.
    assert "items.map((i) => ({ path: i.path, new_path: i.new_path ?? null }))" in modal


def test_the_dialog_is_mounted_follows_its_job_and_blocks_background_refresh():
    assert "<DatRenameModal />" in _read("App.svelte")
    modal = _MODAL.read_text()
    assert "jobs.jobs.find((j) => j.id === jobId)" in modal
    assert "job?.result?.results" in modal
    # A rename can finish before the job stream subscribes to it (it adds
    # running jobs every 0.1 s), so the dialog reads its job from the API
    # until it ends rather than wait on the stream alone.
    follow = modal.split("async function follow(", 1)[1].split("\n  }\n", 1)[0]
    assert "api.getJob(id)" in follow
    assert "jobs.adopt([" in follow
    assert "TERMINAL.has(" in follow
    # The read is added to the jobs list before the dialog-session check, so
    # a closed dialog still lands the job in the list (CodeRabbit 4602644957).
    assert follow.index("jobs.adopt([") < follow.index("if (mine !== request)")
    # A job the server no longer has stops the loop with an error, rather
    # than polling forever (CodeRabbit 4602644995).
    assert "e?.status === 404" in follow


def test_the_start_answer_stays_in_the_dialog_session_that_asked():
    """A start answered after the dialog closed and reopened on another
    selection never lands in the new one (CodeRabbit 4602644943)."""
    modal = _MODAL.read_text()
    confirm = modal.split("async function confirm()", 1)[1].split("\n  }\n", 1)[0]
    assert "const mine = request;" in confirm
    assert confirm.index("const mine = request;") < confirm.index("await api.datRename(")
    after = confirm.split("await api.datRename(", 1)[1]
    assert "if (mine !== request)" in after
    # The job is still followed (and listed) whichever dialog is open.
    assert "follow(id, mine)" in after
    ui = _read("lib", "stores", "ui.svelte.js")
    assert "!!this.datRenamePaths" in ui


def test_the_job_and_the_finding_have_labels():
    assert "dat_rename: 'Rename to DAT name'" in _read("lib", "stores", "jobConstants.js")
    view = _read("lib", "components", "views", "FindingsView.svelte")
    assert "rename_conflict: 'Rename conflict'" in view
    # A rename conflict is about DATs, not the folder's Platform.
    assert "row.kind !== 'rename_conflict'" in view
