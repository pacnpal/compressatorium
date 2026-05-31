// File browser store — volumes, current path, listings, selection, sort,
// pagination, archive nav. No SSE; manual + on-demand refresh only.

import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { jobs } from './jobs.svelte.js';
import { conversion } from './conversion.svelte.js';

const DEFAULT_PAGE_SIZE = 50;
const SORT_FIELDS = new Set(['name', 'size', 'extension', 'type']);

class FileBrowserStore {
  // Volumes
  volumes = $state([]);
  volumesLoading = $state(false);
  volumesError = $state(null);
  selectedVolume = $state(null);

  // Navigation
  currentPath = $state(null);
  currentArchivePath = $state(null);

  // Entries
  entries = $state([]);
  entriesError = $state(null);
  loading = $state(false);

  // Selection
  selectedFiles = new SvelteMap();
  lastSelectedIndex = $state(-1);

  // Search
  searchMode = $state(false);
  searchResults = $state(null);
  searchQuery = $state('');

  // View state
  filter = $state(null);
  sortBy = $state('name');
  sortOrder = $state('asc');
  page = $state(1);
  pageSize = $state(DEFAULT_PAGE_SIZE);
  autoRefresh = $state(true);

  // ─── Derived ──────────────────────────────────────────────────────────
  get breadcrumbSegments() {
    if (!this.currentPath) return [];
    const segments = this.currentPath.split('/').filter(Boolean);
    let acc = '';
    return segments.map((name) => {
      acc += `/${name}`;
      return { name, path: acc };
    });
  }

  /**
   * Entries to render. When in search mode, flattens the
   * /api/files/search response (files + archive members) into a single
   * list and applies the client-side `searchQuery` filter (the backend
   * search endpoint takes no query — the query is purely client-side,
   * matching the legacy UI's behavior). Otherwise returns the current
   * directory's `entries`.
   */
  get sourceEntries() {
    if (!this.searchMode) return this.entries;
    if (!this.searchResults) return [];
    // Normalize search response rows. The /api/files/search payload's
    // `files` array is plain files from the directory tree, and the
    // `archives` array is files *inside* archives (the backend builds
    // each row at app/routes/files.py:357-379 with paths like
    // `archive.zip::dir/disc.cue` and `in_archive: true`). Both are
    // selectable file rows from the user's perspective, so stamp
    // `type: 'file'` on both arrays — marking the archive-member rows
    // as `type: 'archive'` would wrongly exclude them from Select All
    // (toggleSelectAll filters out archive containers) and break the
    // extension filter in filteredEntries (which only keeps files).
    const files = (this.searchResults.files ?? []).map((f) => ({
      ...f,
      type: f.type ?? 'file',
    }));
    const archived = (this.searchResults.archives ?? []).map((a) => ({
      ...a,
      type: a.type ?? 'file',
    }));
    const flat = [...files, ...archived];
    const q = (this.searchQuery ?? '').trim().toLowerCase();
    if (!q) return flat;
    return flat.filter((e) => {
      const name = (e.name ?? '').toLowerCase();
      const path = (e.path ?? '').toLowerCase();
      return name.includes(q) || path.includes(q);
    });
  }

  get sortedEntries() {
    const list = this.sourceEntries.slice();
    const order = this.sortOrder === 'asc' ? 1 : -1;
    list.sort((a, b) => {
      // Directories first
      if (a.type === 'directory' && b.type !== 'directory') return -1;
      if (a.type !== 'directory' && b.type === 'directory') return 1;
      let av, bv;
      switch (this.sortBy) {
        case 'size':
          av = a.size ?? 0;
          bv = b.size ?? 0;
          break;
        case 'extension':
          av = (a.extension ?? '').toLowerCase();
          bv = (b.extension ?? '').toLowerCase();
          break;
        case 'type':
          av = a.type;
          bv = b.type;
          break;
        default:
          av = a.name.toLowerCase();
          bv = b.name.toLowerCase();
      }
      if (av < bv) return -1 * order;
      if (av > bv) return 1 * order;
      return 0;
    });
    return list;
  }

