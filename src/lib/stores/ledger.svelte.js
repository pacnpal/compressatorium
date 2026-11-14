// Ledger store: the dashboard's view of the permanent job record (#292).
//
// The Ledger row is written by a terminal listener *after* the job's
// completion event goes out, so a fetch fired on that event can land before
// the insert commits. `refresh()` therefore fetches now and once more after a
// short settle delay, and any newer refresh supersedes an older one.

import { api } from '$lib/api/endpoints.js';

// ponytail: fixed settle delay; push a server event after the insert if this
// ever proves too short on a slow disk.
const SETTLE_MS = 1500;

class LedgerStore {
  recent = $state([]);
  savings = $state(null);
  _gen = 0;
  _timer = null;

  async _load(gen) {
    try {
      const [recent, savings] = await Promise.all([
        api.getLedgerRecent(5),
        api.getLedgerSavings(),
      ]);
      if (gen !== this._gen) return;
      this.recent = recent;
      this.savings = savings;
    } catch {
      // Keep the last good values; the next refresh retries.
    }
  }

  refresh() {
    const gen = ++this._gen;
    clearTimeout(this._timer);
    this._load(gen);
    this._timer = setTimeout(() => this._load(gen), SETTLE_MS);
  }
}

export const ledger = new LedgerStore();
