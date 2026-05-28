// DAT matching store — wraps /api/dat endpoints. Caches per-path match
// results so FileList rows can render badges without per-row fetches.

import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';

class DATMatchingStore {
  matches = new SvelteMap();
  hasDats = $state(false);
  panelOpen = $state(false);
  importingDat = $state(false);
  syncing = $state(false);
  syncStatus = $state(null);

  matchFor(path) {
    return this.matches.get(path) ?? null;
  }

  async refreshHasDats() {
    try {
      const stats = await api.getDATStats();
      // /api/dat/stats returns total_dats (legacy UI checked this exact field);
      // `total` / `imported_count` are not part of the response.
      this.hasDats = (stats?.total_dats ?? 0) > 0;
      return this.hasDats;
    } catch (_e) {
      this.hasDats = false;
      return false;
    }
  }

  /** Fetch cached matches (never hashes) for a batch of paths. */
  async hydrate(paths) {
    if (!paths?.length) return;
    try {
      const data = await api.getMatchCache(paths);
      const results = data?.results ?? {};
      for (const [path, result] of Object.entries(results)) {
        this.matches.set(path, result);
      }
    } catch (_e) {
      // non-fatal
    }
  }

  async matchBatch(paths) {
    if (!paths?.length) return null;
    try {
      const data = await api.matchBatch(paths);
      const results = data?.results ?? {};
      for (const [path, result] of Object.entries(results)) {
        this.matches.set(path, result);
      }
      return data;
    } catch (_e) {
      return null;
    }
  }

  async startMatchJob(paths) {
    if (!paths?.length) return null;
    return api.startMatchJob(paths);
  }

  async importDAT(file) {
    this.importingDat = true;
    try {
      const result = await api.importDAT(file);
      await this.refreshHasDats();
      return result;
    } finally {
      this.importingDat = false;
    }
  }

  async deleteDAT(datId) {
    const result = await api.deleteDAT(datId);
    await this.refreshHasDats();
    return result;
  }

  async syncMAMERedump(tag = null) {
    // /api/dat/sync only schedules a background task and returns
    // immediately. Do NOT clear `syncing` in a finally — the sync is
    // still running on the backend. The store stays in the syncing
    // state until pollSyncStatus() observes the backend reporting
    // syncing=false, or cancelSync() is called explicitly.
    this.syncing = true;
    try {
      return await api.syncMAMERedump(tag);
    } catch (e) {
      // The start request itself failed — there is no background work
      // to wait for, so clear the flag and re-raise.
      this.syncing = false;
      throw e;
    }
  }

  async pollSyncStatus() {
    try {
      this.syncStatus = await api.getSyncStatus();
      const stillSyncing = !!this.syncStatus?.syncing;
      this.syncing = stillSyncing;
      if (!stillSyncing) {
        // Sync just finished — refresh hasDats so badges flip on.
        await this.refreshHasDats();
      }
      return this.syncStatus;
    } catch (_e) {
      this.syncStatus = null;
      this.syncing = false;
      return null;
    }
  }

  async cancelSync() {
    try {
      const res = await api.cancelSync();
      this.syncing = false;
      return res;
    } catch (_e) {
      return null;
    }
  }
}

export const datMatching = new DATMatchingStore();