  get filteredEntries() {
    if (!this.filter) return this.sortedEntries;
    const f = this.filter.toLowerCase();
    return this.sortedEntries.filter(
      (e) => e.type !== 'file' || (e.extension ?? '').toLowerCase() === f,
    );
  }

  get pageCount() {
    return Math.max(1, Math.ceil(this.filteredEntries.length / this.pageSize));
  }

  get visibleEntries() {
    const start = (this.page - 1) * this.pageSize;
    return this.filteredEntries.slice(start, start + this.pageSize);
  }

  get allVisibleSelected() {
    // Match the toggleSelectAll predicate: directories, archive
    // containers, and rows not accepted by the active conversion mode
    // are never selectable.
    const visible = this.visibleEntries.filter((e) => this._isSelectable(e));
    if (visible.length === 0) return false;
    return visible.every((e) => this.selectedFiles.has(e.path));
  }

  // ─── Volumes ──────────────────────────────────────────────────────────
  async loadVolumes() {
    this.volumesLoading = true;
    this.volumesError = null;
    try {
      const data = await api.getVolumes();
      this.volumes = Array.isArray(data) ? data : [];
      if (!this.selectedVolume && this.volumes.length > 0) {
        await this.selectVolume(this.volumes[0]);
      }
    } catch (e) {
      this.volumesError = e?.message ?? 'Failed to load volumes';
    } finally {
      this.volumesLoading = false;
    }
  }

  async selectVolume(volume) {
    if (!volume) return;
    this.exitSearch();
    this.selectedVolume = volume;
    this.currentPath = volume.path;
    this.currentArchivePath = null;
    this.clearSelection();
    this.page = 1;                       // reset paging — new listing
    // Force so navigation isn't suppressed while jobs are running.
    await this.refresh({ force: true });
  }

  // ─── Navigation ───────────────────────────────────────────────────────
  async navigate(path) {
    if (!path) return;
    // Refresh() short-circuits when searchMode is true, so we'd be
    // stuck on the old search results under the new currentPath if we
    // didn't drop search state first.
    this.exitSearch();
    this.currentPath = path;
    this.currentArchivePath = null;
    this.clearSelection();
    this.page = 1;                       // smaller folder shouldn't strand on page N>1
    await this.refresh({ force: true });
  }

  /**
   * Internal helper: fetch the archive listing into `entries`. Does
   * NOT clear selection, reset paging, or flip currentArchivePath.
   * Used by both browseArchive() (entering anew) and refresh() (when
   * already inside an archive) so refresh doesn't trash the user's
   * archive-member selections.
   */
  async _loadArchiveEntries(archivePath) {
    const data = await api.listArchive(archivePath);
    return (data.files ?? []).map((f) => {
      // Archive members in subdirectories (e.g. "games/disc.cue") expose
      // the full subpath via `internal_path`. Falling back to `name` (the
      // basename) would build `archive::disc.cue` and the backend would
      // fail to locate the member during conversion.
      const member = f.internal_path ?? f.name;
      return {
        ...f,
        type: 'file',
        path: `${archivePath}::${member}`,
      };
    });
  }

  /**
   * Enter an archive view. Used when the user clicks an archive row;
   * clears any parent-directory selection, resets paging, and only
   * commits the archive view after the listing loads successfully.
   */
  async browseArchive(archivePath) {
    if (!archivePath) return;
    try {
      const next = await this._loadArchiveEntries(archivePath);
      // Success — commit atomically: clear stale parent-dir selections,
      // reset paging, swap entries, then flip into archive mode.
      this.clearSelection();
      this.page = 1;
      this.entries = next;
      this.currentArchivePath = archivePath;
      this.entriesError = null;
    } catch (e) {
      // Leave the previous listing in place; surface the error.
      this.entriesError = e?.message ?? 'Failed to read archive';
    }
  }

  leaveArchive() {
    // Drop archive-member selections (now-invisible archive::member
    // entries) so they can't tag along with parent-folder selections
    // on the next submission.
    this.clearSelection();
    this.currentArchivePath = null;
    this.page = 1;
    return this.refresh({ force: true });
  }

