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

    assert "ui.bulkDeleteEntries = fileBrowser.filesToDelete();" in file_list
    store = (_LIB / "stores" / "fileBrowser.svelte.js").read_text(encoding="utf-8")
    body = store.split("  filesToDelete() {", 1)[1].split("\n  }\n", 1)[0]
    assert "this.selectedPlaylists.keys()" in body


def test_the_folder_search_filters_games_by_name(tmp_path):
    data = _run(tmp_path, _LISTING + """
await fileBrowser.search('saga');
process.stdout.write(JSON.stringify({ shown: fileBrowser.visibleEntries.map((e) => e.name) }));
""")

    assert data["shown"] == ["Saga.m3u"], data


def test_an_everywhere_search_answering_after_this_folder_is_dropped(tmp_path):
    """Codex 4654246817: back on This folder, a late Everywhere answer must not
    put the view into search mode over the folder filter."""
    data = _run(tmp_path, _LISTING + """
fileBrowser.currentPath = '/v';
let answer;
api.searchFiles = () => new Promise((resolve) => { answer = resolve; });
const everywhere = fileBrowser.setSearchScope('everywhere', 'saga');
await fileBrowser.setSearchScope('folder', 'saga');
answer({ files: [{ name: 'Other.iso', path: '/v/sub/Other.iso' }], archives: [] });
await everywhere;
process.stdout.write(JSON.stringify({
  searchMode: fileBrowser.searchMode,
  searching: fileBrowser.searching,
  scope: fileBrowser.searchScope,
  shown: fileBrowser.visibleEntries.map((e) => e.name),
}));
""")

    assert data == {
        "searchMode": False, "searching": False, "scope": "folder", "shown": ["Saga.m3u"],
    }, data


def test_a_finished_convert_unticks_only_what_it_sent(tmp_path):
    """CodeRabbit 4654265194: a game ticked while the queue request was out
    stays ticked; a playlist whose discs went with the run is unticked."""
    data = _run(tmp_path, _LISTING + """
const m = fileBrowser.rows.find((r) => r.path === '/v/Saga.m3u');
fileBrowser.toggleSelect(m);
fileBrowser.toggleSelect(loose);
fileBrowser.deselect(['/v/Saga (Disc 1).chd', '/v/Saga (Disc 2).chd']);
process.stdout.write(JSON.stringify({
  files: [...fileBrowser.selectedFiles.keys()],
  playlists: [...fileBrowser.selectedPlaylists.keys()],
}));
""")

    assert data == {"files": ["/v/Loose.iso"], "playlists": []}, data


def test_a_refresh_during_a_search_keeps_the_newest_words(tmp_path):
    """CodeRabbit 4654557567: Refresh while `beta` is still searching
    searches `beta` again, not the last finished `alpha`."""
    data = _run(tmp_path, _LISTING + """
fileBrowser.currentPath = '/v';
fileBrowser.searchScope = 'everywhere';
api.searchFiles = async () => ({ files: [], archives: [] });
await fileBrowser.search('alpha');
let answer;
api.searchFiles = () => new Promise((resolve) => { answer = resolve; });
const beta = fileBrowser.search('beta');
api.searchFiles = async () => ({ files: [], archives: [] });
await fileBrowser.refresh({ force: true });
answer({ files: [], archives: [] });
await beta;
process.stdout.write(JSON.stringify({ query: fileBrowser.searchQuery }));
""")

    assert data == {"query": "beta"}, data


def test_delete_never_takes_part_of_a_split_disc(tmp_path):
    """Codex 4654710759: a playlist whose disc is a folded split set
    (`Game.iso.0`, `split_parts: 2`) is deleted without that disc; a partial
    set is never sent."""
    data = _run(tmp_path, """
const m3u = { name: 'Game.m3u', path: '/v/Game.m3u', type: 'file', size: 20 };
const split = { name: 'Game.iso', path: '/v/Game.iso.0', type: 'file', size: 9,
  split_parts: 2, disc_of: '/v/Game.m3u' };
const cue = { name: 'B.cue', path: '/v/B.cue', type: 'file', size: 1 };
const bin = { name: 'B.bin', path: '/v/B.bin', type: 'file', size: 1, companion_of: '/v/B.cue' };
fileBrowser.entries = [m3u, split, cue, bin];
for (const row of fileBrowser.rows) fileBrowser.toggleSelect(row);
process.stdout.write(JSON.stringify(fileBrowser.filesToDelete().map((e) => e.path).sort()));
""")

    assert data == ["/v/B.bin", "/v/B.cue", "/v/Game.m3u"], data


