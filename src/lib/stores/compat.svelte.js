// The "Compatible with..." table (#429), fetched once from the server. Shared
// by the compression picker and the Rule editor so neither holds a copy.

import { api } from '$lib/api/endpoints.js';
import { compatTargetsForMode } from '$lib/tools/compat.js';

class CompatStore {
  table = $state(null);
  #loading = null;

  /** Fetch the table once; a failed fetch leaves the controls hidden. */
  load() {
    if (!this.#loading) {
      this.#loading = api
        .getCompatPresets()
        .then((data) => {
          this.table = data;
        })
        .catch(() => {
          this.#loading = null;
        });
    }
    return this.#loading;
  }

  /** The target with `id`, or null. */
  target(id) {
    return this.table?.targets?.find((t) => t.id === id) ?? null;
  }

  /** Targets that have an entry for `mode`. */
  targetsForMode(mode) {
    return compatTargetsForMode(this.table, mode);
  }
}

export const compat = new CompatStore();
