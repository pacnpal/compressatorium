// The Convert panel's store. Auto is the only way to convert: the panel sends
// the selected games and its "Convert to" (`target`: '' for each game's best
// format, a format such as `.chd`, or a mode id) to `/api/jobs/auto-plan`,
// shows the plan, and queues it through `/api/jobs/auto` with the safety
// choices. One queue path, so the RomM metadata snapshot, the plan the user
// saw and what is queued always agree.

import { toast } from 'svelte-sonner';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';
import { formatCompression, parseCompression } from '$lib/tools/compression.js';
import { specsForTarget } from '$lib/tools/convertTargets.js';
import { hashFor } from '$lib/router/routes.js';
import { jobs } from './jobs.svelte.js';
import { jobToasts } from './jobToasts.svelte.js';
import { deleteCheckFor } from '$lib/util/deleteCheck.js';

const PREF_SAVE_DEBOUNCE_MS = 500;
// How long the selection or Convert to must hold still before it is planned.
const PLAN_DEBOUNCE_MS = 400;
// Above this many games the plan is fetched only when Convert is pressed.
export const PLAN_LIVE_LIMIT = 500;
const isBrowser = typeof window !== 'undefined';

// What `untilAborted` settles with when the signal wins.
const ABORTED = Object.freeze({ aborted: true });

// Retire the rows an abandoned RomM snapshot records once it finally returns.
// Only the server rows: no store state, so a newer run is never touched.
function retireLateSnapshot(planning, planned) {
  planning
    .catch(() => {})
    .then(() => {
      const ids = Object.values(planned.recorded_ids).filter((id) => id != null);
      if (ids.length) api.cancelRommRepin(ids).catch(() => {});
    });
}

// Resolves with ABORTED once `signal` aborts (at once if it already has).
function abortedPromise(signal) {
  if (signal.aborted) return Promise.resolve(ABORTED);
  return new Promise((resolve) => {
    signal.addEventListener('abort', () => resolve(ABORTED), { once: true });
  });
}

// Settles with `promise`, or with ABORTED as soon as `signal` aborts,
// whichever comes first. A late rejection of `promise` is swallowed: the
// caller has already moved on, and it must not go unhandled.
function untilAborted(promise, signal) {
  if (!signal) return promise;
  promise.catch(() => {});
  return Promise.race([promise, abortedPromise(signal)]);
}

// How many skipped files a submit toast names before it counts the rest.
const SKIP_REASONS_SHOWN = 3;

function baseName(path) {
  return String(path ?? '').split('::').pop().split(/[\\/]/).pop();
}

function games(n) {
  return n === 1 ? 'game' : 'games';
}

/**
 * The toast description for a batch's skipped files (#504): the first few as
 * "name: reason", then how many more. Null when the backend reported none.
 * @param {{ path: string, reason: string }[]} skipped
 */
function skipReasons(skipped) {
  if (!Array.isArray(skipped) || skipped.length === 0) return null;
  const lines = skipped.slice(0, SKIP_REASONS_SHOWN).map((skip) => {
    const name = baseName(skip?.path);
    return name ? `${name}: ${skip?.reason ?? 'skipped'}` : (skip?.reason ?? 'skipped');
  });
  const rest = skipped.length - SKIP_REASONS_SHOWN;
  if (rest > 0) lines.push(`and ${rest} more`);
  return lines.join('; ');
}

// One toast for a submit: what started converting, and what was left out.
function convertedToast(created, requested, skipped) {
  const view = {
    label: 'View',
    onClick: () => { if (isBrowser) window.location.hash = hashFor('activity', { tab: 'running' }); },
  };
  const left = Array.isArray(skipped) && skipped.length ? skipped : null;
  if (created === 0) {
    toast.warning(`Nothing converted: all ${requested} skipped`, {
      description: skipReasons(left) ?? undefined, duration: 10000,
    });
  } else if (left) {
    toast.success(`Converting ${created}. ${left.length} skipped: ${left[0].reason ?? 'skipped'}`, {
      description: left.length > 1 ? skipReasons(left) : undefined, duration: 10000, action: view,
    });
  } else {
    toast.success(`Converting ${created} ${games(created)}`, { action: view });
  }
}

// The checks a delete can rest on, weakest first.
const CHECK_STRENGTH = ['intact', 'recognized', 'round_tripped'];

class ConversionStore {
  // "Convert to": '' (each game's best format), a format (`.chd`) or a mode id.
  // Session-only: the panel resets it when the selection is cleared.
  target = $state('');
  // Where the new files go: '' writes each next to its original.
  outputDir = $state('');
  split = $state(false);
  // The safety choices, seeded once a session from Settings → Defaults
  // (`seedSafety`): `{ checkAfter, deleteOriginal, quickCheck }`.
  safety = $state(null);
  // Path -> the console the user picked for it this session (Pick console).
  overrides = $state({});

