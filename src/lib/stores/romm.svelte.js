// RomM integration state: connection, settings, platforms, automation rules,
// and the metadata re-pin queue.
//
// The ROM rows themselves deliberately live in `fileBrowser` (RomM is a third
// listing source there, alongside search and archive views) because the backend
// returns the same DirectoryListing/FileEntry shape a directory does — so
// FileList, FileRow and ConvertPanel render the catalog unchanged. This store
// owns only what is genuinely RomM-specific.

import { api } from '$lib/api/endpoints.js';
import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
import { conversion } from '$lib/stores/conversion.svelte.js';
import { ui } from '$lib/stores/ui.svelte.js';
import { registry } from '$lib/tools/registry.js';
import { RulesSource } from '$lib/rules/rulesSource.svelte.js';
import { LatestSaveQueue } from '$lib/stores/latestSaveQueue.js';

/** Weekday labels for the schedule editor; index === Date.getDay() - 1 (Mon=0). */
export const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export const ORDER_LABELS = {
  name: 'Name (A–Z)',
  size_desc: 'Largest first',
  size_asc: 'Smallest first',
  id: 'Oldest added',
  newest: 'Newest added',
};

/**
 * The status's `converter` block (#385): RomM's own converter, or null when
 * this RomM has none. Absent on an older server, which reads as "none".
 */
function readConverter(raw) {
  if (!raw || typeof raw !== 'object') return null;
  return {
    enabled: raw.enabled === true,
    platformFormats: raw.platform_formats ?? {},
    libraryTargets: raw.library_targets && typeof raw.library_targets === 'object'
      ? raw.library_targets
      : null,
    modes: raw.modes ?? {},
    error: raw.error ?? null,
  };
}

/**
 * The status's `swap` block (#390): whether "keep the ROM id when converting"
 * is on, and, when it is, which of its token scopes RomM says are missing.
 * Absent on an older server, which reads as off.
 */
function readSwap(raw) {
  return {
    keepRomId: raw?.keep_rom_id === true,
    missing: Array.isArray(raw?.missing) ? raw.missing : [],
    error: raw?.error ?? null,
  };
}

// The RomM Library's Rules come from RulesSource, shared with folder Libraries;
// what is here is RomM's own: connection, settings, platforms, re-pin.
class RommStore extends RulesSource {
  constructor() {
    super({
      getRules: () => api.getRommRules(),
      saveRules: (rules, checkRules) => api.saveRommRules(rules, checkRules),
      forgetConverted: (platformIds) => api.forgetRommConverted(platformIds),
      preview: (body) => api.previewRommAutoConvert(body),
      run: (body) => api.runRommAutoConvert(body),
      loadError: 'Failed to load automation rules',
    });
  }

  // Connection / configuration
  status = $state(null);
  statusLoading = $state(false);
  statusError = $state(null);

  settings = $state(null);
  settingsSaving = $state(false);
  testResult = $state(null);
  testing = $state(false);

  // Platforms
  platforms = $state([]);
  platformsLoading = $state(false);
  // Monotonic ticket for `loadPlatforms`; see there. Not $state -- nothing
  // renders it, and it changes on every load.
  #platformsTicket = 0;
  // One ticket per read that a settings save re-issues. Every one of these can
  // still be waiting on the OLD instance -- a slow or unreachable URL is
  // exactly why the operator is changing it -- and a late answer overwriting
  // the new one leaves the view describing a server nobody is pointed at.
  // `loadPlatforms` has its own (`#platformsTicket`) because it is also the
  // one that can be superseded mid-flight by `cancelPlatformLoad`.
  #statusTicket = 0;
  #settingsTicket = 0;
  // Settings saves run one at a time in click order and only the latest
  // answer is adopted (#408), as for a folder Library's settings.
  #saveQueue = new LatestSaveQueue();
  #pendingSaves = 0;
  // A save that changed the connection may have been superseded before it
  // could re-derive the status and platforms; the latest save does it for it.
  #identityDirty = false;
  platformsError = $state(null);
  selectedPlatformId = $state(null);

