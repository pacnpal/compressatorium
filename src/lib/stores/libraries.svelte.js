// Libraries (#303): RomM, and every folder registered as one.
//
// The Rules view edits one Library's Rules at a time through a RulesSource
// (`$lib/rules/rulesSource.svelte.js`): the RomM store for RomM, and a
// FolderRulesSource below for a folder Library. The Rule bookkeeping (load,
// edit, save with the save-race guard, preview, run) lives once, in
// RulesSource; this file adds only what a folder Library has of its own.
//
// What a Library allows comes from the backend: a folder Library's options
// list only the `skip` duplicate policy, so the editor never offers the others.

import { api } from '$lib/api/endpoints.js';
import { RulesSource } from '$lib/rules/rulesSource.svelte.js';
import { romm } from '$lib/stores/romm.svelte.js';
import { ui } from '$lib/stores/ui.svelte.js';

export const ROMM_LIBRARY_ID = 'romm';

/** One folder Library: its Platforms, counts and schedule switch, plus its Rules. */
export class FolderRulesSource extends RulesSource {
  emptyTitle = 'No Platforms found';
  emptyHint = 'No Image in this folder has a Platform yet. Images in a folder named after '
    + 'a Platform (ps2, gc, psx, ...) or with a readable header are picked up.';

  library = $state(null);
  platforms = $state([]);
  counts = $state({ unsorted: 0, conflicted: 0, unreadable: 0 });
  automatableModes = $state(null);
  platformsLoading = $state(false);
  platformsError = $state(null);

  constructor(library) {
    const id = () => library?.id;
    super({
      getRules: () => api.getLibraryRules(id()),
      saveRules: (rules) => api.saveLibraryRules(id(), rules),
      forgetConverted: (platformIds) => api.forgetLibraryConverted(id(), platformIds),
      preview: (body) => api.previewLibrary(id(), body),
      run: (body) => api.runLibrary(id(), body),
      loadError: 'Failed to load the Rules',
    });
    this.library = library;
  }

  get id() {
    return this.library?.id;
  }

  get scheduleEnabled() {
    return this.library?.schedule_enabled === true;
  }

  async setScheduleEnabled(enabled) {
    this.library = await api.updateLibrary(this.id, { schedule_enabled: enabled });
    libraries.adopt(this.library);
  }

  imageCount(platform) {
    return platform?.images ?? 0;
  }

  platformName(platformId) {
    return this.platforms.find((p) => p.id === String(platformId))?.name ?? String(platformId);
  }

  async loadPlatforms() {
    this.platformsLoading = true;
    this.platformsError = null;
    try {
      const data = await api.getLibraryPlatforms(this.id);
      this.platforms = Array.isArray(data?.platforms) ? data.platforms : [];
      this.counts = {
        unsorted: data?.unsorted ?? 0,
        conflicted: data?.conflicted ?? 0,
        unreadable: data?.unreadable ?? 0,
      };
      this.automatableModes = Array.isArray(data?.automatable_modes)
        ? data.automatable_modes
        : null;
    } catch (e) {
      this.platformsError = e?.message ?? 'Failed to list the Library';
      this.platforms = [];
    } finally {
      this.platformsLoading = false;
    }
  }
}

class LibrariesStore {
  list = $state([]);
  loading = $state(false);
  error = $state(null);
  selectedId = $state(ROMM_LIBRARY_ID);
  registering = $state(false);
  // One source per folder Library, kept while the app runs so unsaved edits
  // survive switching Libraries in the picker.
  #sources = new Map();

  get selected() {
    return this.list.find((lib) => lib.id === this.selectedId) ?? null;
  }

  get folders() {
    return this.list.filter((lib) => lib.kind === 'folder');
  }

  /** The rules source for *id*: the RomM store, or a folder Library's own. */
  sourceFor(id) {
    if (id === ROMM_LIBRARY_ID) return romm;
    const library = this.list.find((lib) => lib.id === id);
    if (!library) return null;
    let source = this.#sources.get(id);
    if (!source) {
      source = new FolderRulesSource(library);
      this.#sources.set(id, source);
    }
    return source;
  }

  async load() {
    this.loading = true;
    this.error = null;
    try {
      const data = await api.listLibraries();
      this.list = Array.isArray(data) ? data : [];
      if (!this.list.some((lib) => lib.id === this.selectedId)) {
        this.selectedId = ROMM_LIBRARY_ID;
      }
    } catch (e) {
      this.error = e?.message ?? 'Failed to list Libraries';
    } finally {
      this.loading = false;
    }
    return this.list;
  }

  /** Replace one Library's row with what the backend just returned. */
  adopt(library) {
    if (!library?.id) return;
    const at = this.list.findIndex((lib) => lib.id === library.id);
    this.list = at === -1
      ? [...this.list, library]
      : this.list.map((lib) => (lib.id === library.id ? library : lib));
  }

  select(id) {
    this.selectedId = id || ROMM_LIBRARY_ID;
  }

  /** Open the Rules view on *id*. The RomM view's Automation tab passes `romm`. */
  open(id = ROMM_LIBRARY_ID) {
    this.select(id);
    ui.navigate('rules');
  }

  async register(path, name = null) {
    this.registering = true;
    try {
      const library = await api.registerLibrary(path, name);
      this.adopt(library);
      return library;
    } finally {
      this.registering = false;
    }
  }

  async remove(id) {
    await api.removeLibrary(id);
    this.#sources.delete(id);
    this.list = this.list.filter((lib) => lib.id !== id);
    if (this.selectedId === id) this.selectedId = ROMM_LIBRARY_ID;
  }
}

export const libraries = new LibrariesStore();