  // The plan for the current selection, and for what (`planKey`).
  plan = $state(null);
  planKey = $state(null);
  planning = $state(false);
  planError = $state(null);

  // How many re-pin rows the last submit recorded, for the caller to surface.
  lastRepinRecorded = $state(0);
  // The backend's own count of rows still waiting, as of the last plan. The
  // badge is set from this rather than added to, because `record()` supersedes
  // the row for a destination instead of stacking one — so a retry records a
  // row without changing the total. Null when nothing was planned.
  lastRepinPending = $state(null);

  deletePlan = $state(null);
  converting = $state(false);
  // The last Convert run's job ids (#595), for its result line in the panel.
  lastRun = $state(null);

  // The Convert panel's own button, `{ count, disabled, busy, run }`, while a
  // panel is on screen; null otherwise. The file list's selection bar offers
  // the same action from it, so both buttons run one submit flow. Raw: `run`
  // is the panel's function and the object is replaced, never edited.
  convertAction = $state.raw(null);

  // Server-saved per-tool compression ({ [toolId]: "<wire value>" }), loaded
  // once on boot and PUT (debounced) on every change. The plan uses it, so a
  // change bumps `prefsVersion` and the panel plans again.
  prefs = $state({});
  prefsVersion = $state(0);
  #touched = {};
  #saveTimer = null;
  // The preference PUTs, one at a time in the order asked for, so an older
  // body can never land after a newer one.
  #saveChain = Promise.resolve();
  // Why the last preference save failed, or null once one lands. Settings →
  // Defaults shows it with Try again; the failure is also toasted, so an
  // edit made in the Convert panel never fails silently either.
  compressionSaveError = $state(null);
  // Bumped by every read of the remembered compression and every local
  // change to it; a read adopts its answer only if nothing came after it, so
  // a late boot read can't put old values over a newer edit.
  #prefsGeneration = 0;
  // The tools whose compression changed here and isn't on the server yet. A
  // save sends only these over the server's current copy, so it never
  // writes back what another tab saved meanwhile.
  #dirtyTools = {};
  // Only the newest delete-plan ask may answer; an older one, landing late or
  // failing late, is dropped (null) instead of replacing or clearing it.
  #deletePlanRequest = 0;

  #planTimer = null;
  #planAbort = null;
  #planResolve = null;
  #planSeq = 0;
  // The modes the last delete plan covered, for the check it rests on.
  #deleteModes = [];

  /** The Convert panel publishes its action here, and null when it goes. */
  publishConvert(action) {
    this.convertAction = action;
  }

  // ─── The Convert-to value ─────────────────────────────────────────────
  /** The mode a mode-id target names, else null (a format or Best). */
  get mode() {
    return this.target && !this.target.startsWith('.') && registry.specFor(this.target)
      ? this.target : null;
  }

  /** The tool of a mode-id target, else null. */
  get currentTool() {
    return this.mode ? registry.toolForMode(this.mode) ?? null : null;
  }

  /** The format Convert to makes (`.chd`): the format itself, or the mode's; null for Best. */
  get targetExt() {
    if (this.target.startsWith('.')) return this.target;
    return this.mode ? registry.specFor(this.mode)?.outputExt ?? null : null;
  }

  chooseTarget(value) {
    const next = value ?? '';
    if (next === this.target) return;
    this.target = next;
    if (!specsForTarget(next).some((s) => s.supportsSplit)) this.split = false;
  }

  /** Remember a console for games detection couldn't place; null forgets it. */
  setOverride(paths, slug) {
    const next = { ...this.overrides };
    for (const path of paths ?? []) {
      if (slug) next[path] = slug;
      else delete next[path];
    }
    this.overrides = next;
  }

  overridesFor(paths) {
    const out = {};
    for (const path of paths ?? []) {
      if (this.overrides[path]) out[path] = this.overrides[path];
    }
    return out;
  }

  /** Seed the safety choices from Settings → Defaults, once a session. */
  seedSafety(values) {
    if (this.safety) return;
    this.safety = {
      checkAfter: values?.verify_after ?? true,
      deleteOriginal: !!values?.delete_on_verify,
      // The quick check only while its opt-out is on (#593).
      quickCheck: !!values?.container_only && !!values?.allow_quick_check,
    };
  }

  setSafety(state) {
    this.safety = { ...state };
  }

  // ─── What a row may be offered for ────────────────────────────────────
  /**
   * Whether a listing row may be converted. A file always may: the plan says
   * which ones it leaves alone and why. A folder only when the backend marked
   * it convertible by a tool with a folder mode (a PS3 disc folder). An
   * archive container only when the Convert-to target takes the archive
   * itself (romz's "Game from 7z/zip").
   */
  allowsInputEntry(entry) {
    if (!entry) return false;
    if (entry.type === 'directory') {
      return Array.isArray(entry.convertible_by)
        && entry.convertible_by.some((id) => (registry.forTool(id)?.modes ?? [])
          .some((m) => Array.isArray(m.inputKinds) && m.inputKinds.includes('directory')));
    }
    if (entry.type === 'archive') return this.allowsInput(entry.path);
    return true;
  }

