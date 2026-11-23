"""Coming back after the backend restarts reads what a restart can change (#569).

A container restarted with other Volumes (an upgrade, a compose edit) left an
open tab on the old Volume list: clicking one answered "Access denied: path
outside configured volumes", and the version and the converters' ready list
were the ones read at page load. Each reconnect of the live jobs feed after
the first now bumps ``ui.reconnects``; App reads the version, the tools and
the Volumes again on it, and the file browser leaves a Volume that's gone.
"""
from __future__ import annotations

from pathlib import Path

from .test_frontend_file_browser_open_folder import _run as _run_browser
from .test_frontend_tool_availability_496 import _run as _run_ui

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_only_a_reconnect_counts(tmp_path):
    data = _run_ui(tmp_path, """
const seen = [];
ui.reportConnection('open');
seen.push(ui.reconnects);
ui.reportConnection('reconnecting');
seen.push(ui.reconnects);
ui.reportConnection('open');
seen.push(ui.reconnects);
ui.reportConnection('reconnecting');
ui.reportConnection('open');
seen.push(ui.reconnects);
clearTimeout(ui._disconnectTimer);
process.stdout.write(JSON.stringify(seen));
""")
    # The feed opens again only after it dropped: every opening after the
    # first is the backend coming back.
    assert data == [0, 0, 1, 2], data


def test_a_volume_still_there_is_kept_as_it_is(tmp_path):
    data = _run_browser(tmp_path, """
await fileBrowser.fetchVolumes();
await within(fileBrowser.selectVolume(fileBrowser.volumes[1]), 300);
await within(fileBrowser.navigate('/target/roms'), 300);
api.getVolumes = async () => [
  { name: 'Target', path: '/target' },
  { name: 'Added', path: '/added' },
];
await within(fileBrowser.refreshVolumes(), 300);
process.stdout.write(JSON.stringify({
  volumes: fileBrowser.volumes.map((v) => v.path),
  volume: fileBrowser.selectedVolume?.path ?? null,
  path: fileBrowser.currentPath,
}));
""")
    assert data == {
        "volumes": ["/target", "/added"], "volume": "/target", "path": "/target/roms",
    }, data


def test_app_reads_the_version_tools_and_volumes_again_on_a_reconnect():
    app = (_SRC / "App.svelte").read_text(encoding="utf-8")
    effect = app[app.index("ui.reconnects"):]
    effect = effect[:effect.index("\n  });")]
    for call in ("ui.loadVersion()", "loadTools()", "fileBrowser.refreshVolumes()"):
        assert call in effect, call
    # Codex on #586: the tool registry isn't reactive, so a backend that came
    # back as another version (an upgrade: other tools, another page) reloads
    # the page instead of patching a registry nothing re-reads.
    assert "location.reload()" in effect
    assert effect.index("ui.loadVersion()") < effect.index("location.reload()")
    assert effect.index("location.reload()") < effect.index("loadTools()")


def test_no_volume_left_drops_the_search_selection_and_archive(tmp_path):
    """Codex on #586: with every Volume gone during an Everywhere search, the
    results and the selection survived, so Convert could still send paths
    from a Volume that's no longer mounted."""
    data = _run_browser(tmp_path, """
await fileBrowser.fetchVolumes();
await within(fileBrowser.selectVolume(fileBrowser.volumes[1]), 300);
fileBrowser.searchMode = true;
fileBrowser.searchResults = { files: [{ name: 'x.iso', path: '/target/x.iso' }], archives: [] };
fileBrowser.currentArchivePath = '/target/a.zip';
fileBrowser.selectedFiles.set('/target/x.iso', { path: '/target/x.iso' });
api.getVolumes = async () => [];
await within(fileBrowser.refreshVolumes(), 300);
process.stdout.write(JSON.stringify({
  volume: fileBrowser.selectedVolume, path: fileBrowser.currentPath,
  searchMode: fileBrowser.searchMode, results: fileBrowser.searchResults,
  archive: fileBrowser.currentArchivePath, selected: fileBrowser.selectedFiles.size,
  entries: fileBrowser.entries.length,
}));
""")
    assert data == {
        "volume": None, "path": None, "searchMode": False, "results": None,
        "archive": None, "selected": 0, "entries": 0,
    }, data


def test_files_starts_whether_or_not_the_list_is_loading():
    """VolumeList starts Files on mount, loading or not, and says when it
    leaves; every ordering is in test_frontend_files_selection_569.py."""
    volume_list = (_SRC / "lib" / "components" / "panels" / "VolumeList.svelte").read_text(
        encoding="utf-8",
    )
    on_mount = volume_list.split("onMount(() => {", 1)[1].split("});", 1)[0]
    lines = [ln.strip() for ln in on_mount.strip().splitlines()]
    assert lines == ["fileBrowser.startFiles();", "return () => fileBrowser.stopFiles();"]


def test_the_browser_tab_names_the_page():
    """#578: every page was titled "Compressatorium"."""
    app = (_SRC / "App.svelte").read_text(encoding="utf-8")
    assert "<svelte:head>" in app
    assert "<title>{VIEW_TITLES[ui.activeView] ?" in app