  // Re-pin queue
  repinning = $state(false);

  get configured() {
    return this.status?.configured === true;
  }

  get connected() {
    return this.status?.connected === true;
  }

  /** True only when RomM answers AND its library is actually visible here. */
  get usable() {
    return this.connected && this.status?.libraryRootMounted === true;
  }

  get pendingRepins() {
    return this.status?.pendingRepins ?? 0;
  }

  /**
   * Output extensions that keep RomM's DAT match through a conversion.
   *
   * Served by the backend rather than hardcoded here: which formats RomM can
   * hash is RomM's knowledge, and duplicating the list in the frontend is
   * exactly the second source of truth that goes stale when RomM adds one.
   */
  get datSafeOutputExts() {
    return this.status?.datSafeOutputExts ?? [];
  }

  /** True when *modeEntry* produces a format RomM cannot hash-match itself. */
  losesDatMatch(modeEntry) {
    // Mirrors the backend gate (`romm_repin.mode_needs_repin`) exactly: an
    // extension outside the DAT-safe set loses the match, and that includes
    // the input-mapped modes whose outputExt is null (Z3DS, NSZ) — their
    // outputs are .z3ds / .nsz, which RomM cannot hash either. Guessing "safe"
    // there hid the warning on the two formats that most need it.
    if (!modeEntry || this.datSafeOutputExts.length === 0) return false;
    const ext = (modeEntry.outputExt ?? '').toLowerCase();
    return !this.datSafeOutputExts.includes(ext);
  }

  /**
   * Expected output/input size ratio for a mode, or null when unknown.
   *
   * Served by the backend (SIZE_RATIOS) rather than duplicated here, so the
   * savings estimate and the progress estimator can never disagree.
   */
  sizeRatioFor(mode) {
    const ratio = this.status?.sizeRatios?.[mode];
    return typeof ratio === 'number' ? ratio : null;
  }

  // ─── beside RomM's converter (#385) ──────────────────────────────────

  /**
   * RomM's own converter when it is switched on, else null.
   *
   * The banner, the Rule flags and the can't-convert marks all read this, and
   * all of it comes from the one converter read the status refresh makes. Off,
   * or a RomM from before the converter: nothing is shown.
   */
  get activeConverter() {
    const converter = this.status?.converter;
    return converter?.enabled ? converter : null;
  }

  /**
   * What RomM's converter does to *platform*, for the Rule editor.
   *
   * `{ kind: 'converts', target, modes, agrees }` when RomM converts it:
   * `target` is RomM's format, `modes` this app's modes for the same format,
   * and `agrees` whether *rule* writes it (null with no Rule). `{ kind:
   * 'cannot' }` when RomM cannot convert this Platform at all. null when there
   * is nothing to say. Warnings only: nothing here stops a Rule.
   */
  converterNoteFor(platform, rule) {
    const converter = this.activeConverter;
    const slug = String(platform?.slug ?? '').trim().toLowerCase();
    if (!converter || !slug) return null;
    const target = converter.platformFormats[slug];
    if (target) {
      const modes = converter.modes[slug] ?? [];
      return {
        kind: 'converts',
        target,
        modes,
        agrees: rule?.mode ? modes.includes(rule.mode) : null,
      };
    }
    if (converter.libraryTargets && !(slug in converter.libraryTargets)) {
      return { kind: 'cannot' };
    }
    return null;
  }

