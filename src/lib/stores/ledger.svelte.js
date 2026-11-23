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
  // Finished jobs by status, all time: {completed, failed, cancelled}.
  totals = $state(null);
  // How many History rows passed a check: History's own Checked total (#595),
  // so Activity's count agrees with the list it links to.
  checkedTotal = $state(null);
  _seq = 0;
  _timer = null;

  async _load() {
    const seq = ++this._seq;
    // Each read stands alone: one that fails keeps its card's last good value
    // (the next refresh retries) without dropping the others.
    const [recent, savings, totals, checked] = await Promise.allSettled([
      api.getLedgerRecent(5),
      api.getLedgerSavings(),
      api.getLedgerTotals(),
      api.getLedgerPage({ checked: true, limit: 1 }),
    ]);
    if (seq !== this._seq) return;
    if (recent.status === 'fulfilled') this.recent = recent.value;
    if (savings.status === 'fulfilled') this.savings = savings.value;
    if (totals.status === 'fulfilled') this.totals = totals.value?.jobs ?? null;
    if (checked.status === 'fulfilled' && typeof checked.value?.total === 'number') {
      this.checkedTotal = checked.value.total;
    }
  }

  refresh() {
    clearTimeout(this._timer);
    this._load();
    this._timer = setTimeout(() => this._load(), SETTLE_MS);
  }
}

export const ledger = new LedgerStore();
