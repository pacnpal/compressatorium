// CHD metadata cache store, hydrates per-path media_type (cd/dvd/etc.) for
// FileList badges, and orchestrates the background metadata scan job.

import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';

class CHDMetadataStore {
  byPath = new SvelteMap();
  scanRunning = $state(false);
  scanStatus = $state(null);

  metadataFor(path) {
    return this.byPath.get(path) ?? null;
  }

  async hydrate(paths) {
    if (!paths?.length) return;
    try {
      const data = await api.getCHDMetadataBatch(paths);
      for (const [path, meta] of Object.entries(data)) {
        this.byPath.set(path, meta);
      }
    } catch (_e) {
      // non-fatal
    }
  }

  async startScan({ force = false } = {}) {
    try {
      const result = await api.scanMetadata(force);
      this.scanRunning = true;
      return result;
    } catch (e) {
      this.scanRunning = false;
      throw e;
    }
  }

  async pollStatus() {
    try {
      this.scanStatus = await api.getScanStatus();
      this.scanRunning = !!this.scanStatus?.scanning;
      return this.scanStatus;
    } catch (_e) {
      this.scanStatus = null;
      this.scanRunning = false;
      return null;
    }
  }
}

export const chdMetadata = new CHDMetadataStore();
