"""The navigation shell (restructure PR 1, item 8), asserted against the source.

Four places plus Help in the sidebar, the activity chip in the top bar, the
view switch in App, and the shells that host the existing screens.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_COMPONENTS = _SRC / "lib" / "components"


def _read(rel: str) -> str:
    return (_COMPONENTS / rel).read_text(encoding="utf-8")


def test_the_sidebar_is_four_places_and_a_help_footer():
    src = _read("layout/Sidebar.svelte")
    places = re.findall(r"\{ view: '(\w+)', label: '(\w+)'", src)
    assert places == [
        ("files", "Files"), ("libraries", "Libraries"),
        ("activity", "Activity"), ("settings", "Settings"),
    ]
    footer = src.split('<div class="section footer">', 1)[1]
    assert 'label="Help"' in footer and "toggleSidebar" in footer
    assert "section-title" not in src, "the Navigate / Tool headings are gone"
    assert "visibleTools" not in src, "no per-tool section"
    style = src.split("<style>", 1)[1]
    assert "position: sticky;" in style and "height: 100dvh;" in style
    assert "margin-top: auto;" in style


def test_activity_counts_what_needs_you_first():
    src = _read("layout/Sidebar.svelte")
    assert "attention > 0 ? attention : running > 0 ? running : null" in src
    assert "attention > 0 ? 'warning' : 'info'" in src
    item = _read("layout/SidebarItem.svelte")
    assert "<Badge tone={badgeTone}>{badge}</Badge>" in item
    assert 'class="dot tone-{badgeTone}"' in item, "a collapsed item shows a dot"


def test_the_top_bar_chip_links_to_running():
    src = _read("layout/TopBar.svelte")
    assert "jobs.processingCount" in src and "jobs.queuedCount" in src
    assert "hashFor('activity', { tab: 'running' })" in src
    assert "VIEW_TITLES[view]" in src
    assert "hashFor('libraries')" in src, "a Library page has Libraries › before its name"


def test_app_switches_between_the_five_views():
    app = (_SRC / "App.svelte").read_text(encoding="utf-8")
    views = re.findall(r"ui\.activeView === '(\w+)'\}<(\w+) />", app)
    assert views == [
        ("files", "FilesView"), ("libraries", "LibrariesView"),
        ("activity", "ActivityView"), ("settings", "SettingsView"), ("help", "HelpView"),
    ]
    assert "defaults.load()" in app
    for gone in ("Dashboard", "WorkArea", "workspaceTool"):
        assert gone not in app, gone


def test_the_retired_pieces_are_gone():
    for rel in (
        "views/Dashboard.svelte", "views/WorkArea.svelte",
        "dashboard/QuickToolsCard.svelte", "dashboard/MetadataScanCard.svelte",
        "dashboard/VolumeOverviewCard.svelte", "dashboard/RecentConversionsCard.svelte",
    ):
        assert not (_COMPONENTS / rel).exists(), rel
    assert not (_SRC / "lib" / "tools" / "helpModes.js").exists()
    ui = (_SRC / "lib" / "stores" / "ui.svelte.js").read_text(encoding="utf-8")
    for gone in ("showHelp", "workspaceTool", "VALID_TOOLS", "rulesTarget"):
        assert gone not in ui, gone


def test_files_hosts_the_old_workspace_without_the_jobs_panel():
    src = _read("views/FilesView.svelte")
    assert "<ConvertPanel />" in src and "<FileList />" in src
    assert "JobsPanel" not in src
    assert "files.firstRunDismissed" in src
    assert "Make this folder a Library" in src and "In Library: {inLibrary.name}" in src
    assert "fileBrowser.openFolder(path)" in src, "#/files/<path> opens that folder"


def test_the_shells_host_the_existing_screens():
    libs = _read("views/LibrariesView.svelte")
    assert "{#if id === ROMM_LIBRARY_ID}\n  <RommView />" in libs
    assert "<AddLibraryModal" in libs
    activity = _read("views/ActivityView.svelte")
    for part in ("<FindingsView />", "<JobsPanel />", "<LedgerView />",
                 "<SavingsCard />", "<QueueSummaryCard />"):
        assert part in activity, part
    settings = _read("views/SettingsView.svelte")
    for section in ("connections", "identification", "notifications", "converters"):
        assert f'id="settings-{section}"' in settings, section
    assert "<DATView />" in settings and "<Converters />" in settings
    romm = _read("views/RommView.svelte")
    assert "ui.navigate('libraries', { id: 'romm', tab: ROUTE_OF_TAB[id] })" in romm


def test_help_is_short_and_links_to_the_readme():
    src = _read("views/HelpView.svelte")
    assert len(src.splitlines()) < 130
    for anchor in ("#supported-conversions", "#romm-integration", "#inboxes",
                   "#verifying-and-deleting-safely", "#dat-matching", "#troubleshooting"):
        assert anchor in src, anchor
    assert "DOCKER-COMPOSE.md" in src


def test_the_folder_picker_never_takes_a_typed_path():
    src = _read("ui/FolderPicker.svelte")
    assert "<input" not in src
    assert "api.getVolumes()" in src and "api.listFiles(folder, false)" in src
    assert "No folders in here." in src and "Couldn't open this folder." in src
    modal = _read("modals/FolderPickerModal.svelte")
    assert "<FolderPicker" in modal and "<BaseModal" in modal
