"""Files always has something open, however the Volumes were loaded (#569).

Four Codex findings on #586 were one bug class: what Files opened depended on
which path loaded the Volume list (Files' own mount, a reconnect, Libraries'
fetch, a ``#/files/<path>`` link) and in which order. One invariant owns it
now, ``fileBrowser.ensureFilesSelection()``: while Files is mounted, with the
Volumes listed, no list fetch on its way and nothing open, it opens the
link's folder when that Volume is listed, else the first Volume. Every
ordering below goes through it. Each case is a fresh store in the Node
harness the other ``test_frontend_*`` store tests use.
"""
from __future__ import annotations

import pytest

from .test_frontend_file_browser_open_folder import _run

# A Volume list fetch the case answers by hand, and the views' steps.
_PRELUDE = """
const VOLUMES = {
  two: [{ name: 'First', path: '/first' }, { name: 'Target', path: '/target' }],
  none: [],
  target: [{ name: 'Target', path: '/target' }],
  other: [{ name: 'Other', path: '/other' }],
};
let fetches = 0;
let pending = null;
let next = VOLUMES.two;
let held = false;
api.getVolumes = () => {
  fetches += 1;
  const answer = next;
  if (!held) return Promise.resolve(answer);
  return new Promise((resolve) => { pending = () => resolve(answer); });
};
const answer = () => { const go = pending; pending = null; held = false; go?.(); };
const settle = (p) => within(p, 300);
// What the views do.
const libraries = () => fileBrowser.fetchVolumes();          // Libraries' own read
const mountFiles = () => fileBrowser.startFiles();            // VolumeList's mount
const leaveFiles = () => fileBrowser.stopFiles();             // VolumeList's unmount
const reconnect = () => fileBrowser.refreshVolumes();         // App, on a reconnect
const link = (path) => { ui.filesPath = path; return fileBrowser.openFolder(path); };
const state = () => ({
  volume: fileBrowser.selectedVolume?.path ?? null,
  path: fileBrowser.currentPath ?? null,
  fetches,
});
"""

CASES = {
    "files mounts before any load": (
        "await settle(mountFiles());",
        {"volume": "/first", "path": "/first", "fetches": 1},
    ),
    "files mounts while Libraries' load is out": (
        """held = true; const lib = libraries(); const m = mountFiles();
        answer(); await settle(Promise.all([lib, m]));""",
        {"volume": "/first", "path": "/first", "fetches": 1},
    ),
    "files mounts after Libraries loaded the list": (
        "await settle(libraries()); await settle(mountFiles());",
        {"volume": "/first", "path": "/first", "fetches": 1},
    ),
    "files mounts while a reconnect lists the volumes": (
        """await settle(libraries()); held = true; const r = reconnect();
        const m = mountFiles(); answer(); await settle(Promise.all([r, m]));""",
        {"volume": "/first", "path": "/first", "fetches": 2},
    ),
    "reconnect with the volumes unchanged keeps what is open": (
        """await settle(mountFiles());
        await settle(fileBrowser.selectVolume(fileBrowser.volumes[1]));
        await settle(reconnect());""",
        {"volume": "/target", "path": "/target", "fetches": 2},
    ),
    "reconnect with every volume lost": (
        "await settle(mountFiles()); next = VOLUMES.none; await settle(reconnect());",
        {"volume": None, "path": None, "fetches": 2},
    ),
    "every volume lost, then returned": (
        """await settle(mountFiles()); next = VOLUMES.none; await settle(reconnect());
        next = VOLUMES.two; await settle(reconnect());""",
        {"volume": "/first", "path": "/first", "fetches": 3},
    ),
    "a different set returned": (
        """await settle(mountFiles());
        await settle(fileBrowser.selectVolume(fileBrowser.volumes[1]));
        next = VOLUMES.other; await settle(reconnect());""",
        {"volume": "/other", "path": "/other", "fetches": 2},
    ),
    "a #/files link whose volume returns": (
        """await settle(link('/target/roms')); await settle(mountFiles());
        next = VOLUMES.none; await settle(reconnect());
        next = VOLUMES.target; await settle(reconnect());""",
        {"volume": "/target", "path": "/target/roms", "fetches": 3},
    ),
    "a #/files link whose volume doesn't return": (
        """await settle(link('/target/roms')); await settle(mountFiles());
        next = VOLUMES.none; await settle(reconnect());
        next = VOLUMES.other; await settle(reconnect());""",
        {"volume": "/other", "path": "/other", "fetches": 3},
    ),
    "a tab not on Files is never given a selection": (
        """await settle(mountFiles()); leaveFiles();
        next = VOLUMES.none; await settle(reconnect());
        next = VOLUMES.two; await settle(reconnect());""",
        {"volume": None, "path": None, "fetches": 3},
    ),
    "a tab that never listed the volumes stays lazy": (
        "await settle(reconnect());",
        {"volume": None, "path": None, "fetches": 0},
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_files_opens_one_volume_in_every_ordering(tmp_path, case):
    steps, expected = CASES[case]
    data = _run(tmp_path, _PRELUDE + steps + "\nprocess.stdout.write(JSON.stringify(state()));\n")
    assert data == expected, f"{case}: {data}"
