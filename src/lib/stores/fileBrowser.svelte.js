// File browser store — volumes, current path, listings, selection, sort,
// pagination, archive nav. No SSE; manual + on-demand refresh only.

import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { jobs } from './jobs.svelte.js';

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

  get sortedEntries() {
    const list = this.entries.slice();
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
    const visible = this.visibleEntries.filter((e) => e.type !== 'directory');
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
    this.selectedVolume = volume;
    this.currentPath = volume.path;
    this.currentArchivePath = null;
    this.clearSelection();
    // Force so navigation isn't suppressed while jobs are running.
    await this.refresh({ force: true });
  }

  // ─── Navigation ───────────────────────────────────────────────────────
  async navigate(path) {
    if (!path) return;
    this.currentPath = path;
    this.currentArchivePath = null;
    this.clearSelection();
    await this.refresh({ force: true });
  }

  async browseArchive(archivePath) {
    if (!archivePath) return;
    this.currentArchivePath = archivePath;
    try {
      const data = await api.listArchive(archivePath);
      this.entries = (data.files ?? []).map((f) => {
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
    } catch (e) {
      this.entriesError = e?.message ?? 'Failed to read archive';
    }
  }

  leaveArchive() {
    this.currentArchivePath = null;
    return this.refresh({ force: true });
  }

  /**
   * @param {{ force?: boolean }} [opts] - `force: true` overrides the
   *   "skip while jobs active and autoRefresh off" guard, so explicit
   *   user navigation (volume switch, directory change, leave archive)
   *   always updates the listing.
   */
  async refresh({ force = false } = {}) {
    if (this.searchMode) return;
    if (!force && jobs.hasActive && !this.autoRefresh) return;
    // When the user is inside an archive view, a vanilla refresh against
    // currentPath would replace the archive members with the parent
    // directory listing while keeping currentArchivePath set, so
    // selections/conversions would suddenly operate on the wrong rows.
    // Re-fetch the archive contents instead.
    if (this.currentArchivePath) {
      await this.browseArchive(this.currentArchivePath);
      return;
    }
    if (!this.currentPath) {
      this.entries = [];
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
    }
  }

  async search(query) {
    // /api/files/search has no query parameter — it returns every
    // convertible file under the current path. The `query` is used
    // client-side to filter the returned set (see filteredSearchResults).
    if (!query) {
      this.searchMode = false;
      this.searchResults = null;
      this.searchQuery = '';
      return;
    }
    if (!this.currentPath) return;
    this.searchMode = true;
    this.searchQuery = query;
    try {
      this.searchResults = await api.searchFiles(this.currentPath, true, true);
    } catch (e) {
      this.entriesError = e?.message ?? 'Search failed';
      this.searchResults = null;
    }
  }

  exitSearch() {
    this.searchMode = false;
    this.searchResults = null;
  }

  // ─── Selection ────────────────────────────────────────────────────────
  toggleSelect(entry, { shift = false } = {}) {
    if (!entry || entry.type === 'directory') return;
    const visible = this.visibleEntries.filter((e) => e.type !== 'directory');
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
    const visible = this.visibleEntries.filter((e) => e.type !== 'directory');
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