  /**
   * @param {{ force?: boolean }} [opts] - `force: true` overrides the
   *   "skip while jobs active and autoRefresh off" guard, so explicit
   *   user navigation (volume switch, directory change, leave archive)
   *   always updates the listing.
   */
  async refresh({ force = false } = {}) {
    // Search mode keeps the recursive result set, not the current
    // directory. A vanilla refresh against currentPath would clobber
    // the user's search view. But modals (rename/delete) DO call
    // refresh({ force: true }) after a successful mutation expecting
    // the listing to reflect the change — silently skipping in search
    // mode leaves stale rows pointing at paths that no longer exist
    // and the user can re-act on them. When forced, re-run the
    // search instead of dropping the call entirely.
    if (this.searchMode) {
      // Re-run the recursive scan, preserving the active filter — which
      // is an empty string for a one-click "Search all" view, so we
      // re-fetch on any forced refresh rather than gating on a non-empty
      // query (the legacy "Search All" behaviour).
      if (force && this.currentPath) {
        await this._enterSearch(this.searchQuery);
      }
      return;
    }
    if (!force && jobs.hasActive && !this.autoRefresh) return;
    // When the user is inside an archive view, a vanilla refresh against
    // currentPath would replace the archive members with the parent
    // directory listing while keeping currentArchivePath set, so
    // selections/conversions would suddenly operate on the wrong rows.
    // Re-fetch the archive contents via the internal loader (which does
    // NOT clear selection, so the user's archive-member picks survive
    // manual/auto refresh).
    if (this.currentArchivePath) {
      this.loading = true;
      this.entriesError = null;
      try {
        this.entries = await this._loadArchiveEntries(this.currentArchivePath);
      } catch (e) {
        this.entriesError = e?.message ?? 'Failed to read archive';
      } finally {
        this.loading = false;
        this._clampPage();
      }
      return;
    }
    if (!this.currentPath) {
      this.entries = [];
      this._clampPage();
      return;
    }
    this.loading = true;
    this.entriesError = null;
    try {
      const data = await api.listFiles(this.currentPath, true);
      this.entries = data?.entries ?? [];
    } catch (e) {
      this.entriesError = e?.message ?? 'Failed to load files';
      this.entries = [];
    } finally {
      this.loading = false;
      this._clampPage();
    }
  }

  /**
   * Clamp `page` into the new pageCount after entries shrink — a
   * background refresh that drops the visible row count could
   * otherwise strand the user on an empty page they can't see is
   * empty. Never moves the page if the current value is still valid.
   */
  _clampPage() {
    const max = this.pageCount;
    if (this.page > max) this.page = max;
  }

  /**
   * Run the recursive `/api/files/search` scan (subdirectories + inside
   * archives) and flip into search mode on success, applying `query` as
   * the client-side filter. An empty `query` is valid — it surfaces every
   * convertible file in the tree (the legacy "Search All" view); see
   * sourceEntries, which returns the full flattened set when searchQuery
   * is blank.
   */
  async _enterSearch(query) {
    if (!this.currentPath) return;
    try {
      const data = await api.searchFiles(this.currentPath, true, true);
      // Flip into search mode only on success. If the request fails,
      // leaving searchMode false keeps the user looking at the current
      // directory's entries instead of an empty search view.
      this.searchResults = data;
      this.searchQuery = query;
      this.searchMode = true;
      this.page = 1;
      // Drop the current directory's selection set — those paths
      // aren't necessarily in the recursive search results, so
      // leaving them selected would let the bulk-action bar / Convert
      // panel act on rows the user can't see. Symmetric with the
      // cleanup the X button does on exitSearch.
      this.clearSelection();
    } catch (e) {
      this.entriesError = e?.message ?? 'Search failed';
      // Stay out of search mode so the directory listing remains visible.
    }
  }

