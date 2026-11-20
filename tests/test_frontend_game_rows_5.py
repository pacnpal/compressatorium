"""A multi-file game is one row in Files (restructure 2/6, plan item 5).

``fileBrowser.rows`` folds the rows the listing marks (``companion_of``: a
sheet's tracks, a RAR set's later volumes; ``disc_of``: a playlist's discs)
under their head before sort and paging. Ticking a playlist ticks its discs,
and Delete also takes the ``.m3u``. The store is evaluated with Node like the
other ``test_frontend_*`` store tests; skipped without Node.
"""
from __future__ import annotations

from pathlib import Path

from .test_frontend_file_browser_open_folder import _run

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"

_LISTING = """
const cue = { name: 'Game.cue', path: '/v/Game.cue', type: 'file', size: 100 };
const bin1 = { name: 'Game (Track 1).bin', path: '/v/Game (Track 1).bin', type: 'file',
  size: 1000, companion_of: '/v/Game.cue' };
const bin2 = { name: 'Game (Track 2).bin', path: '/v/Game (Track 2).bin', type: 'file',
  size: 500, companion_of: '/v/Game.cue' };
const m3u = { name: 'Saga.m3u', path: '/v/Saga.m3u', type: 'file', size: 40 };
const d1 = { name: 'Saga (Disc 1).chd', path: '/v/Saga (Disc 1).chd', type: 'file',
  size: 2000, disc_of: '/v/Saga.m3u' };
const d2 = { name: 'Saga (Disc 2).chd', path: '/v/Saga (Disc 2).chd', type: 'file',
  size: 3000, disc_of: '/v/Saga.m3u' };
const loose = { name: 'Loose.iso', path: '/v/Loose.iso', type: 'file', size: 7 };
fileBrowser.entries = [bin1, cue, d2, bin2, m3u, d1, loose];
"""


def test_a_cue_and_its_bins_fold_to_one_row_with_the_summed_size(tmp_path):
    data = _run(tmp_path, _LISTING + """
const rows = fileBrowser.rows;
const head = rows.find((r) => r.path === '/v/Game.cue');
process.stdout.write(JSON.stringify({
  rows: rows.map((r) => r.name).sort(),
  size: head.size,
  parts: head.parts.map((p) => p.name),
  kind: head.part_kind,
  raw: fileBrowser.entries.length,
}));
""")

    assert data["rows"] == ["Game.cue", "Loose.iso", "Saga.m3u"], data
    assert data["size"] == 1600, data
    assert data["parts"] == ["Game (Track 1).bin", "Game (Track 2).bin"], data
    assert data["kind"] == "files", data
    assert data["raw"] == 7, data


def test_a_playlist_folds_its_discs(tmp_path):
    data = _run(tmp_path, _LISTING + """
const m = fileBrowser.rows.find((r) => r.path === '/v/Saga.m3u');
process.stdout.write(JSON.stringify({
  kind: m.part_kind, discs: m.parts.map((p) => p.name).sort(), size: m.size,
}));
""")

    assert data == {
        "kind": "discs", "discs": ["Saga (Disc 1).chd", "Saga (Disc 2).chd"], "size": 5040,
    }, data


def test_paging_counts_games_not_files(tmp_path):
    data = _run(tmp_path, _LISTING + """
fileBrowser.pageSize = 2;
process.stdout.write(JSON.stringify({
  pages: fileBrowser.pageCount,
  first: fileBrowser.visibleEntries.map((e) => e.name),
}));
""")

    assert data["pages"] == 2, data
    assert data["first"] == ["Game.cue", "Loose.iso"], data


def test_ticking_a_playlist_selects_its_discs(tmp_path):
    data = _run(tmp_path, _LISTING + """
const m = fileBrowser.rows.find((r) => r.path === '/v/Saga.m3u');
fileBrowser.toggleSelect(m);
const on = {
  selected: [...fileBrowser.selectedFiles.keys()].sort(),
  playlists: [...fileBrowser.selectedPlaylists.keys()],
  ticked: fileBrowser.isRowSelected(m),
};
fileBrowser.toggleSelect(m);
process.stdout.write(JSON.stringify({
  on,
  off: [...fileBrowser.selectedFiles.keys()],
  offPlaylists: [...fileBrowser.selectedPlaylists.keys()],
}));
""")

    assert data["on"] == {
        "selected": ["/v/Saga (Disc 1).chd", "/v/Saga (Disc 2).chd"],
        "playlists": ["/v/Saga.m3u"],
        "ticked": True,
    }, data
    assert data["off"] == [] and data["offPlaylists"] == [], data


def test_a_nested_part_is_never_ticked_alone(tmp_path):
    data = _run(tmp_path, _LISTING + """
fileBrowser.toggleSelect(d1);
fileBrowser.toggleSelect(bin1);
fileBrowser.selectAllFiltered();
process.stdout.write(JSON.stringify({ selected: [...fileBrowser.selectedFiles.keys()].sort() }));
""")

    assert data["selected"] == [
        "/v/Game.cue", "/v/Loose.iso", "/v/Saga (Disc 1).chd", "/v/Saga (Disc 2).chd",
    ], data


def test_delete_takes_the_playlist_and_every_file_of_each_game():
    file_list = (_LIB / "components" / "panels" / "FileList.svelte").read_text(encoding="utf-8")

    assert "fileBrowser.selectedPlaylists" in file_list
    assert "gameFiles(" in file_list


def test_the_folder_search_filters_games_by_name(tmp_path):
    data = _run(tmp_path, _LISTING + """
await fileBrowser.search('saga');
process.stdout.write(JSON.stringify({ shown: fileBrowser.visibleEntries.map((e) => e.name) }));
""")

    assert data["shown"] == ["Saga.m3u"], data
