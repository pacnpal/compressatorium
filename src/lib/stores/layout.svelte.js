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

  async #syncFromServer() {
    try {
      const remote = await api.getPreferences();
      if (remote && (remote.panels || remote.columns)) {
        this.#apply(remote);
        writeString(STORAGE_KEYS.LAYOUT, JSON.stringify(this.#serialize()));
      }
    } catch {
      // Offline or endpoint missing — keep local values.
    }
  }

  #persist() {
    const snapshot = this.#serialize();
    writeString(STORAGE_KEYS.LAYOUT, JSON.stringify(snapshot));
    if (!isBrowser) return;
    if (this.#saveTimer) clearTimeout(this.#saveTimer);
    this.#saveTimer = setTimeout(() => {
      this.#saveTimer = null;
      api.putPreferences(snapshot).catch(() => {
        // Best-effort; localStorage already holds the change.
      });
    }, SAVE_DEBOUNCE_MS);
  }
}

export const layout = new LayoutStore();
