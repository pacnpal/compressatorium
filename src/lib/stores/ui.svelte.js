// Global UI state: active view, sidebar/drawer, modal targets, app version.
//
// Theme management lives in mode-watcher (./App.svelte mounts the
// <ModeWatcher /> component); ThemeToggle reads userPrefersMode / mode
// directly from mode-watcher rather than going through this store.
//
// Toast notifications live in svelte-sonner; callers `import { toast } from
// 'svelte-sonner'` directly. The one piece this store still owns is the
// SSE connection-state toast (reportConnection) because it needs
// debouncing + sticky-toast-with-id tracking that's awkward to inline.

import { toast } from 'svelte-sonner';
import { STORAGE_KEYS, readBool, writeBool } from '$lib/util/localStorage.js';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';
import { VIEW_TITLES, legacyRedirect, parseHash, hashFor } from '$lib/router/routes.js';

export { VIEW_TITLES };

// *value* when *route* is on *view*, else null: a part of another view's
// route never lingers in this one's fields.
function routePart(route, view, value) {
  return route.view === view ? value : null;
}

class UIStore {
  // Routing: one field per part of the route (src/lib/router/routes.js).
  activeView = $state('files');
  // The Library a `#/libraries/<id>` link names, and its tab.
  libraryTarget = $state(null);
  libraryTab = $state(null);
  // Activity's tab, and the Library `#/activity/attention/<id>` filters to.
  activityTab = $state(null);
  activityLibrary = $state(null);
  // The folder a `#/files/<path>` link opens.
  filesPath = $state(null);
  // The Settings section a `#/settings/<section>` link scrolls to.
  settingsSection = $state(null);
  // Tool ids the backend reports unavailable (e.g. Switch with no prod.keys).
  // Only meaningful once `toolsKnown`; read the tool list through
  // `visibleTools`, which draws nothing until then.
  hiddenTools = $state(new Set());
  // True once the first /api/tools answer (or its failure) has been applied.
  toolsKnown = $state(false);
  // Tool id -> why it can't run, with the fix (#515). Shown, never hidden.
  toolReasons = $state({});

  // Layout
  sidebarCollapsed = $state(readBool(STORAGE_KEYS.SIDEBAR_COLLAPSED, false));
  mobileDrawerOpen = $state(false);

  // Metadata from /api/version
  appVersion = $state(null);
  // After queueing from search results, go back to the folder (#542). This
  // browser's choice; it was a server environment variable.
  #searchAutoReturn = $state(readBool(STORAGE_KEYS.SEARCH_AUTO_RETURN, true));

  get searchAutoReturnToFileList() {
    return this.#searchAutoReturn;
  }