  /**
   * Import RomM's converter targets as Rules, for Platforms that have none.
   *
   * The backend plans it (RomM's targets mapped onto this app's modes); the
   * new Rules are added to the ones the editor holds and saved through the
   * Rules API like any edit. A Platform that has a Rule here, saved or not,
   * keeps it. Imported Rules start off, as every new Rule does.
   *
   * @returns {Promise<{created: number, skipped: Array<object>}>}
   */
  async importConverterTargets() {
    const plan = await api.getRommConverterImportPlan();
    await this.loadRules();
    const created = this.addRules(Object.fromEntries(
      Object.entries(plan?.rules ?? {}).map(([platformId, entry]) => [platformId, entry?.mode]),
    ));
    if (created > 0) await this.saveRules();
    return { created, skipped: plan?.skipped ?? [] };
  }

  get selectedPlatform() {
    return this.platforms.find((p) => p.id === this.selectedPlatformId) ?? null;
  }

  // ─── the rules-source contract the Rules view reads (libraries.svelte.js) ──

  imageNouns = ['ROM', 'ROMs'];
  emptyTitle = 'No platforms yet';
  emptyHint = 'Connect RomM in Settings, then your platforms appear here.';
  matchedLabel = 'Only ROMs RomM has identified';
  unmatchedLabel = 'Only ROMs RomM could not identify';

  imageCount(platform) {
    return platform?.rom_count ?? 0;
  }

  /** RomM's master switch for the scheduler, kept in its own settings. */
  get scheduleEnabled() {
    return this.settings?.auto_convert === true;
  }

  async setScheduleEnabled(enabled) {
    await this.saveSettings({ auto_convert: enabled });
  }

  /** Used by an EmulationStation frontend: conversions update gamelist.xml (#386). */
  get esFrontend() {
    return this.settings?.es_frontend === true;
  }

  async setEsFrontend(enabled) {
    await this.saveSettings({ es_frontend: enabled });
  }

  /** The library's ES-DE gamelists folder, or null (#387). */
  get gamelistsFolder() {
    return this.settings?.gamelists_folder || null;
  }

  /** Save it ('' clears); the backend refuses one outside every Volume. */
  async setGamelistsFolder(path) {
    await this.saveSettings({ gamelists_folder: path });
  }

  /** Which modes may run unattended, from /api/romm/status (null: no opinion). */
  get automatableModes() {
    return this.status?.automatableModes ?? null;
  }

  /**
   * A run that queued formats RomM cannot hash-match has just written pending
   * re-pin rows. Without this the header badge keeps its old count and the
   * Re-match action stays hidden until the page is reloaded, which hides the
   * follow-up step from exactly the run that created the need.
   */
  afterRun(result) {
    this.setPendingRepins(result?.pending_repins);
  }

  platformName(platformId) {
    const id = Number(platformId);
    return this.platforms.find((p) => p.id === id)?.name ?? `Platform ${platformId}`;
  }

  // ─── status / settings ────────────────────────────────────────────────

