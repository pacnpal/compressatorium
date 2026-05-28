// Global UI state: active view, sidebar/drawer, theme, modal targets,
// app version, and a tiny notification surface. Owns all DOM-level theme
// side effects via an effect set up in App.svelte's onMount.

import { STORAGE_KEYS, readString, writeString } from '$lib/util/localStorage.js';
import { api } from '$lib/api/endpoints.js';

const VIEWS = Object.freeze(['dashboard', 'workspace', 'dat', 'help']);
const THEMES = Object.freeze(['light', 'dark', 'system']);
const VALID_TOOLS = new Set(['chdman', 'dolphin', 'z3ds']);

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
  activeView = $state('dashboard');
  workspaceTool = $state(loadPrimaryTool());

  // Layout
  sidebarCollapsed = $state(false);
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
  _notifyTimer = null;

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
      this.activeView = 'dashboard';
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
      this.activeView = 'dashboard';
    }
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
    this._notifyTimer = setTimeout(() => {
      this.notification = null;
      this._notifyTimer = null;
    }, ttl);
  }

  dismissNotification() {
    if (this._notifyTimer) {
      clearTimeout(this._notifyTimer);
      this._notifyTimer = null;
    }
    this.notification = null;
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
