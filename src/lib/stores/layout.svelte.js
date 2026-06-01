// Workspace layout widths: the panel split (file list vs the right
// Convert/Jobs panel) and the file table's column widths.
//
// Persistence is local-first with server sync. localStorage is read
// synchronously on construct so widths apply with no flash; the server
// copy is then fetched and, if present, wins and is written back to
// localStorage. Every change writes localStorage immediately and PUTs
// to the server debounced, so the layout survives a browser/cache wipe
// and follows the user across browsers.
//
// Panel widths are global. Column widths are per tool (chdman / dolphin
// / z3ds) because each tool lists different file types.

import { STORAGE_KEYS, readString, writeString } from '$lib/util/localStorage.js';
import { api } from '$lib/api/endpoints.js';

const SAVE_DEBOUNCE_MS = 500;

// Defaults mirror the static CSS the resizable layout replaces.
const DEFAULT_PANELS = Object.freeze({ left: 220, right: 360 });
const DEFAULT_COLUMNS = Object.freeze({ name: 360, size: 80, ext: 60, status: 160 });

// Clamp ranges keep a drag from collapsing a panel/column to nothing or
// stretching it past anything useful.
const PANEL_LIMITS = Object.freeze({
  left: { min: 160, max: 360 },
  right: { min: 280, max: 640 },
});
const COLUMN_LIMITS = Object.freeze({
  name: { min: 120, max: 900 },
  size: { min: 48, max: 240 },
  ext: { min: 48, max: 240 },
  status: { min: 80, max: 480 },
});

const isBrowser = typeof window !== 'undefined';

function clamp(value, { min, max }) {
  const n = Number(value);
  if (!Number.isFinite(n)) return min;
  return Math.min(max, Math.max(min, Math.round(n)));
}

function readLocal() {
  const raw = readString(STORAGE_KEYS.LAYOUT, null);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

class LayoutStore {
  panels = $state({ ...DEFAULT_PANELS });
  // { [toolId]: { name, size, ext, status } } — only the keys a user has
  // actually dragged are stored; columnsFor() merges over defaults.
  columns = $state({});

  #saveTimer = null;
  // Bumped on every local edit. Captured before the boot server fetch so
  // a late response can't clobber a width the user changed in the
  // meantime.
  #mutationVersion = 0;

  constructor() {
    const local = readLocal();
    if (local) this.#apply(local);
    // Reconcile with the server copy after first paint. Fire-and-forget:
    // a failed fetch just leaves the local values in place.
    if (isBrowser) this.#syncFromServer();
  }

  // ── Reads ────────────────────────────────────────────────────────────

  /** Column widths for a tool, defaults filled in for untouched columns. */
  columnsFor(toolId) {
    return { ...DEFAULT_COLUMNS, ...(this.columns[toolId] ?? {}) };
  }

  /** Min/max for a column, for the resize handle's aria-value range. */
  columnLimit(col) {
    return COLUMN_LIMITS[col] ?? { min: undefined, max: undefined };
  }

  // ── Mutations ────────────────────────────────────────────────────────

  setPanelWidth(side, px) {
    const limits = PANEL_LIMITS[side];
    if (!limits) return;
    this.panels = { ...this.panels, [side]: clamp(px, limits) };
    this.#persist();
  }

  resetPanel(side) {
    if (!(side in DEFAULT_PANELS)) return;
    this.panels = { ...this.panels, [side]: DEFAULT_PANELS[side] };
    this.#persist();
  }

  setColumnWidth(toolId, col, px) {
    const limits = COLUMN_LIMITS[col];
    if (!limits || !toolId) return;
    const current = this.columns[toolId] ?? {};
    this.columns = { ...this.columns, [toolId]: { ...current, [col]: clamp(px, limits) } };
    this.#persist();
  }

  resetColumn(toolId, col) {
    const current = this.columns[toolId];
    if (!current || !(col in current)) return;
    const next = { ...current };
    delete next[col];
    this.columns = { ...this.columns, [toolId]: next };
    this.#persist();
  }

  // ── Persistence ──────────────────────────────────────────────────────

  #apply(data) {
    if (data.panels && typeof data.panels === 'object') {
      const next = { ...this.panels };
      for (const side of ['left', 'right']) {
        if (data.panels[side] != null) next[side] = clamp(data.panels[side], PANEL_LIMITS[side]);
      }
      this.panels = next;
    }
    if (data.columns && typeof data.columns === 'object') {
      const next = {};
      for (const [toolId, cols] of Object.entries(data.columns)) {
        if (!cols || typeof cols !== 'object') continue;
        const clean = {};
        for (const [col, px] of Object.entries(cols)) {
          if (COLUMN_LIMITS[col] && px != null) clean[col] = clamp(px, COLUMN_LIMITS[col]);
        }
        if (Object.keys(clean).length) next[toolId] = clean;
      }
      this.columns = next;
    }
  }

  #serialize() {
    return { panels: { ...this.panels }, columns: { ...this.columns } };
  }

  // `dirty` marks localStorage as holding an edit not yet confirmed saved
  // to the server. If the tab closes before the debounced PUT fires, the
  // next boot sees the flag and pushes local up instead of pulling stale
  // server data down.
  #writeLocal(dirty) {
    writeString(STORAGE_KEYS.LAYOUT, JSON.stringify({ ...this.#serialize(), dirty }));
  }

  async #syncFromServer() {
    const local = readLocal();
    if (local?.dirty) {
      // Local has an unsynced edit — it wins. Push it up rather than
      // overwriting it with whatever the server still holds.
      const before = this.#mutationVersion;
      try {
        await api.putPreferences(this.#serialize());
        // Only clear dirty if the user didn't drag again during the PUT;
        // otherwise that newer edit is still unsaved and must stay dirty.
        if (this.#mutationVersion === before) this.#writeLocal(false);
      } catch {
        // Still offline; leave dirty set so the next boot retries.
      }
      return;
    }
    const before = this.#mutationVersion;
    try {
      const remote = await api.getPreferences();
      // The user dragged something while the fetch was in flight — keep
      // their in-progress layout instead of snapping back to the server's.
      if (this.#mutationVersion !== before) return;
      if (remote && (remote.panels || remote.columns)) {
        this.#apply(remote);
        this.#writeLocal(false);
      }
    } catch {
      // Offline or endpoint missing — keep local values.
    }
  }

  #persist() {
    this.#mutationVersion += 1;
    const version = this.#mutationVersion;
    // Mark dirty immediately so an unflushed change survives a tab close.
    this.#writeLocal(true);
    if (!isBrowser) return;
    if (this.#saveTimer) clearTimeout(this.#saveTimer);
    this.#saveTimer = setTimeout(() => {
      this.#saveTimer = null;
      const snapshot = this.#serialize();
      api.putPreferences(snapshot)
        .then(() => {
          // Only clear dirty if no newer edit landed while saving.
          if (this.#mutationVersion === version) this.#writeLocal(false);
        })
        .catch(() => {
          // Best-effort; localStorage keeps the dirty change.
        });
    }, SAVE_DEBOUNCE_MS);
  }
}

export const layout = new LayoutStore();
