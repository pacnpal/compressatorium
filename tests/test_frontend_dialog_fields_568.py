"""A dialog's fields come before its buttons, and Rename starts in its field (#568).

Add a Library showed Cancel / Make this a Library, then the Name field under
them, because FolderPickerModal rendered its extra fields (and its error line)
after the picker, whose own action row ended it. Rename opened with focus on
the dialog's close button.
"""
from __future__ import annotations

from pathlib import Path

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib" / "components"


def _read(rel: str) -> str:
    return (_LIB / rel).read_text(encoding="utf-8")


def test_the_picker_renders_fields_and_errors_above_its_buttons():
    picker = _read("ui/FolderPicker.svelte")
    markup = picker[picker.index("</script>"):]
    assert markup.index("{@render fields()}") < markup.index('<div class="actions">')

    # The modal hands its fields and its error line to the picker, inside it,
    # instead of rendering them after it.
    modal = _read("modals/FolderPickerModal.svelte")
    given = modal[modal.index("{#snippet fields()}"):modal.index("</FolderPicker>")]
    assert "{@render extra()}" in given
    assert 'class="error"' in given
    assert modal.count("{@render extra()}") == 1


def test_a_dialog_opens_on_its_marked_field():
    base = _read("modals/BaseModal.svelte")
    assert "onOpenAutoFocus" in base
    assert "[data-autofocus]" in base
    rename = _read("modals/RenameModal.svelte")
    assert "data-autofocus" in rename


def test_a_long_drive_path_never_squeezes_the_drive_name():
    css = _read("ui/FolderPicker.svelte").split("<style>", 1)[1]
    assert ".name { flex-shrink: 0;" in css
    assert ".row > :global(svg) { flex-shrink: 0; }" in css


def test_the_delete_plan_count_reads_inside_its_sentence():
    """#573: a wrapping flex row put the count badge on a line of its own and
    started the sentence below it in lower case."""
    css = _read("modals/DeletePlanModal.svelte").split("<style>", 1)[1]
    lead = css[css.index(".dp-lead {"):]
    lead = lead[:lead.index("}")]
    assert "display: flex" not in lead


def test_every_dialog_with_a_name_to_type_opens_on_it():
    """`autofocus` does nothing inside a bits-ui dialog (it moves focus itself),
    so a dialog's field is marked for BaseModal instead."""
    for rel in ("views/LibraryView.svelte", "modals/RenameModal.svelte"):
        source = _read(rel)
        assert "data-autofocus" in source, rel
        assert " autofocus" not in source, rel