def test_delete_reads_each_ticked_game_as_the_listing_has_it_now(tmp_path):
    """CodeRabbit 4654883878: a refresh after ticking changes a game's files;
    Delete takes the files the row has now, and drops a row that's gone."""
    data = _run(tmp_path, """
const cue = { name: 'A.cue', path: '/v/A.cue', type: 'file', size: 1 };
const oldBin = { name: 'Old.bin', path: '/v/Old.bin', type: 'file', size: 1, companion_of: '/v/A.cue' };
const gone = { name: 'Gone.iso', path: '/v/Gone.iso', type: 'file', size: 1 };
fileBrowser.entries = [cue, oldBin, gone];
for (const row of fileBrowser.rows) fileBrowser.toggleSelect(row);
const newBin = { name: 'New.bin', path: '/v/New.bin', type: 'file', size: 1, companion_of: '/v/A.cue' };
fileBrowser.entries = [cue, oldBin && { ...oldBin, companion_of: null }, newBin];
process.stdout.write(JSON.stringify(fileBrowser.filesToDelete().map((e) => e.path).sort()));
""")

    assert data == ["/v/A.cue", "/v/New.bin"], data


def test_an_archive_opens_without_the_folders_name_filter(tmp_path):
    """Codex 4655058125: the folder's name filter would hide the archive's
    members; it is dropped on entering the archive and on leaving it."""
    data = _run(tmp_path, _LISTING + """
fileBrowser.currentPath = '/v';
await fileBrowser.search('saga');
api.listArchive = async () => ({ files: [{ name: 'Game.gba', internal_path: 'Game.gba' }] });
await fileBrowser.browseArchive('/v/a.zip');
const inside = { query: fileBrowser.folderQuery, shown: fileBrowser.visibleEntries.map((e) => e.name) };
await fileBrowser.search('game');
api.listFiles = async () => ({ entries: [] });
await fileBrowser.leaveArchive();
process.stdout.write(JSON.stringify({ inside, after: fileBrowser.folderQuery }));
""")

    assert data == {"inside": {"query": "", "shown": ["Game.gba"]}, "after": ""}, data


def test_leaving_everywhere_drops_what_this_folder_does_not_list(tmp_path):
    """Codex 4655165264: a nested game ticked in Everywhere results is
    unticked on the way back to This folder; one the folder lists stays."""
    data = _run(tmp_path, _LISTING + """
fileBrowser.currentPath = '/v';
api.searchFiles = async () => ({
  files: [
    { name: 'Loose.iso', path: '/v/Loose.iso' },
    { name: 'Deep.iso', path: '/v/sub/Deep.iso' },
  ],
  archives: [],
});
await fileBrowser.setSearchScope('everywhere', '');
for (const row of fileBrowser.rows) fileBrowser.toggleSelect(row);
const during = [...fileBrowser.selectedFiles.keys()].sort();
await fileBrowser.setSearchScope('folder', '');
process.stdout.write(JSON.stringify({
  during, after: [...fileBrowser.selectedFiles.keys()],
  convertible: fileBrowser.convertibleSelection.map((e) => e.path),
}));
""")

    assert data["during"] == ["/v/Loose.iso", "/v/sub/Deep.iso"], data
    assert data["after"] == ["/v/Loose.iso"], data
    assert data["convertible"] == ["/v/Loose.iso"], data


def test_the_store_names_the_query_it_applies(tmp_path):
    """Codex 4655353322: the box shows what the store filters by, in
    either scope, so a remounted FileList starts from it."""
    data = _run(tmp_path, _LISTING + """
fileBrowser.currentPath = '/v';
api.searchFiles = async () => ({ files: [], archives: [] });
await fileBrowser.setSearchScope('folder', 'saga');
const folder = fileBrowser.activeQuery;
await fileBrowser.setSearchScope('everywhere', 'mario');
const everywhere = fileBrowser.activeQuery;
fileBrowser.exitSearch();
process.stdout.write(JSON.stringify({ folder, everywhere, none: fileBrowser.activeQuery }));
""")

    assert data == {"folder": "saga", "everywhere": "mario", "none": ""}, data


