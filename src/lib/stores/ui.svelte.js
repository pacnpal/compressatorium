// Global UI state: active view, sidebar/drawer, theme, modal targets,
// app version, and a tiny notification surface. Owns all DOM-level theme
// side effects via an effect set up in App.svelte's onMount.

import { STORAGE_KEYS, readBool, readString, writeBool, writeString } from '$lib/util/localStorage.js';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';

const VIEWS = Object.freeze(['dashboard', 'workspace', 'dat', 'help']);
const THEMES = Object.freeze(['light', 'dark', 'system']);
// Tool ids come from the registry — single source of truth, no
// hardcoded set to keep in sync when a 4th tool is added.
const VALID_TOOLS = registry.ids();
const DEFAULT_VIEW = 'workspace';

function loadTheme() {
  const raw = readString(STORAGE_KEYS.THEME, 'system');
  return THEMES.includes(raw) ? raw : 'system';
}

function loadPrimaryTool() {
  const raw = readString(STORAGE_KEYS.PRIMARY_TOOL, 'chdman');
  return VALID_TOOLS.has(raw) ? raw : 'chdman';
}

function systemPrefersDark() {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

class UIStore {
  // Routing
  activeView = $state(DEFAULT_VIEW);
  workspaceTool = $state(loadPrimaryTool());

  // Layout
  sidebarCollapsed = $state(readBool(STORAGE_KEYS.SIDEBAR_COLLAPSED, false));
  mobileDrawerOpen = $state(false);

  // Theme
  theme = $state(loadTheme());
  systemIsDark = $state(systemPrefersDark());

  // Metadata from /api/version
  appVersion = $state(null);
  searchAutoReturnToFileList = $state(true);

  // Modal targets — one at a time
  chdInfoTarget = $state(null);
  renameTarget = $state(null);
  deleteTarget = $state(null);
  bulkDeleteEntries = $state(null);
  bulkVerifyItems = $state(null);
  duplicateCheck = $state(null);
  deletePlan = $state(null);
  showCancelAll = $state(false);
  showClearDone = $state(false);
  showHelp = $state(false);

  // Transient notifications
  notification = $state(null);

  // Connection status (used by SSE → notification wiring)
  connectionStatus = $state('connecting');  // 'connecting' | 'open' | 'reconnecting'

  // Focus signal — bumped on view change so App.svelte can move focus to
  // the main landmark without screen readers losing context.
  focusBump = $state(0);

  _notifyTimer = null;
  _disconnectTimer = null;
  _disconnectAnnounced = false;

  get resolvedTheme() {
    return this.theme === 'system' ? (this.systemIsDark ? 'dark' : 'light') : this.theme;
  }

  setTheme(t) {
    if (!THEMES.includes(t)) return;
    this.theme = t;
    writeString(STORAGE_KEYS.THEME, t);
    this.applyTheme();
  }

  cycleTheme() {
    const order = ['light', 'dark', 'system'];
    const next = order[(order.indexOf(this.theme) + 1) % order.length];
    this.setTheme(next);
  }

  applyTheme() {
    if (typeof document !== 'undefined') {
      document.documentElement.dataset.theme = this.resolvedTheme;
    }
  }

  toggleSidebar() {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    writeBool(STORAGE_KEYS.SIDEBAR_COLLAPSED, this.sidebarCollapsed);
  }

  openDrawer() {
    this.mobileDrawerOpen = true;
  }

  closeDrawer() {
    this.mobileDrawerOpen = false;
  }

  /** Navigate by updating the URL hash; the router reads it back into state. */
  navigate(view, tool) {
    if (!VIEWS.includes(view)) return;
    let target = `#/${view}`;
    if (view === 'workspace' && tool && VALID_TOOLS.has(tool)) {
      target += `/${tool}`;
    }
    if (typeof window !== 'undefined') {
      if (window.location.hash === target) {
        // Force-apply if already on this hash (e.g. switching tools).
        this.applyHash(target);
      } else {
        window.location.hash = target;
      }
    }
  }

  applyHash(hash) {
    const cleaned = (hash ?? '').replace(/^#/, '').replace(/^\/+/, '');
    if (!cleaned) {
      this.activeView = DEFAULT_VIEW;
      this.focusBump++;
      return;
    }
    const [view, sub] = cleaned.split('/');
    if (VIEWS.includes(view)) {
      this.activeView = view;
      if (view === 'workspace' && sub && VALID_TOOLS.has(sub)) {
        this.workspaceTool = sub;
        writeString(STORAGE_KEYS.PRIMARY_TOOL, sub);
      }
    } else {
      this.activeView = DEFAULT_VIEW;
    }
    this.focusBump++;
  }

  setWorkspaceTool(toolId) {
    if (!VALID_TOOLS.has(toolId)) return;
    this.workspaceTool = toolId;
    writeString(STORAGE_KEYS.PRIMARY_TOOL, toolId);
    if (this.activeView === 'workspace') {
      this.navigate('workspace', toolId);
    }
  }

  notify(message, kind = 'info', ttl = 4000) {
    this.notification = { message, kind, id: Date.now() };
    if (this._notifyTimer) clearTimeout(this._notifyTimer);
    if (ttl > 0) {
      this._notifyTimer = setTimeout(() => {
        this.notification = null;
        this._notifyTimer = null;
      }, ttl);
    }
  }

  dismissNotification() {
    if (this._notifyTimer) {
      clearTimeout(this._notifyTimer);
      this._notifyTimer = null;
    }
    this.notification = null;
  }

  /**
   * Called by the jobs SSE wrapper on connection state changes.
   * Surfaces brief reconnection notices and clears stale errors on
   * recovery. Brief drops (< 1.5s) are not announced to avoid
   * spamming the toast on momentary network blips.
   */
  reportConnection(state) {
    this.connectionStatus = state;
    if (state === 'open') {
      if (this._disconnectTimer) {
        clearTimeout(this._disconnectTimer);
        this._disconnectTimer = null;
      }
      if (this._disconnectAnnounced) {
        this._disconnectAnnounced = false;
        this.notify('Reconnected to backend', 'success', 2500);
      }
    } else if (state === 'reconnecting') {
      // Debounce so flaps don't spam.
      if (this._disconnectAnnounced || this._disconnectTimer) return;
      this._disconnectTimer = setTimeout(() => {
        this._disconnectTimer = null;
        this._disconnectAnnounced = true;
        // ttl 0 = sticky until we receive an 'open' state.
        this.notify('Lost connection — retrying…', 'warning', 0);
      }, 1500);
    }
  }

  async loadVersion() {
    try {
      const data = await api.getVersion();
      this.appVersion = data?.version ?? null;
      if (typeof data?.search_auto_return_to_file_list === 'boolean') {
        this.searchAutoReturnToFileList = data.search_auto_return_to_file_list;
      }
    } catch (_e) {
      this.appVersion = null;
    }
  }
}

export const ui = new UIStore();