  /**
   * Text-box search: filters the recursive result set by `query`. An
   * empty query exits search and returns to the plain directory listing
   * (the X / clear-search contract).
   */
  async search(query) {
    // /api/files/search has no query parameter — it returns every
    // convertible file under the current path. The `query` is used
    // client-side to filter the returned set (see sourceEntries).
    if (!query) {
      this.exitSearch();
      return;
    }
    await this._enterSearch(query);
  }

  /**
   * One-click "Search all" — recursively list every convertible file
   * under the current path, including files inside archives, with no
   * text filter. Restores the legacy "🔍 Search All" action.
   */
  async searchAll() {
    await this._enterSearch('');
  }

  exitSearch() {
    this.searchMode = false;
    this.searchResults = null;
  }

  // ─── Selection ────────────────────────────────────────────────────────
  /**
   * Predicate for whether a row is checkable at all — directories and
   * archive containers are out (the backend rejects archive paths as
   * conversion inputs; directories aren't files). Conversion-mode
   * eligibility is NOT enforced here because the same selection set
   * also drives bulk Verify and bulk Delete; gating it at toggle time
   * would hide checkboxes on rows the user might want to verify or
   * delete (e.g. a verified `.chd` while in CHDMAN createcd). The
   * convert path filters its inputs separately via
   * `conversion.allowsInput()`.
   */
  _isSelectable(entry) {
    if (!entry) return false;
    return entry.type !== 'directory' && entry.type !== 'archive';
  }

  /**
   * The subset of the current selection that the active conversion
   * mode would accept as input. ConvertPanel filters through this
   * before calling `conversion.submit()` so the worker never sees
   * paths it would reject.
   */
  get convertibleSelection() {
    const out = [];
    for (const entry of this.selectedFiles.values()) {
      if (conversion.allowsInput(entry?.path)) out.push(entry);
    }
    return out;
  }

  toggleSelect(entry, { shift = false } = {}) {
    if (!this._isSelectable(entry)) return;
    // Range-selectable rows are non-directory, non-archive AND match
    // the active mode's input extensions. Same predicate as
    // toggleSelectAll so shift-click never picks up rows the worker
    // would reject.
    const visible = this.visibleEntries.filter((e) => this._isSelectable(e));
    const idx = visible.findIndex((e) => e.path === entry.path);

    if (shift && this.lastSelectedIndex >= 0 && idx >= 0) {
      const [start, end] = idx > this.lastSelectedIndex
        ? [this.lastSelectedIndex, idx]
        : [idx, this.lastSelectedIndex];
      for (let i = start; i <= end; i += 1) {
        this.selectedFiles.set(visible[i].path, visible[i]);
      }
    } else if (this.selectedFiles.has(entry.path)) {
      this.selectedFiles.delete(entry.path);
    } else {
      this.selectedFiles.set(entry.path, entry);
    }
    this.lastSelectedIndex = idx;
  }

  toggleSelectAll() {
    // Archive containers can't be submitted as conversion inputs — the
    // backend expects either a regular file path or `archive::member`
    // form. Same gate for mode-incompatible inputs (e.g. .rvz under
    // CHDMAN createcd). Filter to rows the active conversion mode
    // actually accepts.
    const visible = this.visibleEntries.filter((e) => this._isSelectable(e));
    if (this.allVisibleSelected) {
      for (const e of visible) this.selectedFiles.delete(e.path);
    } else {
      for (const e of visible) this.selectedFiles.set(e.path, e);
    }
  }

  clearSelection() {
    this.selectedFiles.clear();
    this.lastSelectedIndex = -1;
  }

  // ─── Sorting / paging / filter ────────────────────────────────────────
  setSort(field) {
    if (!SORT_FIELDS.has(field)) return;
    if (this.sortBy === field) {
      this.sortOrder = this.sortOrder === 'asc' ? 'desc' : 'asc';
    } else {
      this.sortBy = field;
      this.sortOrder = 'asc';
    }
    this.page = 1;
  }

  setFilter(ext) {
    this.filter = ext ? ext.toLowerCase() : null;
    this.page = 1;
  }

  setPage(p) {
    this.page = Math.max(1, Math.min(p, this.pageCount));
  }
}

export const fileBrowser = new FileBrowserStore();
