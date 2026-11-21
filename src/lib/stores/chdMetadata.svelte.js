// CHD metadata cache store, hydrates per-path media_type (cd/dvd/etc.) for
// FileList badges, and orchestrates the background metadata scan job.

import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';

class CHDMetadataStore {
  byPath = new SvelteMap();
  scanRunning = $state(false);
  scanStatus = $state(null);
  // Bumped by every status read and every scan start: only the latest one
  // writes, so a status read that set off before Scan was pressed can't say
  // the new scan isn't running.
  #seq = 0;

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
    this.#seq += 1;
    const result = await api.scanMetadata(force);
    // The scan is running now: any status read still in flight predates it.
    this.#seq += 1;
    this.scanRunning = true;
    return result;
  }

  async pollStatus() {
    const mine = ++this.#seq;
    try {
      const status = await api.getScanStatus();
      if (mine !== this.#seq) return this.scanStatus;
      this.scanStatus = status;
      this.scanRunning = !!status?.scanning;
      return status;
    } catch (_e) {
      // A failed read says nothing about the scan: keep what is known, so a
      // running scan keeps being polled.
      return this.scanStatus;
    }
  }
}

export const chdMetadata = new CHDMetadataStore();