  set searchAutoReturnToFileList(on) {
    this.#searchAutoReturn = !!on;
    writeBool(STORAGE_KEYS.SEARCH_AUTO_RETURN, this.#searchAutoReturn);
  }

  // Modal targets, one at a time
  infoTarget = $state(null);
  renameTarget = $state(null);
  // The file-list row a patch is being applied to (#428), null = closed.
  patchTarget = $state(null);
  deleteTarget = $state(null);
  bulkDeleteEntries = $state(null);
  bulkVerifyItems = $state(null);
  // Full paths of the discs a playlist is being made from (null = closed).
  playlistPaths = $state(null);
  // Full paths of the Images a rename to DAT name was asked for (#425).
  datRenamePaths = $state(null);
  duplicateCheck = $state(null);
  deletePlan = $state(null);
  showCancelAll = $state(false);
  // ConvertPanel awaits the user on a plan shown in a dialog (a selection
  // too big to plan live). Observed by App.svelte so background
  // refresh-on-terminal events can't swap entries out from under it.
  planPromptOpen = $state(false);
  // Same idea for the delete-on-verify plan confirmation.
  deletePlanPromptOpen = $state(false);

  // Focus signal, bumped on view change so App.svelte can move focus to
  // the main landmark without screen readers losing context.
  focusBump = $state(0);

  /**
   * The tools to draw (sidebar, quick tools card): none until the backend has
   * said which can run, so an unavailable tool is never shown and then pulled.
   */
  get visibleTools() {
    if (!this.toolsKnown) return [];
    return registry.all().filter((t) => !this.hiddenTools.has(t.id));
  }

  /** The tools that can't run here, each with the backend's reason (#515). */
  get unavailableTools() {
    if (!this.toolsKnown) return [];
    return registry.all()
      .filter((t) => this.hiddenTools.has(t.id))
      .map((t) => ({ ...t, reason: this.toolReasons[t.id] ?? `${t.label} isn't ready here.` }));
  }

  /**
   * True when any modal that depends on file-listing state is open.
   * Used by App.svelte to suppress background refreshes that would
   * swap entries / selection / paging underneath an open dialog. The
   * pure-confirmation modals (cancel-all, clear-completed, help) are
   * intentionally excluded since they don't reference fileBrowser
   * entries.
   */
  get anyEntryModalOpen() {
    return (
      !!this.infoTarget ||
      !!this.renameTarget ||
      !!this.patchTarget ||
      !!this.deleteTarget ||
      !!this.bulkDeleteEntries ||
      !!this.bulkVerifyItems ||
      !!this.playlistPaths ||
      !!this.datRenamePaths ||
      !!this.duplicateCheck ||
      !!this.deletePlan ||
      this.planPromptOpen ||
      this.deletePlanPromptOpen
    );
  }

  // SSE connection-state tracking. Internal, only reportConnection() touches.
  _connectionToastId = null;
  _disconnectTimer = null;

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

  /**
   * Navigate by updating the URL hash; the router reads it back into state.
   * *opts* is `{ id, tab, path }`, as `hashFor` takes it.
   */
  navigate(view, opts = {}) {
    const target = hashFor(view, opts);
    if (typeof window !== 'undefined') {
      if (window.location.hash === target) {
        // Force-apply if already on this hash (the same link clicked again).
        this.applyHash(target);
      } else {
        window.location.hash = target;
      }
    }
  }

  /** Read *hash* into the routing fields. An old route reads as its new one. */
  applyHash(hash) {
    const route = parseHash(legacyRedirect(hash) ?? hash);
    this.activeView = route.view;
    this.libraryTarget = routePart(route, 'libraries', route.id);
    this.libraryTab = routePart(route, 'libraries', route.tab);
    this.activityTab = routePart(route, 'activity', route.tab);
    this.activityLibrary = routePart(route, 'activity', route.id);
    this.filesPath = routePart(route, 'files', route.path);
    this.settingsSection = routePart(route, 'settings', route.tab);
    this.focusBump++;
  }

  /**
   * Apply the backend's tool-availability report (from GET /api/tools).
   * Keeps tools the backend can't run (e.g. Switch without prod.keys) out of
   * the working list, with their reasons for `unavailableTools`. Passing
   * nothing (the fetch failed) shows every tool. Either way the list is now
   * known.
   */
  applyToolAvailability(available, reasons = {}) {
    const ids = registry.ids();
    const allow = new Set(available ?? [...ids]);
    this.hiddenTools = new Set([...ids].filter((id) => !allow.has(id)));
    this.toolReasons = reasons ?? {};
    this.toolsKnown = true;
  }

  /**
   * Called by the jobs SSE wrapper on connection state changes.
   * Brief drops (<1.5s) are NOT announced so we don't spam the toast on
   * momentary blips. On longer drops we open a sticky warning toast,
   * remember its id, and dismiss + surface a brief success toast on
   * recovery.
   */
  reportConnection(state) {
    if (state === 'open') {
      if (this._disconnectTimer) {
        clearTimeout(this._disconnectTimer);
        this._disconnectTimer = null;
      }
      if (this._connectionToastId != null) {
        toast.dismiss(this._connectionToastId);
        this._connectionToastId = null;
        toast.success('Reconnected to backend', { duration: 2500 });
      }
    } else if (state === 'reconnecting') {
      if (this._connectionToastId != null || this._disconnectTimer) return;
      this._disconnectTimer = setTimeout(() => {
        this._disconnectTimer = null;
        this._connectionToastId = toast.warning('Lost connection, retrying…', {
          duration: Number.POSITIVE_INFINITY,
        });
      }, 1500);
    }
  }

  async loadVersion() {
    try {
      const data = await api.getVersion();
      this.appVersion = data?.version ?? null;
    } catch (_e) {
      this.appVersion = null;
    }
  }
}

export const ui = new UIStore();
