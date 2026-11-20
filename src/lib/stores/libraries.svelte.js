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
import { LatestSaveQueue } from '$lib/stores/latestSaveQueue.js';
import { romm } from '$lib/stores/romm.svelte.js';
import { ui } from '$lib/stores/ui.svelte.js';

export const ROMM_LIBRARY_ID = 'romm';

// The backend's 409 detail when the folder's previous walk is still running
// (routes/libraries.py LISTING_IN_PROGRESS).
const LISTING_IN_PROGRESS = 'listing_in_progress';

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
  // The previous walk of this folder is still running (a slow or stuck
  // mount). A wait, not an error: the view offers a retry.
  platformsBusy = $state(false);

  constructor(library) {
    // The endpoints read `this.id` (the reactive `library` field) when they
    // run, after construction; nothing is captured from the constructor.
    super({
      getRules: () => api.getLibraryRules(this.id),
      saveRules: (rules, checkRules) => api.saveLibraryRules(this.id, rules, checkRules),
      forgetConverted: (platformIds) => api.forgetLibraryConverted(this.id, platformIds),
      preview: (body) => api.previewLibrary(this.id, body),
      run: (body) => api.runLibrary(this.id, body),
      recommended: () => api.getRecommendedRules(this.id),
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
    await libraries.saveSettings(this.id, { schedule_enabled: enabled });
  }

  /** Used by an EmulationStation frontend: conversions update gamelist.xml (#386). */
  get esFrontend() {
    return this.library?.es_frontend === true;
  }

  async setEsFrontend(enabled) {
    await libraries.saveSettings(this.id, { es_frontend: enabled });
  }

  /** Its ES-DE gamelists folder, or null (#387). */
  get gamelistsFolder() {
    return this.library?.gamelists_folder ?? null;
  }

  /** Save it ('' clears); the backend refuses one outside every Volume. */
  async setGamelistsFolder(path) {
    await libraries.saveSettings(this.id, { gamelists_folder: path });
  }

  /**
   * Take the Library's latest settings (name, schedule switch, ...) in place.
   * Only the Library row changes; the Rules and any unsaved edit to them live
   * on RulesSource and are left alone.
   */
  adoptLibrary(library) {
    if (library?.id === this.id) this.library = library;
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
    // The settings generation this read started at; see `LibrariesStore.adopt`.
    const readAt = libraries.settingsGeneration(this.id);
    try {
      const data = await api.getLibraryPlatforms(this.id);
      this.platformsBusy = false;
      // The response carries the Library's current row; a setting changed
      // elsewhere (another tab) shows here, in the picker and in this source.
      // Unless a settings save landed while this read was out: then the row
      // is older than the save and is not adopted. The Platforms still are.
      if (data?.library) libraries.adopt(data.library, readAt);
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
      if (e?.status === 409 && e?.message === LISTING_IN_PROGRESS) {
        // Keep whatever was listed before; the walk will finish.
        this.platformsBusy = true;
        return;
      }
      this.platformsBusy = false;
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
  // Library id -> the facts its card shows (`{ consoles, games, watchFolders }`),
  // null while they load. Read lazily, once per card; see `loadFacts`.
  facts = $state({});
  // One source per folder Library, kept while the app runs so unsaved edits
  // survive switching Libraries in the picker.
  #sources = new Map();
  // Libraries removed while the app runs. A response that set off before the
  // removal (a Platform listing, a list load, a settings save) can answer
  // after it; it must not put the Library back. Safe to keep for good: a
  // folder Library's id is never reused (the table is AUTOINCREMENT).
  #removed = new Set();
  // Library id -> its settings generation, bumped when a settings save starts
  // and again when it lands (`saveSettings`). A read records the generation it
  // started at; a Library row it brings back is adopted only if no save began
  // since, so a read older than a save never puts the old settings back.
  #settingsGenerations = new Map();
  // Library id -> its settings saves, one at a time in click order so the
  // backend applies them in that order, of which only the latest save's answer
  // is adopted: two in flight could land the older one last.
  #saves = new LatestSaveQueue();
  // Bumped by every registration. A Library registered after a list load set
  // off is missing from that load's (older) catalogue and is kept, not dropped.
  #catalogueEpoch = 0;
  // Library id -> the catalogue epoch it was registered at.
  #registeredAt = new Map();

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
    const readAt = new Map(this.#settingsGenerations);
    const epoch = this.#catalogueEpoch;
    try {
      const data = await api.listLibraries();
      const current = new Map(this.list.map((lib) => [lib.id, lib]));
      // A row whose settings were saved while this read was out keeps the
      // saved row; see `adopt`.
      const rows = (Array.isArray(data) ? data : [])
        .filter((lib) => !this.#removed.has(lib.id))
        .map((lib) => (
          this.#isStale(lib.id, readAt.get(lib.id) ?? 0) && current.has(lib.id)
            ? current.get(lib.id)
            : lib
        ));
      // A Library registered while this read was out is not in its catalogue.
      const listed = new Set(rows.map((lib) => lib.id));
      const added = this.list.filter((lib) => (
        !listed.has(lib.id) && (this.#registeredAt.get(lib.id) ?? -1) >= epoch
      ));
      this.list = [...rows, ...added];
      this.#refreshSources(this.list);
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

  /** The settings generation of Library *id*; a read records it when it starts. */
  settingsGeneration(id) {
    return this.#settingsGenerations.get(id) ?? 0;
  }

  #isStale(id, readAt) {
    return readAt !== this.settingsGeneration(id);
  }

  #bumpSettings(id) {
    this.#settingsGenerations.set(id, this.settingsGeneration(id) + 1);
  }

  /**
   * Save Library *id*'s settings (*patch*: name, schedule switch, ...). The one
   * way a settings change is written from the app. Bumps the settings
   * generation as the save starts and again when it lands, so any read that
   * set off before the save answered is too old to adopt.
   *
   * A Library's saves are queued and sent one at a time, in click order, so
   * the backend applies them in that order. Only the latest save asked for
   * is adopted. If the latest save fails, the row is read back from the
   * server once the queue has drained (it is the last in it), since an
   * earlier save may have landed without being adopted.
   */
  saveSettings(id, patch) {
    // Reads that set off from here on are older than this save.
    this.#bumpSettings(id);
    return this.#saves.schedule(id, (ticket) => this.#saveNow(id, patch, ticket));
  }

  async #saveNow(id, patch, ticket) {
    this.#bumpSettings(id);
    let saved;
    try {
      saved = await api.updateLibrary(id, patch);
    } catch (e) {
      if (this.#saves.isLatest(id, ticket)) await this.#reloadRow(id);
      throw e;
    } finally {
      this.#bumpSettings(id);
    }
    if (this.#saves.isLatest(id, ticket)) this.adopt(saved);
    return saved;
  }

  /** Read Library *id*'s row back from the server, unless a save starts meanwhile. */
  async #reloadRow(id) {
    const readAt = this.settingsGeneration(id);
    try {
      const rows = await api.listLibraries();
      const row = Array.isArray(rows) ? rows.find((lib) => lib.id === id) : null;
      if (row) this.adopt(row, readAt);
    } catch {
      // The save's own error is what the caller reports; the next load fixes the row.
    }
  }

  /**
   * Replace one Library's row with what the backend just returned.
   *
   * *readAt* is the settings generation a read started at (`settingsGeneration`).
   * Given, the row is adopted only if no settings save began since: an older
   * read would put the previous settings back over the saved ones. A save's
   * own answer passes none, and is always adopted.
   */
  adopt(library, readAt = null) {
    if (!library?.id || this.#removed.has(library.id)) return;
    if (readAt !== null && this.#isStale(library.id, readAt)) return;
    const at = this.list.findIndex((lib) => lib.id === library.id);
    this.list = at === -1
      ? [...this.list, library]
      : this.list.map((lib) => (lib.id === library.id ? library : lib));
    this.#refreshSources([library]);
  }

  /** Hand each cached source its Library's fresh row, keeping the source (and its edits). */
  #refreshSources(rows) {
    for (const row of rows) this.#sources.get(row.id)?.adoptLibrary(row);
  }

  select(id) {
    this.selectedId = id || ROMM_LIBRARY_ID;
  }

  /**
   * The facts Library *id*'s card shows: how many consoles and games it has,
   * and how many watch folders. Read once; a part that can't be read is left
   * out rather than shown as nought.
   */
  async loadFacts(id) {
    if (id in this.facts) return;
    this.facts = { ...this.facts, [id]: null };
    const [listing, inboxes] = await Promise.allSettled([
      id === ROMM_LIBRARY_ID ? api.getRommPlatforms() : api.getLibraryPlatforms(id),
      api.listInboxes(id),
    ]);
    const facts = {};
    if (listing.status === 'fulfilled') {
      const platforms = id === ROMM_LIBRARY_ID ? listing.value : listing.value?.platforms;
      const rows = Array.isArray(platforms) ? platforms : [];
      facts.consoles = rows.length;
      facts.games = rows.reduce((n, p) => n + (p.images ?? p.rom_count ?? 0), 0);
    }
    if (inboxes.status === 'fulfilled') {
      facts.watchFolders = inboxes.value?.inboxes?.length ?? 0;
    }
    this.facts = { ...this.facts, [id]: facts };
  }

  /** Open Library *id*'s page, on *tab* when given (`#/libraries/<id>[/<tab>]`). */
  open(id = ROMM_LIBRARY_ID, tab = null) {
    this.select(id);
    ui.navigate('libraries', { id, tab });
  }

  async register(path, name = null) {
    this.registering = true;
    try {
      const library = await api.registerLibrary(path, name);
      if (library?.id) this.#registeredAt.set(library.id, this.#catalogueEpoch++);
      this.adopt(library);
      return library;
    } finally {
      this.registering = false;
    }
  }

  async remove(id) {
    await api.removeLibrary(id);
    this.#removed.add(id);
    this.#sources.delete(id);
    this.list = this.list.filter((lib) => lib.id !== id);
    const { [id]: _gone, ...facts } = this.facts;
    this.facts = facts;
    if (this.selectedId === id) this.selectedId = ROMM_LIBRARY_ID;
  }
}

export const libraries = new LibrariesStore();
