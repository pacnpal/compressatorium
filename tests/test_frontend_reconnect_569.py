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


def test_a_volume_that_is_gone_gives_way_to_the_first_one_left(tmp_path):
    data = _run_browser(tmp_path, """
await fileBrowser.fetchVolumes();
await within(fileBrowser.selectVolume(fileBrowser.volumes[1]), 300);
api.getVolumes = async () => [
  { name: 'New', path: '/new' },
  { name: 'Other', path: '/other' },
];
await within(fileBrowser.refreshVolumes(), 300);
process.stdout.write(JSON.stringify({
  volumes: fileBrowser.volumes.map((v) => v.path),
  volume: fileBrowser.selectedVolume?.path ?? null,
  path: fileBrowser.currentPath,
  listed: __listed,
}));
""")
    assert data == {
        "volumes": ["/new", "/other"], "volume": "/new", "path": "/new",
        "listed": ["/target", "/new"],
    }, data


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


def test_a_tab_that_never_listed_volumes_leaves_the_start_to_files(tmp_path):
    """Codex on #586: a tab opened on Settings that reconnects before Files
    was visited filled the list with nothing picked, so Files (which loads
    and picks only while the list is empty) stayed blank."""
    data = _run_browser(tmp_path, """
let calls = 0;
const real = api.getVolumes;
api.getVolumes = async () => { calls += 1; return real(); };
await within(fileBrowser.refreshVolumes(), 300);
const after = { calls, volumes: fileBrowser.volumes.length };
await within(fileBrowser.loadVolumes(), 300);
process.stdout.write(JSON.stringify({ ...after,
  volume: fileBrowser.selectedVolume?.path ?? null }));
""")
    assert data == {"calls": 0, "volumes": 0, "volume": "/first"}, data


def test_volumes_that_come_back_after_none_are_listed_and_one_is_picked(tmp_path):
    """Codex on #586: a reconnect left no Volumes, so the list was empty with
    nothing picked; the next reconnect, with Volumes back, skipped the fetch
    as if the tab had never listed them, and an open Files stayed on "No
    volumes". Having listed them once is what counts, not the list's size."""
    data = _run_browser(tmp_path, """
await fileBrowser.fetchVolumes();
await within(fileBrowser.selectVolume(fileBrowser.volumes[1]), 300);
const back = api.getVolumes;
api.getVolumes = async () => [];
await within(fileBrowser.refreshVolumes(), 300);
const gone = fileBrowser.volumes.length;
api.getVolumes = back;
await within(fileBrowser.refreshVolumes(), 300);
process.stdout.write(JSON.stringify({ gone,
  volumes: fileBrowser.volumes.map((v) => v.path),
  volume: fileBrowser.selectedVolume?.path ?? null }));
""")
    assert data == {"gone": 0, "volumes": ["/first", "/target"], "volume": "/first"}, data


def test_a_folder_link_reopens_when_its_volume_comes_back(tmp_path):
    """Codex on #586: Files opened through `#/files/<path>`, every Volume went,
    then came back. `ui.filesPath` still held the link, so no Volume was
    picked, and FilesView reopens the folder only when the route changes:
    Files stayed blank. The link's folder opens again when its Volume is
    back; a link whose Volume didn't return gives way to the first one."""
    data = _run_browser(tmp_path, """
const back = api.getVolumes;
const run = async (link, returning) => {
  __listed.length = 0;
  api.getVolumes = back;
  ui.filesPath = link;
  await within(fileBrowser.openFolder(link), 300);
  api.getVolumes = async () => [];
  await within(fileBrowser.refreshVolumes(), 300);
  api.getVolumes = async () => returning;
  await within(fileBrowser.refreshVolumes(), 300);
  return { volume: fileBrowser.selectedVolume?.path ?? null, path: fileBrowser.currentPath };
};
const kept = await run('/target/roms', [{ name: 'Target', path: '/target' }]);
const moved = await run('/target/roms', [{ name: 'Other', path: '/other' }]);
process.stdout.write(JSON.stringify({ kept, moved }));
""")
    assert data == {
        "kept": {"volume": "/target", "path": "/target/roms"},
        "moved": {"volume": "/other", "path": "/other"},
    }, data


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


def test_the_browser_tab_names_the_page():
    """#578: every page was titled "Compressatorium"."""
    app = (_SRC / "App.svelte").read_text(encoding="utf-8")
    assert "<svelte:head>" in app
    assert "<title>{VIEW_TITLES[ui.activeView] ?" in app
