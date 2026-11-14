// Ledger store: the dashboard's view of the permanent job record (#292).
//
// The Ledger row is written by a terminal listener *after* the job's
// completion event goes out, so a fetch fired on that event can land before
// the insert commits. `refresh()` therefore fetches now and once more after a
// short settle delay. Every load takes its own token and only the newest load
// started may write, so a slow earlier load can't overwrite a later one.

import { api } from '$lib/api/endpoints.js';

// ponytail: fixed settle delay; push a server event after the insert if this
// ever proves too short on a slow disk.
const SETTLE_MS = 1500;

class LedgerStore {
  recent = $state([]);
  savings = $state(null);
  _seq = 0;
  _timer = null;

  async _load() {
    const seq = ++this._seq;
    try {
      const [recent, savings] = await Promise.all([
        api.getLedgerRecent(5),
        api.getLedgerSavings(),
      ]);
      if (seq !== this._seq) return;
      this.recent = recent;
      this.savings = savings;
    } catch {
      // Keep the last good values; the next refresh retries.
    }
  }

  refresh() {
    clearTimeout(this._timer);
    this._load();
    this._timer = setTimeout(() => this._load(), SETTLE_MS);
  }
}

export const ledger = new LedgerStore();