def test_a_remounted_file_list_shows_the_query_still_applied():
    file_list = (_LIB / "components" / "panels" / "FileList.svelte").read_text(encoding="utf-8")

    assert "let searchInput = $state(fileBrowser.activeQuery);" in file_list


def test_a_folded_game_shows_when_any_of_its_files_matches(tmp_path):
    """Codex 4655464285: a playlist whose discs are CHDs stays under the
    CHD filter; a name query keeps a game when a disc or track matches."""
    data = _run(tmp_path, _LISTING + """
const shown = () => fileBrowser.filteredEntries.map((e) => e.path);
fileBrowser.filter = '.chd';
const chd = shown();
fileBrowser.filter = '.bin';
const bin = shown();
fileBrowser.filter = '';
fileBrowser.folderQuery = 'disc 2';
const named = shown();
process.stdout.write(JSON.stringify({ chd, bin, named }));
""")

    assert data["chd"] == ["/v/Saga.m3u"], data
    assert data["bin"] == ["/v/Game.cue"], data
    assert data["named"] == ["/v/Saga.m3u"], data


def test_everywhere_keeps_a_game_whole_when_one_file_matches(tmp_path):
    data = _run(tmp_path, _LISTING + """
fileBrowser.currentPath = '/v';
api.searchFiles = async () => ({ files: [m3u, d1, d2, loose], archives: [] });
await fileBrowser.setSearchScope('everywhere', 'disc 2');
const byDisc = fileBrowser.filteredEntries.map((e) => [e.path, (e.parts ?? []).length]);
await fileBrowser.setSearchScope('everywhere', 'saga.m3u');
const byName = fileBrowser.filteredEntries.map((e) => [e.path, (e.parts ?? []).length]);
process.stdout.write(JSON.stringify({ byDisc, byName }));
""")

    assert data["byDisc"] == [["/v/Saga.m3u", 2]], data
    assert data["byName"] == [["/v/Saga.m3u", 2]], data


def test_every_folded_file_keeps_its_own_actions():
    """Codex 4655722988: a disc, a track or a part shown under its game has
    the same actions menu (Info, Verify, Rename, Delete...) it had as a row,
    nested tracks included, and a disc can still be ticked on its own."""
    row = (_LIB / "components" / "panels" / "FileRow.svelte").read_text(encoding="utf-8")
    children = row.split("{#if expanded}", 1)[1].split("{/if}\n\n<style>", 1)[0]

    assert "{#each childRows as child (child.part.path)}" in children
    assert "<RowActionsMenu entry={child.part} />" in children
    assert "fileBrowser.isSelectable(child.part)" in children
    flatten = row.split("function flattenParts(", 1)[1].split("\n  }\n", 1)[0]
    assert "flattenParts(item.parts" in flatten


def test_a_disc_is_ticked_on_its_own_and_unticking_one_drops_its_playlist(tmp_path):
    data = _run(tmp_path, _LISTING + """
const saga = fileBrowser.rows.find((r) => r.path === '/v/Saga.m3u');
const disc2 = saga.parts.find((d) => d.path === '/v/Saga (Disc 2).chd');
const alone = fileBrowser.isSelectable(disc2);
fileBrowser.toggleSelect(disc2);
const one = [...fileBrowser.selectedFiles.keys()];
fileBrowser.toggleSelect(disc2);
fileBrowser.toggleSelect(saga);
fileBrowser.toggleSelect(disc2);
process.stdout.write(JSON.stringify({
  alone, one,
  after: [...fileBrowser.selectedFiles.keys()],
  playlists: [...fileBrowser.selectedPlaylists.keys()],
  sagaTicked: fileBrowser.isRowSelected(saga),
}));
""")

    assert data["alone"] is True, data
    assert data["one"] == ["/v/Saga (Disc 2).chd"], data
    assert data["after"] == ["/v/Saga (Disc 1).chd"], data
    assert data["playlists"] == [], data
    assert data["sagaTicked"] is False, data
