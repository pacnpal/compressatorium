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
    effect = effect[:effect.index("});")]
    for call in ("ui.loadVersion()", "loadTools()", "fileBrowser.refreshVolumes()"):
        assert call in effect, call