  /** Whether the Convert-to target reads *path* by name (archives, members). */
  allowsInput(path) {
    if (!path) return false;
    const member = path.includes('::');
    const name = (member ? path.split('::').pop() : path).toLowerCase();
    const specs = specsForTarget(this.target);
    if (specs.length === 0) {
      if (member || !this.#isArchiveName(name)) return true;
      // Best: an archive some mode reads whole (an eXoDOS game zip, a RAR
      // set) is offered, so it can be ticked and its Convert to picked.
      return registry.all().some((tool) => (tool.modes ?? [])
        .some((spec) => (spec.inputExtensions ?? []).some((ext) => name.endsWith(ext))));
    }
    return specs.some((spec) => (!member || spec.allowsArchiveInput)
      && (spec.inputExtensions ?? []).some((ext) => name.endsWith(ext)));
  }

  #isArchiveName(name) {
    return /\.(zip|7z|rar)$/.test(name);
  }

  // ─── Compression, remembered per tool ─────────────────────────────────
  /** Fetch the saved per-tool compression. Best-effort. */
  async loadServerPrefs() {
    const generation = ++this.#prefsGeneration;
    try {
      const prefs = await api.getConversionPrefs();
      if (generation !== this.#prefsGeneration) return;
      if (prefs && typeof prefs === 'object') {
        // A choice made while the fetch was in flight wins.
        this.prefs = { ...prefs, ...this.#touched };
        this.prefsVersion += 1;
      }
    } catch {
      // Keep the tool defaults.
    }
  }

  /** The compression *mode* runs with: its tool's saved choice, else its default. */
  compressionFor(mode) {
    const tool = registry.toolForMode(mode);
    return parseCompression(mode, tool ? this.prefs[tool.id] : null);
  }

  /** Remember *value* as *mode*'s tool's compression, here and on the server. */
  setCompressionFor(mode, value) {
    const tool = registry.toolForMode(mode);
    if (!tool) return;
    this.setPref(tool.id, formatCompression(mode, value));
  }

  /** Save one tool's wire value (null: the tool's own default). */
  setPref(toolId, wire) {
    this.#prefsGeneration += 1;
    const next = { ...this.prefs };
    if (wire == null) delete next[toolId];
    else next[toolId] = wire;
    this.prefs = next;
    this.#touched = { ...this.#touched, [toolId]: wire ?? undefined };
    this.#dirtyTools = { ...this.#dirtyTools, [toolId]: true };
    this.prefsVersion += 1;
    this.#schedulePrefsSave();
  }

  /**
   * Settings → Defaults saved *toolId*'s compression (*wire*, or null for the
   * tool's own default): it becomes the live choice the plan uses, even over
   * an edit made in the Convert panel, and is saved.
   */
  applySavedDefault(toolId, wire) {
    this.setPref(toolId, wire);
  }

  // The debounce lives here, in the store, so leaving the screen that made
  // the change (Settings, the Convert panel) never drops it.
  #schedulePrefsSave() {
    if (!isBrowser) return;
    if (this.#saveTimer) clearTimeout(this.#saveTimer);
    this.#saveTimer = setTimeout(() => {
      this.#saveTimer = null;
      this.#putPrefs();
    }, PREF_SAVE_DEBOUNCE_MS);
  }

