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
  dats = $state([]);
  datsLoading = $state(false);
  datsError = $state(null);
  stats = $state(null);

  matchFor(path) {
    return this.matches.get(path) ?? null;
  }

  async refreshHasDats() {
    try {
      const stats = await api.getDATStats();
      this.stats = stats;
      // /api/dat/stats returns total_dats (legacy UI checked this exact field);
      // `total` / `imported_count` are not part of the response.
      this.hasDats = (stats?.total_dats ?? 0) > 0;
      return this.hasDats;
    } catch (_e) {
      this.hasDats = false;
      return false;
    }
  }

  async loadDATs() {
    this.datsLoading = true;
    this.datsError = null;
    try {
      // /api/dat/list returns a bare array of DAT records
      // (app/services/dat_store.py:list_dats), not an envelope. Each
      // record: { id, name, description, version, imported_at, file_count }.
      const data = await api.listDATs();
      this.dats = Array.isArray(data) ? data : (Array.isArray(data?.dats) ? data.dats : []);
      await this.refreshHasDats();
    } catch (e) {
      this.datsError = e?.message ?? 'Failed to load DATs';
    } finally {
      this.datsLoading = false;
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

  async deleteDAT(datId) {
    const result = await api.deleteDAT(datId);
    // The backend cascades DATMatch rows for the deleted DAT, so any
    // cached match entry that survives in the client map is now
    // stale — files that were only matched by this DAT would keep
    // showing the badge until next reload because hydrate() only adds
    // returned rows, never removes absent ones. Drop the whole cache
    // so the next FileList hydration re-establishes truth.
    this.matches.clear();
    await this.loadDATs();
    return result;
  }

  async importDAT(file) {
    this.importingDat = true;
    try {
      const result = await api.importDAT(file);
      // The backend's _import_dat_sync wipes the DATMatch cache because
      // newly-imported hashes may flip match results for files the
      // user already has. Mirror that on the client so stale badges
      // don't survive until the next visit — hydrate() only adds rows,
      // it never removes absent ones.
      this.matches.clear();
      await this.loadDATs();
      return result;
    } finally {
      this.importingDat = false;
    }
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
      // 409 means a MAMERedump sync is already running on the
      // backend. The legacy UI treated that as "good — keep polling
      // and observe progress"; clearing `syncing` here would freeze
      // any consumer poll/progress UI even though the backend is
      // actively working. Stay in the syncing state.
      if (e?.status === 409) return null;
      // Any other error means the start request itself failed —
      // there is no background work to wait for, so clear the flag
      // and re-raise.
      this.syncing = false;
      throw e;
    }
  }

  async pollSyncStatus() {
    const wasSyncing = this.syncing;
    try {
      this.syncStatus = await api.getSyncStatus();
      const stillSyncing = !!this.syncStatus?.syncing;
      this.syncing = stillSyncing;
      if (!stillSyncing && wasSyncing) {
        // Sync just finished (syncing → done transition). The backend
        // has persisted the new DAT set and may have dropped the old
        // one, so reload the full list — not just hasDats — and clear
        // the stale match cache (new hashes can flip prior matches).
        // loadDATs() refreshes hasDats internally.
        this.matches.clear();
        await this.loadDATs();
      } else if (!stillSyncing) {
        // Cold poll (e.g. on mount) with no sync running: cheap
        // hasDats refresh is enough; no need to reload the whole list.
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
