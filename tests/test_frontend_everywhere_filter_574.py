"""Search Everywhere filters as you type once the tree is listed (#574).

Everywhere listed the whole tree under the folder, then ignored typing until
Enter, and after an Enter left the list filtered by those words while the
box showed new ones. ``/api/files/search`` takes no query (the filter is
applied in the browser), so the words now filter the listed tree as they are
typed, with no new scan; Enter still lists the tree again.
"""
from __future__ import annotations

from pathlib import Path

from .test_frontend_file_browser_open_folder import _run

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"

_TREE = """
fileBrowser.currentPath = '/v';
let scans = 0;
api.searchFiles = async () => {
  scans += 1;
  return {
    files: [
      { name: 'ico.iso', path: '/v/ps2/ico.iso', type: 'file', size: 1 },
      { name: 'okami.iso', path: '/v/ps2/okami.iso', type: 'file', size: 1 },
      { name: 'castle.sfc', path: '/v/snes/castle.sfc', type: 'file', size: 1 },
    ],
    archives: [],
  };
};
const shown = () => fileBrowser.visibleEntries.map((e) => e.name);
"""


def test_typing_filters_the_listed_tree_without_a_new_scan(tmp_path):
    data = _run(tmp_path, _TREE + """
await fileBrowser.setSearchScope('everywhere', '');
const all = shown();
fileBrowser.filterSearch('okami');
const okami = shown();
fileBrowser.filterSearch('ico');
const ico = shown();
fileBrowser.filterSearch('');
const cleared = shown();
process.stdout.write(JSON.stringify({ all, okami, ico, cleared, scans,
  searchMode: fileBrowser.searchMode }));
""")

    assert data == {
        "all": ["castle.sfc", "ico.iso", "okami.iso"],
        "okami": ["okami.iso"],
        "ico": ["ico.iso"],
        "cleared": ["castle.sfc", "ico.iso", "okami.iso"],
        "scans": 1,
        "searchMode": True,
    }, data


def test_words_typed_while_the_tree_is_being_listed_are_the_ones_applied(tmp_path):
    data = _run(tmp_path, _TREE + """
let answer;
const real = api.searchFiles;
api.searchFiles = () => new Promise((resolve) => { answer = () => real().then(resolve); });
const listing = fileBrowser.setSearchScope('everywhere', '');
fileBrowser.filterSearch('cas');
answer();
await listing;
process.stdout.write(JSON.stringify({ shown: shown(), query: fileBrowser.searchQuery }));
""")

    assert data == {"shown": ["castle.sfc"], "query": "cas"}, data


def test_this_folder_is_untouched_by_the_everywhere_filter(tmp_path):
    data = _run(tmp_path, _TREE + """
fileBrowser.entries = [{ name: 'Loose.iso', path: '/v/Loose.iso', type: 'file', size: 1 }];
fileBrowser.filterSearch('zzz');
process.stdout.write(JSON.stringify({ shown: shown(), scans }));
""")

    assert data == {"shown": ["Loose.iso"], "scans": 0}, data


def test_the_box_filters_everywhere_as_you_type():
    file_list = (_LIB / "components" / "panels" / "FileList.svelte").read_text(encoding="utf-8")
    typed = file_list.split("function typed() {", 1)[1].split("\n  }\n", 1)[0]
    assert "fileBrowser.filterSearch(" in typed