  #putPrefs() {
    this.#saveChain = this.#saveChain.then(async () => {
      const tools = Object.keys(this.#dirtyTools);
      if (!tools.length) return;
      const local = { ...this.prefs };
      try {
        // The PUT replaces the whole object: start from the server's copy as
        // it is now and change only the tools edited here. A tool set back to
        // its own default (null) drops its key.
        const body = { ...((await api.getConversionPrefs()) ?? {}) };
        for (const tool of tools) {
          if (local[tool] == null) delete body[tool];
          else body[tool] = local[tool];
        }
        await api.putConversionPrefs(body);
        // Saved, unless it changed again while this save was out.
        const dirty = { ...this.#dirtyTools };
        for (const tool of tools) {
          if (this.prefs[tool] === local[tool]) delete dirty[tool];
        }
        this.#dirtyTools = dirty;
        this.compressionSaveError = null;
      } catch (e) {
        this.compressionSaveError = e?.message ?? 'Request failed';
        toast.error(`Couldn't save the compression: ${this.compressionSaveError}`);
      }
    });
    return this.#saveChain;
  }

  /** Send the remembered compression again, after a save failed. */
  retryCompressionSave() {
    if (this.#saveTimer) clearTimeout(this.#saveTimer);
    this.#saveTimer = null;
    return this.#putPrefs();
  }

  /** Send a debounced preference now; resolves once every save so far has landed. */
  flushCompressionPrefs() {
    if (this.#saveTimer) {
      clearTimeout(this.#saveTimer);
      this.#saveTimer = null;
      this.#putPrefs();
    }
    return this.#saveChain;
  }

  // ─── The plan ─────────────────────────────────────────────────────────
  /** What a plan of *paths* depends on, so a stale plan is never shown as current. */
  keyFor(paths) {
    return JSON.stringify([
      paths ?? [], this.target, this.outputDir, this.overridesFor(paths), this.prefsVersion,
    ]);
  }

  /** True when the plan on hand is for *paths* as they are set now. */
  hasPlanFor(paths) {
    return !!this.plan && this.planKey === this.keyFor(paths);
  }

  /**
   * Plan *paths* after `delay` ms, aborting any plan still pending or in
   * flight. Resolves with the plan, or null when it was superseded, cancelled
   * or failed (`planError` then says why).
   */
  requestPlan(paths, { delay = PLAN_DEBOUNCE_MS } = {}) {
    this.cancelPlan();
    if (!paths?.length) {
      this.plan = null;
      this.planKey = null;
      this.planError = null;
      return Promise.resolve(null);
    }
    const seq = ++this.#planSeq;
    const snapshot = [...paths];
    const key = this.keyFor(snapshot);
    this.planning = true;
    this.planError = null;
    return new Promise((resolve) => {
      this.#planResolve = resolve;
      const ask = { seq, snapshot, key, resolve };
      if (delay > 0 && isBrowser) {
        this.#planTimer = setTimeout(this.#runPlan.bind(this), delay, ask);
      } else {
        this.#runPlan(ask);
      }
    });
  }

  /** One plan request, answered only while it is still the newest. */
  async #runPlan({ seq, snapshot, key, resolve }) {
    this.#planTimer = null;
    const controller = new AbortController();
    this.#planAbort = controller;
    try {
      // The route plans with the saved compression: a change still inside
      // its debounce is sent first.
      await this.flushCompressionPrefs();
      if (seq !== this.#planSeq) return;
      const plan = await api.getAutoPlan(snapshot, this.outputDir || null, {
        signal: controller.signal,
        target: this.target || null,
        platformOverrides: this.overridesFor(snapshot),
      });
      if (seq !== this.#planSeq) return;
      this.plan = Array.isArray(plan) ? plan : [];
      this.planKey = key;
      resolve(this.plan);
    } catch (e) {
      if (seq !== this.#planSeq) return;
      this.plan = null;
      this.planKey = null;
      this.planError = e?.message ?? "Couldn't plan this";
      resolve(null);
    } finally {
      if (seq === this.#planSeq) {
        this.planning = false;
        this.#planAbort = null;
        this.#planResolve = null;
      }
    }
  }

  /** Stop a pending or running plan request; its promise resolves null. */
  cancelPlan() {
    this.#planSeq += 1;
    if (this.#planTimer) clearTimeout(this.#planTimer);
    this.#planTimer = null;
    this.#planAbort?.abort();
    this.#planAbort = null;
    this.#planResolve?.(null);
    this.#planResolve = null;
    this.planning = false;
  }

  clearPlan() {
    this.cancelPlan();
    this.plan = null;
    this.planKey = null;
    this.planError = null;
  }

  // ─── The delete preview ───────────────────────────────────────────────
  /**
   * The files each group's delete would remove, as one plan: `groups` maps a
   * mode to its sources (only modes that delete). Sets and returns `deletePlan`.
   * Only the newest ask answers: an older one, landing or failing late,
   * resolves null and changes nothing.
   */
  async fetchDeletePlan(groups, outputDir = this.outputDir || null) {
    const entries = Object.entries(groups ?? {}).filter(([, paths]) => paths?.length);
    if (entries.length === 0) return null;
    const request = ++this.#deletePlanRequest;
    try {
      const parts = await Promise.all(
        entries.map(([mode, paths]) => api.getDeletePlan(paths, mode, outputDir, 'skip')),
      );
      if (request !== this.#deletePlanRequest) return null;
      this.#deleteModes = entries.map(([mode]) => mode);
      this.deletePlan = {
        items: parts.flatMap((p) => p?.items ?? []),
        total_delete_count: parts.reduce((n, p) => n + (p?.total_delete_count ?? 0), 0),
        blocked: parts.some((p) => p?.blocked),
        disallowed_archives: parts.flatMap((p) => p?.disallowed_archives ?? []),
      };
      return this.deletePlan;
    } catch (e) {
      if (request !== this.#deletePlanRequest) return null;
      toast.error(e?.message ?? "Couldn't work out what would be deleted");
      this.deletePlan = null;
      throw e;
    }
  }

  clearDeletePlan() {
    this.deletePlan = null;
  }

  /** The weakest check any of the delete plan's modes will pass first. */
  get deleteCheck() {
    const quick = !!this.safety?.quickCheck;
    const checks = this.#deleteModes.map((mode) => {
      const spec = registry.specFor(mode) ?? {};
      return deleteCheckFor({
        supportsRoundTrip: !!spec.supportsRoundTrip,
        containerOnly: quick,
        deleteProof: spec.deleteProof ?? 'round_trip',
        roundTripRequired: !!spec.roundTripRequired,
      });
    });
    return CHECK_STRENGTH.find((c) => checks.includes(c)) ?? 'round_tripped';
  }

  // ─── Submission ───────────────────────────────────────────────────────
  /**
   * Queue the confirmed plan through `/api/jobs/auto`.
   *
   * `plan` is the plan the user saw: each row's `mode` is the target its game
   * is queued for, and a row it skipped is never sent. The RomM metadata
   * snapshot and its reconciliation run once per target mode. `outputDir` is
   * the folder the plan was made for; `options` the safety choices
   * (`{ verifyAfter, deleteOnVerify, containerOnly, split }`), sent with the
   * plan's own `target` and console overrides so the server replans the
   * same. `signal` is checked again after the RomM snapshot, right before
   * queueing: an abort there queues nothing and retires the metadata the
   * snapshot recorded.
   */
  async submitAuto(
    filePaths,
    {
      plan = [], rommRepin = false, rommPlatformId = null,
      outputDir = this.outputDir || null, signal = null, options = {},
    } = {},
  ) {
    if (!filePaths?.length || signal?.aborted) return null;
    const wanted = new Set(filePaths);
    // Only the confirmed plan's actionable rows go to the server, each with the
    // target the user saw. The server replans them and leaves out (and
    // reports) any whose target changed, so nothing is queued unconfirmed.
    const items = (plan ?? [])
      .filter((row) => row?.mode && wanted.has(row.file_path))
      .map((row) => ({ file_path: row.file_path, mode: row.mode }));
    if (!items.length) {
      toast.warning('Nothing to convert: every selected game is skipped');
      return null;
    }
    const sent = {
      target: this.target || null,
      platformOverrides: this.overridesFor(items.map((item) => item.file_path)),
      ...options,
    };
    const modeOf = Object.fromEntries(items.map((item) => [item.file_path, item.mode]));
    return this.#queueWithRepin(
      items.map((item) => item.file_path),
      groupByMode(Object.keys(modeOf), modeOf),
      { duplicateAction: 'skip', rommRepin, rommPlatformId, outputDir, signal },
      async () => {
        // After the RomM snapshot, right before queueing: an abort (the
        // panel unmounted) stops here. Nothing becomes a job, so the shared
        // path retires the metadata just recorded, without an error toast.
        if (signal?.aborted) {
          const stopped = new Error('Auto run aborted');
          stopped.aborted = true;
          throw stopped;
        }
        // The route queues with the saved compression: send a change still
        // inside its debounce first, and stop if it didn't save (the choice
        // stays, with Try again in Settings).
        await this.flushCompressionPrefs();
        // Aborted while the save went out (the panel closed): stop here too.
        if (signal?.aborted) {
          const stopped = new Error('Auto run aborted');
          stopped.aborted = true;
          throw stopped;
        }
        if (this.compressionSaveError) {
          throw new Error(`Couldn't save the compression: ${this.compressionSaveError}`);
        }
        let result;
        try {
          result = await api.createAutoJobs(
            items, outputDir, 'skip', signal ? { ...sent, signal } : sent,
          );
        } catch (e) {
          if (!signal?.aborted) throw e;
          // Aborted with the request already out: the server may have made
          // the jobs, so their RomM rows are kept (one with no job behind it
          // ages out on its own) rather than retired under a running job.
          const stopped = new Error('Auto run aborted');
          stopped.aborted = true;
          stopped.keepRecorded = true;
          throw stopped;
        }
        const createdJobs = Array.isArray(result?.jobs) ? result.jobs : [];
        jobs.adopt(createdJobs);
        const ids = createdJobs.map((job) => job?.id).filter((id) => id != null);
        if (ids.length) {
          this.lastRun = { ids };
          jobToasts.trackRun(ids);
        }
        // Each game left out and why (a plan that changed, an output another
        // job holds), for the shared result toast (#520).
        return { result, createdJobs, skipped: result?.skipped ?? [] };
      },
    );
  }

  /**
   * Record RomM metadata for each (mode -> sources) group. Results merge into
   * one `{ recorded, pending, recorded_paths, recorded_ids, generation }`, the
   * shape one `planRommRepin` call returns. `misdirected` re-records sources
   * the queue wrote somewhere other than planned.
   *
   * Ordering never rests on timing. The server orders snapshots by the
   * generation a plan was given when it started, and will not let an older
   * one supersede a newer run's row. Every group after the first carries the
   * generation the first was given, so the whole run stays as old as it is,
   * however late a group is sent. And once `signal` aborts no further group
   * is sent: the request already in flight finishes on its own, and the
   * caller retires what it recorded.
   */
  async #planRepin(
    groups, { duplicateAction, rommPlatformId, outputDir }, misdirected, merged, signal = null,
  ) {
    // `merged` belongs to the caller and is filled one group at a time, so the
    // rows earlier groups saved are still there when a later group's request
    // throws: they are real records on the server, and the caller has to
    // reconcile or retire them like any other.
    for (const [mode, paths] of Object.entries(groups)) {
      if (signal?.aborted) return;
      if (!paths.length) continue;
      const args = [paths, mode, outputDir, duplicateAction, rommPlatformId];
      const outputPaths = misdirected
        ? Object.fromEntries(paths.map((p) => [p, misdirected[p]]))
        : null;
      // Trailing arguments only when set, so a run's first request is the
      // plain five-argument call.
      if (outputPaths || merged.generation != null) args.push(outputPaths);
      if (merged.generation != null) args.push(merged.generation);
      const planned = await api.planRommRepin(...args);
      merged.recorded += planned?.recorded ?? 0;
      if (typeof planned?.pending === 'number') merged.pending = planned.pending;
      if (merged.generation == null && typeof planned?.generation === 'number') {
        merged.generation = planned.generation;
      }
      Object.assign(merged.recorded_paths, planned?.recorded_paths ?? {});
      Object.assign(merged.recorded_ids, planned?.recorded_ids ?? {});
    }
  }

  /**
   * The submission path `submitAuto()` runs: the RomM snapshot, the queue, the reconciliation.
   *
   * @param {string[]} filePaths every path the user asked for
   * @param {Record<string, string[]>} groups target mode -> its sources
   * @param {{ duplicateAction: string, rommRepin: boolean, rommPlatformId: any }} opts
   *   `rommRepin` records each source's RomM metadata before the jobs are
   *   queued, so a format RomM cannot hash-match (RVZ/CSO/NSZ/WUX/Z3DS) can be
   *   re-identified after its rescan. It must happen BEFORE the conversion:
   *   the provider ids are read from the RomM record for the source file, and
   *   that record is what goes stale once the source is converted or deleted.
   * @param {() => Promise<{ result: any, createdJobs: object[] }>} queue
   */
  async #queueWithRepin(filePaths, groups, opts, queue) {
    const { duplicateAction, rommRepin, rommPlatformId } = opts;
    // The output folder the caller captured before its first await; every
    // snapshot (and replan) records paths under the folder the queue writes.
    const target = {
      duplicateAction, rommPlatformId,
      outputDir: opts.outputDir === undefined ? this.outputDir || null : opts.outputDir,
    };
    const modeOf = {};
    for (const [mode, paths] of Object.entries(groups)) {
      for (const path of paths) modeOf[path] = mode;
    }
    this.converting = true;
    this.lastRepinRecorded = 0;
    this.lastRepinPending = null;
    // The metadata snapshot is taken before the batch is submitted, so any
    // path the batch does not end up queueing — a rejected submit, or one the
    // backend filters out during per-file validation — leaves a row describing
    // a conversion that will never run. Source path -> recorded destination.
    let recorded = {};
    // Source path -> the id of the row recorded for it. Retiring is keyed by
    // id, not by destination: `record()` supersedes, so between this plan and
    // its cancel another client can own the row that path now holds, and
    // cancelling by path would retire its live conversion's metadata.
    let recordedIds = {};
    try {
      if (rommRepin) {
        const planned = emptyRepinPlan();
        try {
          // The duplicate policy goes with it: the plan has to record the
          // path the batch will actually write. Under Rename the batch resolves
          // to `Game_1.rvz`, so recording the occupied base path would re-pin
          // the OLD file and leave the new one unidentified; under Skip the
          // conflicting sources are never queued at all.
          // The platform goes with it: the backend would otherwise walk every
          // platform's full catalog looking for these paths, which on a large
          // instance is hundreds of serialised requests before a small batch
          // is even queued. These rows all came from one platform's listing.
          //
          // Abort-aware: a stalled RomM must not hold the run (and Start) once
          // the caller aborts. The snapshot keeps going on its own; whatever
          // it records is retired when it returns, apart from this run's
          // state, so a late answer never touches a newer run.
          const planning = this.#planRepin(groups, target, null, planned, opts.signal);
          const outcome = await untilAborted(planning, opts.signal);
          if (outcome?.aborted) {
            retireLateSnapshot(planning, planned);
            const stopped = new Error('Auto run aborted');
            stopped.aborted = true;
            throw stopped;
          }
        } catch (e) {
          if (e?.aborted) throw e;
          // Best-effort, like the disc-ID tagging hook: losing the metadata
          // snapshot costs a re-match in RomM, while refusing to convert costs
          // the user the thing they actually asked for.
          toast.warning(
            `Could not save RomM metadata (${e?.message ?? 'unknown error'}); `
            + 'converted files may need re-matching in RomM',
          );
        }
        // Whatever was saved, including rows from groups before a failed one,
        // goes through the same reconciliation and `finally` retirement.
        // Reported back to the caller rather than pushed into the RomM store
        // from here: conversion is imported by fileBrowser, which the RomM
        // store imports, so a static import back would be a cycle.
        this.lastRepinRecorded = planned.recorded;
        this.lastRepinPending = planned.pending;
        recorded = planned.recorded_paths;
        recordedIds = planned.recorded_ids;
      }
      // A queue step that failed but still created jobs (they carry
      // `createdJobs`) is reconciled like a success, then its error is
      // rethrown: only snapshots with no job behind them are retired.
      let queued;
      try {
        queued = await queue();
      } catch (e) {
        if (e?.keepRecorded) {
          recorded = {};
          recordedIds = {};
        }
        if (!Array.isArray(e?.createdJobs)) throw e;
        queued = { result: null, createdJobs: e.createdJobs, error: e };
      }
      const { result, createdJobs, error, skipped = [] } = queued;
      // Report what the backend actually created. createBatch can
      // legitimately return fewer jobs than requested when the user
      // chose duplicateAction: 'skip', or when create_batch_jobs
      // rejects inputs during per-file validation, or [] when
      // everything was filtered out. Telling the user "Queued N" when
      // N rows were skipped is misleading.
      const created = createdJobs.length;
      const requested = filePaths.length;
      // On a partial failure the error toast below says what happened.
      if (!error) convertedToast(created, requested, skipped);
      // Reconcile the planned rows against what the queue actually did.
      //
      // Planning resolves a destination by predicting the duplicate policy's
      // answer, and between predicting and queueing the prediction can go
      // stale: another job or an outside process takes the path Rename picked,
      // so the batch writes somewhere else. A row left pointing at the
      // predicted path would then be settled against whatever landed there —
      // this ROM's identity stamped on an unrelated file. So compare against
      // each job's real `output_path`, not just its source.
      const created_jobs = createdJobs.filter(Boolean);
      // The queue reports each job's source as the backend resolved it, which
      // is the symlink-free path; the row was recorded against the path the
      // browser submitted. For a library reached through a symlinked ancestor
      // those differ, and matching on the submitted spelling alone would find
      // no job for a source that was queued — retiring a row the conversion
      // still needs. A destination that some job is writing means the row is
      // live whatever the source is spelled like.
      const actualBySource = new Map(
        created_jobs.map((j) => [j.file_path, j.output_path]),
      );
      const queuedDestinations = new Set(
        created_jobs.map((j) => j.output_path).filter(Boolean),
      );
      const misdirected = {};
      for (const [source, planned] of Object.entries(recorded)) {
        const actual = actualBySource.get(source);
        if (!actual) continue;
        if (actual !== planned) misdirected[source] = actual;
      }
      if (Object.keys(misdirected).length) {
        // Re-record first, retire second. The old row is only wrong once the
        // new one exists: if the re-record fails — RomM went away between the
        // first plan and now — cancelling first would leave the conversion
        // running with no snapshot at all, and the operator none the wiser.
        // Losing this ordering is how metadata disappears silently.
        // Filled group by group: a later group failing leaves the earlier
        // groups' new rows here, and they point at the outputs the queue
        // really wrote, so the incomplete branch below keeps them.
        const replanned = emptyRepinPlan();
        let replanFailed = false;
        try {
          await this.#planRepin(
            groupByMode(Object.keys(misdirected), modeOf),
            target, misdirected, replanned,
          );
        } catch (e) {
          replanFailed = true;
          // Say so rather than swallow it: the conversion is already queued,
          // so this metadata now needs the manual path. The count is corrected
          // below so the badge does not promise a re-match that is not coming.
          toast.warning(
            `Metadata could not be saved for ${Object.keys(misdirected).length} `
            + `file(s) the queue redirected: ${e?.message ?? 'the request failed'}. `
            + 'Re-match those in RomM by hand after converting.',
          );
        }
        // Truthy is not enough: the plan endpoint skips a path it cannot
        // record (RomM no longer lists it, the destination is outside the
        // volumes) and still answers 200, so a partial result would retire
        // the old rows and report metadata that was never saved. Every
        // redirected source has to come back mapped to the path the queue
        // actually chose.
        const replannedPaths = replanned.recorded_paths;
        const replannedIds = replanned.recorded_ids;
        const replanComplete = Object.entries(misdirected).every(
          ([source, destination]) => replannedPaths[source] === destination,
        );
        // A failed request already said so above; this covers a 200 that
        // left some sources out.
        if (!replanFailed && !replanComplete) {
          toast.warning(
            'Metadata could not be saved for every file the queue redirected. '
            + 'Re-match those in RomM by hand after converting.',
          );
        }
        if (replanComplete) {
          await api
            .cancelRommRepin(
              Object.keys(misdirected)
                .map((k) => recordedIds[k])
                .filter((id) => id != null),
            )
            .catch(() => {});
          for (const [source, actual] of Object.entries(misdirected)) {
            recorded[source] = actual;
            recordedIds[source] = replannedIds[source];
          }
        } else {
          // Retire the old rows anyway. They point at a path this batch is no
          // longer writing — under Overwrite that is the file the conversion
          // was going to replace — and a row aimed at the wrong file is worse
          // than no row at all. The `finally` block cannot do it: these
          // sources did become jobs, so they are filtered out of `recorded`
          // below and would be left behind.
          await api
            .cancelRommRepin(
              Object.keys(misdirected)
                .map((k) => recordedIds[k])
                .filter((id) => id != null),
            )
            .catch(() => {});
          for (const source of Object.keys(misdirected)) {
            delete recorded[source];
            delete recordedIds[source];
          }
          // Only the ones that really were not re-recorded: a partial answer
          // still saved metadata for the sources it mapped, and counting
          // those as lost would under-report just as misleadingly.
          const lost = Object.entries(misdirected).filter(
            ([source, destination]) => replannedPaths[source] !== destination,
          ).length;
          this.lastRepinRecorded = Math.max(0, this.lastRepinRecorded - lost);
        }
      }
      // What is left in `recorded` after this is the set the `finally` block
      // retires: rows whose source did NOT become a job at all.
      //
      // Retire by destination, but decide by destination too: two sources can
      // resolve to one output (duplicate basenames landing in a single output
      // folder), the batch collapses them into one job, and cancelling on
      // behalf of the source that lost would delete the row the winner needs.
      // A destination any queued source claims is never retired.
      const queuedSources = new Set(created_jobs.map((job) => job.file_path));
      const claimed = new Set(
        Object.entries(recorded)
          .filter(([source]) => queuedSources.has(source))
          .map(([, destination]) => destination),
      );
      recorded = Object.fromEntries(
        Object.entries(recorded).filter(
          ([source, destination]) =>
            !queuedSources.has(source)
            && !claimed.has(destination)
            // ...and not a row whose destination a job is writing under a
            // source spelled differently (a symlinked ancestor, resolved by
            // the backend). Retiring that row loses the metadata for a
            // conversion that is running.
            && !queuedDestinations.has(destination),
        ),
      );
      // Report only the rows that survive, or the toast would claim metadata
      // was saved for conversions that are not happening.
      this.lastRepinRecorded = Math.max(
        0, this.lastRepinRecorded - Object.keys(recorded).length,
      );
      if (error) throw error;
      return result;
    } catch (e) {
      // An aborted run is not a failure the user needs told about.
      if (!e?.aborted) toast.error(e?.message ?? 'Failed to create jobs');
      throw e;
    } finally {
      this.converting = false;
      // Retire the rows for everything that did not become a job. They are
      // harmless if this fails — a row whose output never changes is never
      // settled and ages out on its own — so it must not mask a real error.
      // By id: `recorded` decides *which* rows (its keys are the sources that
      // did not become jobs), `recordedIds` names them.
      const orphaned = Object.keys(recorded)
        .map((source) => recordedIds[source])
        .filter((id) => id != null);
      if (orphaned.length) {
        // Awaited for its *count*, not for its success. `lastRepinPending` was
        // measured by the plan call, before these rows were retired, so leaving
        // it there showed the pre-reconciliation backlog on the badge — and a
        // fire-and-forget retirement can never correct it, so it stayed wrong
        // until a status reload or a settle pass. The reply carries the
        // authoritative count; a failure leaves the old one, which is the
        // existing best-effort behaviour and still ages out on its own.
        try {
          const reconciled = await api.cancelRommRepin(orphaned);
          if (typeof reconciled?.pending === 'number') {
            this.lastRepinPending = reconciled.pending;
          }
        } catch (_e) {
          // Harmless: a row whose output never changes is never settled.
        }
      }
    }
  }
}

/**
 * An empty accumulator in the shape one `planRommRepin` call returns. A new
 * one per snapshot: a fresh `generation`, so a redirected source's re-record
 * is planned as of now rather than as of the run's start.
 */
function emptyRepinPlan() {
  return { recorded: 0, pending: null, recorded_paths: {}, recorded_ids: {}, generation: null };
}

/** Sources grouped by their target mode: `{ mode: [path, ...] }`. */
function groupByMode(paths, modeOf) {
  const groups = {};
  for (const path of paths) {
    const mode = modeOf[path];
    if (!mode) continue;
    if (!groups[mode]) groups[mode] = [];
    groups[mode].push(path);
  }
  return groups;
}

export const conversion = new ConversionStore();
