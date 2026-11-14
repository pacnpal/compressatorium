// Libraries (#303): RomM, and every folder registered as one.
//
// The Rules view edits one Library's Rules at a time through a *rules source*.
// The RomM store is the RomM Library's source; `FolderRulesSource` below is a
// folder Library's. Both carry the same members, which is all the editor
// (`RuleEditor.svelte`) reads:
//
//   platforms, imageNoun, imageCount(platform), platformName(id),
//   emptyTitle, emptyHint, matchedLabel, unmatchedLabel,
//   rules, ruleDefaults, ruleOptions, ruleState,
//   rulesLoading, rulesSaving, rulesError, rulesDirty,
//   loadRules(), ruleFor(id), lastRunFor(id), setRule(id, rule),
//   removeRule(id), saveRules(),
//   modeIsAutomatable(mode), losesDatMatch(spec),
//   convertedCountFor(id), forgetConverted(ids),
//   sweeping, sweepResult, previewSweep(), runSweep(),
//   scheduleEnabled, setScheduleEnabled(enabled).
//
// What a Library allows comes from the backend: a folder Library's options
// list only the `skip` duplicate policy, so the editor never offers the others.

import { api } from '$lib/api/endpoints.js';
import { romm } from '$lib/stores/romm.svelte.js';
import { ui } from '$lib/stores/ui.svelte.js';

export const ROMM_LIBRARY_ID = 'romm';

/** One folder Library's Rules, platforms and sweeps. */
export class FolderRulesSource {
  imageNoun = 'Images';
  emptyTitle = 'No Platforms found';
  emptyHint = 'No Image in this folder has a Platform yet. Images in a folder named after '
    + 'a Platform (ps2, gc, psx, ...) or with a readable header are picked up.';
  matchedLabel = 'Only Recognized Images (a DAT or Hasheous match)';
  unmatchedLabel = 'Only Images not Recognized';

  library = $state(null);
  platforms = $state([]);
  counts = $state({ unsorted: 0, conflicted: 0, unreadable: 0 });
  automatableModes = $state(null);
  platformsLoading = $state(false);
  platformsError = $state(null);

  rules = $state({});
  ruleDefaults = $state(null);
  ruleOptions = $state({ orders: [], duplicate_actions: ['skip'], days: [] });
  ruleState = $state({});
  rulesLoading = $state(false);
  rulesSaving = $state(false);
  rulesError = $state(null);
  rulesDirty = $state(false);
  sweepResult = $state(null);
  sweeping = $state(false);
  // Bumped by every edit, so a save that an edit overtook does not adopt the
  // older server copy (the same guard the RomM store has).
  #revision = 0;

  constructor(library) {
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

  async loadRules() {
    this.rulesLoading = true;
    this.rulesError = null;
    try {
      const data = await api.getLibraryRules(this.id);
      if (!this.rulesDirty) this.rules = data?.rules ?? {};
      this.ruleDefaults = data?.defaults ?? null;
      this.ruleOptions = data?.options ?? this.ruleOptions;
      this.ruleState = data?.state ?? {};
    } catch (e) {
      this.rulesError = e?.message ?? 'Failed to load the Rules';
      throw e;
    } finally {
      this.rulesLoading = false;
    }
    return this.rules;
  }

  ruleFor(platformId) {
    const key = String(platformId);
    if (this.rules[key]) return { ...this.rules[key] };
    return { ...(this.ruleDefaults ?? {}) };
  }

  lastRunFor(platformId) {
    return this.ruleState[String(platformId)] ?? null;
  }

  setRule(platformId, rule) {
    this.rules = { ...this.rules, [String(platformId)]: rule };
    this.rulesDirty = true;
    this.#revision += 1;
  }

  removeRule(platformId) {
    const next = { ...this.rules };
    delete next[String(platformId)];
    this.rules = next;
    this.rulesDirty = true;
    this.#revision += 1;
  }

  async saveRules() {
    this.rulesSaving = true;
    const revision = this.#revision;
    try {
      const data = await api.saveLibraryRules(this.id, this.rules);
      if (revision !== this.#revision) return this.rules;
      this.rules = data?.rules ?? this.rules;
      this.rulesDirty = false;
      return this.rules;
    } finally {
      this.rulesSaving = false;
    }
  }

  modeIsAutomatable(mode) {
    return Array.isArray(this.automatableModes) ? this.automatableModes.includes(mode) : true;
  }

  /** RomM's metadata warning; a folder has no RomM metadata to lose. */
  losesDatMatch() {
    return false;
  }

  convertedCountFor(platformId) {
    const entry = this.ruleState[String(platformId)] ?? {};
    return entry.converted && typeof entry.converted === 'object'
      ? Object.keys(entry.converted).length
      : 0;
  }

  async forgetConverted(platformIds = null) {
    const data = await api.forgetLibraryConverted(this.id, platformIds);
    this.ruleState = data?.state ?? this.ruleState;
    return data?.cleared ?? 0;
  }

  async previewSweep(platformIds = null) {
    this.sweeping = true;
    try {
      this.sweepResult = await api.previewLibrary(
        this.id, platformIds ? { platform_ids: platformIds } : {},
      );
      return this.sweepResult;
    } finally {
      this.sweeping = false;
    }
  }

  async runSweep(platformIds = null) {
    this.sweeping = true;
    try {
      this.sweepResult = await api.runLibrary(
        this.id, platformIds ? { platform_ids: platformIds } : {},
      );
      await this.loadRules().catch(() => {});
      return this.sweepResult;
    } finally {
      this.sweeping = false;
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
