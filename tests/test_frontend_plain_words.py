"""The UI speaks in plain words (restructure item 20, spec §2).

Each file in ``CHECKED_FILES`` has its ``<script>``, ``<style>`` and HTML
comments removed, and the markup that remains (what a user reads) must not use
the old internal words: Image, Platform, Finding, Ledger, Volume, Intact, and
the rest. Variables, API fields and CSS classes keep their names; only the
copy changes. Each restructure PR adds the files it rewrites.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_COMPONENTS = Path(__file__).resolve().parents[1] / "src" / "lib" / "components"

CHECKED_FILES = [
    # PR 1: the navigation shell and the shared pieces.
    "layout/Sidebar.svelte",
    "layout/SidebarItem.svelte",
    "layout/TopBar.svelte",
    "views/FilesView.svelte",
    "views/LibrariesView.svelte",
    "views/ActivityView.svelte",
    "views/SettingsView.svelte",
    "views/HelpView.svelte",
    "settings/Converters.svelte",
    "settings/ChdMetadataScan.svelte",
    "ui/FolderPicker.svelte",
    "modals/FolderPickerModal.svelte",
    "modals/AddLibraryModal.svelte",
    "panels/SafetyOptions.svelte",
    "panels/CompressionPicker.svelte",
    "panels/ConvertPanel.svelte",
    "panels/ConvertPlan.svelte",
    "panels/FileList.svelte",
    "panels/FileRow.svelte",
    "modals/DeletePlanModal.svelte",
    # PR 3: Libraries.
    "views/LibraryView.svelte",
    "views/LibraryPlacement.svelte",
    "views/RommSettings.svelte",
    "library/LibraryBrowse.svelte",
    "library/RommBrowse.svelte",
    "library/LibraryAutomation.svelte",
    "library/RuleTable.svelte",
    "library/RuleAdvanced.svelte",
    "library/WatchFolders.svelte",
    "library/FrontendSelect.svelte",
    # PR 5: Settings.
    "settings/Connections.svelte",
    "settings/HasheousQuestion.svelte",
    "settings/Identification.svelte",
    "settings/Defaults.svelte",
    "views/NotificationSettings.svelte",
    # PR 4: Activity.
    "activity/ActivitySummary.svelte",
    "activity/NeedsAttention.svelte",
    "activity/RunningList.svelte",
    "activity/HistoryList.svelte",
    "modals/MappingModal.svelte",
    # #576: pieces the restructure's files use but didn't list.
    "panels/VolumeList.svelte",
    "panels/RowActionsMenu.svelte",
    "modals/DatRenameModal.svelte",
    "modals/BulkVerifyModal.svelte",
    "modals/PatchModal.svelte",
]

OLD_WORDS = re.compile(
    r"\b(Image|Images|Platform|Platforms|Finding|Findings|Ledger|Volume|Volumes|Intact"
    r"|Unsorted|Conflicted|settle|Settle|Referrer|Inbox|Inboxes|Workspace)\b"
)


def _markup(text: str) -> str:
    for pattern in (r"<script[\s\S]*?</script>", r"<style[\s\S]*?</style>", r"<!--[\s\S]*?-->"):
        text = re.sub(pattern, "", text)
    return text


@pytest.mark.parametrize("rel", CHECKED_FILES)
def test_the_markup_uses_plain_words(rel):
    markup = _markup((_COMPONENTS / rel).read_text(encoding="utf-8"))
    hits = sorted({m.group(0) for m in OLD_WORDS.finditer(markup)})
    assert not hits, f"{rel} shows {', '.join(hits)}; see the rename table in the spec"


def test_the_guard_catches_an_old_word():
    assert OLD_WORDS.search(_markup("<script>const Image = 1;</script><p>Pick a Platform</p>"))
    assert not OLD_WORDS.search(_markup("<script>const Image = 1;</script><FindingsView />"))


def test_the_dat_rename_count_reads_inside_its_sentence():
    """#576, as #573 for the delete-original confirmation: a wrapping flex
    row put the count badge on a line of its own above the sentence."""
    css = (_COMPONENTS / "modals/DatRenameModal.svelte").read_text(encoding="utf-8")
    css = css.split("<style>", 1)[1]
    lead = css[css.index(".dr-lead {"):]
    assert "display: flex" not in lead[:lead.index("}")]
