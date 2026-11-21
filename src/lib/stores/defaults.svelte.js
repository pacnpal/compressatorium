// The safety choices new conversions and new console settings start with
// (GET/PUT /api/preferences/defaults). Loaded once at boot; `save(patch)`
// writes through and puts the old values back if the server refuses.

import { api } from '$lib/api/endpoints.js';

// The server's own defaults, used until it answers (or if it never does).
export const SAFE_DEFAULTS = Object.freeze({
  verify_after: true,
  delete_on_verify: false,
  container_only: false,
});

class DefaultsStore {
  values = $state({ ...SAFE_DEFAULTS });
  loaded = $state(false);

  async load() {
    try {
      const data = await api.getDefaultsPrefs();
      this.values = { ...SAFE_DEFAULTS, ...pick(data) };
    } catch {
      // Keep the safe defaults; nothing reads this before a user acts.
    } finally {
      this.loaded = true;
    }
  }

  // Saves run one at a time, in call order, so a failed save puts back only
  // what it changed: a later save never starts from values it would undo.
  #queue = Promise.resolve();

  save(patch) {
    const run = this.#queue.then(() => this.#saveNow(patch));
    this.#queue = run.catch(() => {});
    return run;
  }

  async #saveNow(patch) {
    const before = this.values;
    this.values = { ...before, ...pick(patch) };
    try {
      this.values = { ...SAFE_DEFAULTS, ...pick(await api.putDefaultsPrefs(this.values)) };
    } catch (e) {
      this.values = before;
      throw e;
    }
    return this.values;
  }
}

// Only the known boolean fields, so a stray key never reaches the PUT (the
// server refuses unknown fields).
function pick(obj) {
  const out = {};
  for (const key of Object.keys(SAFE_DEFAULTS)) {
    if (typeof obj?.[key] === 'boolean') out[key] = obj[key];
  }
  return out;
}

export const defaults = new DefaultsStore();