  async loadStatus() {
    const ticket = ++this.#statusTicket;
    this.statusLoading = true;
    this.statusError = null;
    try {
      const data = await api.getRommStatus();
      if (ticket !== this.#statusTicket) return;
      this.status = {
        configured: data?.configured === true,
        connected: data?.connected === true,
        version: data?.version ?? null,
        libraryRoot: data?.library_root ?? '',
        libraryRootMounted: data?.library_root_mounted === true,
        tokenSet: data?.token_set === true,
        datSafeOutputExts: data?.dat_safe_output_exts ?? [],
        sizeRatios: data?.size_ratios ?? {},
        // Which modes may run unattended, from the backend contract. Absent on
        // an older server, which the editor reads as "no opinion" and falls
        // back to its own conservative exclusion.
        automatableModes: Array.isArray(data?.automatable_modes)
          ? data.automatable_modes
          : null,
        pendingRepins: data?.pending_repins ?? 0,
        converter: readConverter(data?.converter),
        swap: readSwap(data?.swap),
        error: data?.error ?? null,
      };
    } catch (e) {
      if (ticket !== this.#statusTicket) return;
      this.statusError = e?.message ?? 'Failed to get RomM status';
      this.status = null;
    } finally {
      // Only the newest read clears the spinner, or a superseded one returning
      // last would report "done" while its replacement is still running.
      if (ticket === this.#statusTicket) this.statusLoading = false;
    }
  }

  async loadSettings() {
    const ticket = ++this.#settingsTicket;
    try {
      const data = await api.getRommSettings();
      if (ticket !== this.#settingsTicket) return this.settings;
      this.settings = data;
    } catch (e) {
      if (ticket !== this.#settingsTicket) return this.settings;
      this.statusError = e?.message ?? 'Failed to load RomM settings';
    }
    return this.settings;
  }

  /**
   * Save RomM's settings. Saves are queued and sent one at a time, in click
   * order, so the backend applies them in that order; only the latest save's
   * answer is adopted, so a slower, older answer never puts older settings on
   * screen. A superseded save still reports its own error to its caller
   * without touching the state. If the latest save fails, the settings are
   * read back, since an earlier save may have landed without being adopted.
   */
  saveSettings(patch) {
    this.#pendingSaves += 1;
    this.settingsSaving = true;
    return this.#saveQueue
      .schedule('settings', (ticket) => this.#saveSettingsNow(patch, ticket))
      .finally(() => {
        this.#pendingSaves -= 1;
        if (this.#pendingSaves === 0) this.settingsSaving = false;
      });
  }

  async #saveSettingsNow(patch, ticket) {
    // Only an identity change invalidates what is on screen. Re-deriving
    // unconditionally meant that toggling automation, or saving a metadata
    // preference, re-listed every platform and re-entered the selected one —
    // whose catalog load stats every ROM in it. On a large remote library
    // that holds the Save action for the catalog scan's multi-minute bound
    // and discards a listing that was still correct, because none of those
    // settings changes which server or which files are being described.
    //
    // Keyed off what the *patch* carried rather than off a diff of the saved
    // settings, because the token is never returned to the browser: a
    // credential change is only ever visible here as a submitted field.
    const identityKeys = ['url', 'library_root', 'token', 'clear_token'];
    const changesIdentity = identityKeys.some((key) => key in (patch ?? {}));
    let saved;
    try {
      saved = await api.saveRommSettings(patch);
    } catch (e) {
      if (this.#saveQueue.isLatest('settings', ticket)) await this.loadSettings();
      throw e;
    }
    if (changesIdentity) this.#identityDirty = true;
    if (!this.#saveQueue.isLatest('settings', ticket)) return this.settings;
    // Supersede any read still waiting on the previous connection before
    // adopting the answer: one of them landing afterwards would replace the
    // settings this save just installed with the ones it replaced.
    this.#settingsTicket += 1;
    this.settings = saved;
    if (!this.#identityDirty) return this.settings;
    this.#identityDirty = false;
    await this.loadStatus();
    if (this.usable) {
      await this.loadPlatforms({ force: true });
    } else {
      // Saving an unreachable URL or an unmounted library leaves nothing to
      // browse, and the previous instance's catalog must not outlive it:
      // returning to the Library tab would show those rows, still selected
      // and still wired to the Convert panel, against a connection that no
      // longer exists. Same cleanup the failed-reload path does.
      this.platforms = [];
      this.selectedPlatformId = null;
      fileBrowser.exitRomm();
    }
    return this.settings;
  }

  async testConnection(patch = {}) {
    this.testing = true;
    this.testResult = null;
    try {
      this.testResult = await api.testRommConnection(patch);
      return this.testResult;
    } finally {
      this.testing = false;
    }
  }

  // ─── platforms ────────────────────────────────────────────────────────

  async loadPlatforms({ force = false } = {}) {
    // `force` supersedes rather than waits. It is what runs after the
    // connection details change, and the load already in flight is asking the
    // OLD instance: bailing here let that answer arrive afterwards and select
    // a platform belonging to a server nobody is pointed at any more.
    if (this.platformsLoading && !force) return;
    // Every load takes a ticket, and only the newest one is allowed to write.
    // Two loads can be in flight at once now, and the slower is not
    // necessarily the older.
    const ticket = ++this.#platformsTicket;
    this.platformsLoading = true;
    this.platformsError = null;
    try {
      const data = await api.getRommPlatforms();
      if (ticket !== this.#platformsTicket) return;
      this.platforms = Array.isArray(data) ? data : [];
      // Re-enter whichever platform is selected, not only on first load: the
      // view clears the catalog on exit, so returning to it with a platform
      // still selected must re-fetch rather than show an empty list.
      const stillThere = this.platforms.some((p) => p.id === this.selectedPlatformId);
      if (this.platforms.length === 0) {
        // A successful call that lists nothing is still a change of world —
        // an instance with no platforms, or one whose library was emptied.
        // Leaving the selection and the previous instance's rows on screen
        // showed "No platforms in RomM" above a live catalog, and with no
        // selected platform to narrow it the target picker fell back to every
        // mode in the registry, wired to those stale paths.
        this.selectedPlatformId = null;
        fileBrowser.exitRomm();
      } else if (force || !stillThere || !fileBrowser.rommPlatformId) {
        const target = stillThere ? this.selectedPlatformId : this.platforms[0].id;
        await this.selectPlatform(target);
      }
    } catch (e) {
      if (ticket !== this.#platformsTicket) return;
      this.platformsError = e?.message ?? 'Failed to list RomM platforms';
      this.platforms = [];
      this.selectedPlatformId = null;
      // Leave no catalog behind. Clearing `platforms` alone left the previous
      // platform's rows on the Library tab, still selected and still wired to
      // the Convert panel — so the user could submit conversions against a
      // server or platform that is no longer the one selected, with the error
      // shown right next to them. Leaving RomM mode also restores the ordinary
      // directory listing, which is a working screen rather than a stale one.
      fileBrowser.exitRomm();
    } finally {
      // Only the newest load clears the flag: a superseded one returning last
      // would otherwise report "done" while its replacement is still running.
      if (ticket === this.#platformsTicket) this.platformsLoading = false;
    }
  }

  /**
   * Abandon whatever platform load is in flight, writing nothing.
   *
   * For leaving the RomM view. The load's tail selects a platform, which
   * calls `fileBrowser.enterRomm()` — so a request still outstanding when the
   * view unmounts would repopulate the browser with catalog rows *after* the
   * cleanup put it back on the ordinary listing, and the workspace would open
   * showing a directory heading above RomM entries. The request itself cannot
   * be recalled; superseding its ticket is what makes its answer inert.
   */
  cancelPlatformLoad() {
    this.#platformsTicket += 1;
    // The abandoned load's `finally` will not clear this — it no longer holds
    // the newest ticket — and leaving it set would make every later unforced
    // load bail as "one is already running".
    this.platformsLoading = false;
  }

  /**
   * True when the selected platform is one nothing installed can convert.
   *
   * `tool_ids`/`mode_ids` absent means "no opinion" (unknown slug, older
   * server) and everything stays offered; an empty array is a decision. The
   * distinction matters because the convert panel has no valid tool to fall
   * back to in the second case, so the view hides it instead.
   */
  get selectedPlatformUnsupported() {
    const platform = this.platforms.find((p) => p.id === this.selectedPlatformId);
    if (!platform) return false;
    return (
      (Array.isArray(platform.tool_ids) && platform.tool_ids.length === 0)
      || (Array.isArray(platform.mode_ids) && platform.mode_ids.length === 0)
    );
  }

  async selectPlatform(platformId) {
    const id = Number(platformId);
    if (!Number.isFinite(id)) return;
    this.selectedPlatformId = id;
    this.#adoptPlatformTool(id);
    await fileBrowser.enterRomm(id);
  }

  /**
   * Point the workspace at a tool this platform can actually use.
   *
   * Narrowing the row's `convertible_by` is only an annotation: the shared
   * submit path deliberately does not gate on it (chdman's extract/copy modes
   * take a `.chd` that is badged convertible by nothing). So a workspace left
   * on CHDMAN from a previous session would happily accept a GameCube `.iso`
   * and produce a CHD — exactly the disambiguation this view exists to remove.
   * Switching the active tool is the enforcement point the picker respects.
   */
  #adoptPlatformTool(platformId) {
    const platform = this.platforms.find((p) => p.id === platformId);
    const allowed = platform?.tool_ids;
    const allowedModes = platform?.mode_ids;
    // No opinion from the backend (unknown slug, older server) changes nothing,
    // matching narrow_to_platform's conservative contract.
    if (!Array.isArray(allowed)) return;
    // An explicitly empty list is the opposite of no opinion: the backend
    // knows this platform and no *installed* tool serves it. There is nothing
    // to adopt, so the workspace keeps whatever it had — which is only safe
    // because the view refuses to render the convert panel for such a
    // platform (`selectedPlatformUnsupported`). Treating [] as "no opinion"
    // left the panel wired to the previous tool, and a PS2 ISO could be
    // submitted to Dolphin RVZ from a platform Dolphin does not serve.
    if (allowed.length === 0) return;

    // The tool being allowed is not enough. Two platforms can both allow the
    // chain tool while allowing *different* chain modes, so moving from
    // GameCube to PS2 would keep `nkit_to_rvz` selected: the picker stops
    // offering it, but the Convert panel would still submit it, and a catalog
    // row with a matching extension would be converted to the other console's
    // format. The mode has to be re-checked, not just its tool.
    const modeAllowed = !Array.isArray(allowedModes)
      || allowedModes.includes(conversion.mode);
    if (allowed.includes(conversion.primaryTool) && modeAllowed) return;

    if (!allowed.includes(conversion.primaryTool)) {
      const next = registry.all().find((t) => allowed.includes(t.id));
      if (next) {
        conversion.setPrimaryTool(next.id);
        ui.workspaceTool = next.id;
      }
    }
    // setPrimaryTool resets to the tool's default mode, which may itself be
    // one this platform disallows; and when the tool did not change, nothing
    // has moved off the stale mode yet. Either way, land on an allowed one.
    if (Array.isArray(allowedModes) && !allowedModes.includes(conversion.mode)) {
      const tool = registry.forTool(conversion.primaryTool);
      const fallback = (tool?.modes ?? []).find((m) => allowedModes.includes(m.mode));
      if (fallback) conversion.setMode(fallback.mode);
    }
  }

  // ─── re-pin queue ─────────────────────────────────────────────────────

  /**
   * Set the badge from the count the backend just measured.
   *
   * Assigned, never added to: `record()` *supersedes* the pending row for a
   * destination rather than stacking one, so re-recording after a failed or
   * redirected conversion leaves the total unchanged. Adding each newly
   * recorded row inflated the badge on every retry, and it stayed wrong until
   * a status reload or a settle pass. Both the plan response and the sweep
   * result carry the authoritative figure, which costs them one COUNT.
   */
  setPendingRepins(pending) {
    if (!this.status || typeof pending !== 'number') return;
    this.status.pendingRepins = Math.max(0, pending);
  }

  /**
   * Settle any conversions RomM has since rescanned.
   *
   * Idempotent server-side, so it is safe to call on every view load; a row
   * RomM has not scanned yet simply stays pending for the next call.
   */
  async runRepin() {
    if (this.repinning) return null;
    this.repinning = true;
    try {
      const result = await api.runRommRepin();
      if (this.status) this.status.pendingRepins = result?.pending ?? 0;
      return result;
    } finally {
      this.repinning = false;
    }
  }
}

export const romm = new RommStore();
