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
    "ui/FolderPicker.svelte",
    "modals/FolderPickerModal.svelte",
    "modals/AddLibraryModal.svelte",
    "panels/SafetyOptions.svelte",
    "panels/CompressionPicker.svelte",
    "panels/ModeSelect.svelte",
    # PR 5: Settings.
    "settings/Connections.svelte",
    "settings/HasheousQuestion.svelte",
    "settings/Identification.svelte",
    "settings/Defaults.svelte",
    "views/NotificationSettings.svelte",
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
