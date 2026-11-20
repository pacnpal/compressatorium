// Job queue store, owns the single /api/jobs/events SSE connection (auto-
// reconnecting). All job mutations come through here so components see a
// single source of truth.

import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { STORAGE_KEYS, readBool, writeBool } from '$lib/util/localStorage.js';
import { verification } from './verification.svelte.js';
import { ui } from './ui.svelte.js';
import {
  ACTIVE_STATUSES,
  EXTERNAL_SCAN_MODES,
  TERMINAL_STATUSES,
} from './jobConstants.js';

const MAX_OPTIMISTIC_PLACEHOLDERS = 100;
// How many eviction tombstones the jobs store keeps (newest win).
const EVICTED_TOMBSTONES = 2000;

/**
 * Whether an HTTP snapshot of a job we already hold is newer than our copy,
 * so it should replace it. Shared by refresh() and adopt().
 *
 * - Terminal is final on the backend: an active snapshot for an already
 *   terminal local job is stale. Keep ours.
 * - A terminal snapshot for a job we hold as active heals a missed SSE
 *   terminal event (network blip, tab backgrounded). Take it.
 * - Both active: take a queued -> processing transition or a progress bump
 *   on the same status. A job picked up as `queued` from a poll never gets a
 *   PROCESSING frame if our stream wasn't open when it moved, so without
 *   this the row would sit at queued forever.
 * - Otherwise (both terminal, or no useful progression) the SSE-derived
 *   state is at least as fresh. Keep ours.
 */
function snapshotIsNewer(existing, incoming) {
  const existingTerminal = TERMINAL_STATUSES.has(existing.status);
  const incomingTerminal = TERMINAL_STATUSES.has(incoming.status);
  if (existingTerminal) return false;
  if (incomingTerminal) return true;
  const queuedToProcessing = existing.status === 'queued' && incoming.status === 'processing';
  const progressBump = existing.status === incoming.status
    && (incoming.progress ?? 0) > (existing.progress ?? 0);
  return queuedToProcessing || progressBump;
}

class JobsStore {
  jobs = $state([]);
  creatingJobs = $state([]);
  hiddenIds = new SvelteSet();
  tab = $state('queue');
  pageSize = $state(50);
  page = $state(1);
  showExternalScanJobs = $state(readBool(STORAGE_KEYS.SHOW_METADATA_JOBS, false));

  cancellingAll = $state(false);
  clearingCompleted = $state(false);
  loading = $state(false);

  // index for O(1) lookup; not a $state because it's derived from this.jobs
  _byId = new SvelteMap();
  // Tombstones: ids of finished jobs the backend no longer lists (cleared,
  // or past the history cap), newest last, at most EVICTED_TOMBSTONES. An HTTP
  // response built before the prune (Auto's adopt) must not bring such a job
  // back as an unknown queued row. A plain id->true map, like refresh().
  _evictedIds = Object.create(null);
  _evictedOrder = [];
  _unsubscribe = null;
  _pollTimer = null;

  get activeCount() {
    return this.jobs.reduce((n, j) => (ACTIVE_STATUSES.has(j.status) ? n + 1 : n), 0);
  }

  get hasActive() {
    return this.activeCount > 0;
  }

  get queuedCount() {
    return this.jobs.reduce((n, j) => (j.status === 'queued' ? n + 1 : n), 0);
  }

  get processingCount() {
    return this.jobs.reduce((n, j) => (j.status === 'processing' ? n + 1 : n), 0);
  }

  // Counts of the jobs the list holds. The backend keeps the newest
  // MAX_JOB_HISTORY finished jobs; the all-time totals are the Ledger's
  // (`ledger.totals`, #533).
  get completedCount() {
    return this.jobs.reduce((n, j) => (j.status === 'completed' ? n + 1 : n), 0);
  }

  get failedCount() {
    return this.jobs.reduce((n, j) => (j.status === 'failed' ? n + 1 : n), 0);
  }

  get cancelledCount() {
    return this.jobs.reduce((n, j) => (j.status === 'cancelled' ? n + 1 : n), 0);
  }

  /**
   * Counts scoped to the same predicate as `visibleJobs`, i.e. they
   * exclude jobs the user has locally hidden and (when the metadata
   * toggle is off) external-scan modes. JobsPanel tab badges + the
   * Cancel-all / Clear-all gating use these so a hidden DAT-match or
   * metadata-scan can't drive a count > 0 while the row list is
   * empty, and so destructive actions never operate on jobs the
   * user explicitly chose to hide.
   */
  _matchesTabFilter(job, tab) {
    if (this.hiddenIds.has(job.id)) return false;
    if (!this.showExternalScanJobs && EXTERNAL_SCAN_MODES.has(job.mode)) return false;
    switch (tab) {
      case 'queue':
        return ACTIVE_STATUSES.has(job.status);
      case 'completed':
        return job.status === 'completed';
      case 'failed':
        return job.status === 'failed' || job.status === 'cancelled';
      default:
        return true;
    }
  }

  get visibleQueuedCount() {
    return this.jobs.reduce(
      (n, j) => (this._matchesTabFilter(j, 'queue') ? n + 1 : n),
      0,
    );
  }

  get visibleCompletedCount() {
    return this.jobs.reduce((n, j) => (this._matchesTabFilter(j, 'completed') ? n + 1 : n), 0);
  }

  get visibleFailedCount() {
    return this.jobs.reduce((n, j) => (this._matchesTabFilter(j, 'failed') ? n + 1 : n), 0);
  }

  /** Filter by current `tab` + external-scan toggle + locally hidden ids. */
  get visibleJobs() {
    const filtered = this.jobs.filter((job) => {
      if (this.hiddenIds.has(job.id)) return false;
      if (!this.showExternalScanJobs && EXTERNAL_SCAN_MODES.has(job.mode)) return false;
      switch (this.tab) {
        case 'queue':
          return ACTIVE_STATUSES.has(job.status);
        case 'completed':
          return job.status === 'completed';
        case 'failed':
          return job.status === 'failed' || job.status === 'cancelled';
        default:
          return true;
      }
    });
    return filtered;
  }

  get pageCount() {
    return Math.max(1, Math.ceil(this.visibleJobs.length / this.pageSize));
  }

  /** Slice of `visibleJobs` for the current page. */
  get pageJobs() {
    const start = (this.page - 1) * this.pageSize;
    return this.visibleJobs.slice(start, start + this.pageSize);
  }

  setTab(tab) {
    this.tab = tab;
    this.page = 1;
  }

  setShowExternalScanJobs(value) {
    this.showExternalScanJobs = !!value;
    writeBool(STORAGE_KEYS.SHOW_METADATA_JOBS, this.showExternalScanJobs);
  }

  hideLocally(jobId) {
    this.hiddenIds.add(jobId);
  }

  // ─── Mutations ────────────────────────────────────────────────────────
  /**
   * Whether `id` names a job the backend has deleted (cleared or evicted)
   * that we no longer hold. The one ordering rule for every way a job
   * arrives — an SSE frame, a REST snapshot, an Auto response: any copy of a
   * deleted job was built before its deletion, so it is stale however late
   * it lands, and must not bring the row back. Job ids are never reused.
   */
  _isDeleted(id) {
    return !this._byId.has(id) && Boolean(this._evictedIds[id]);
  }

  _applyJob(job) {
    if (!job?.id || this._isDeleted(job.id)) return;
    const existing = this._byId.get(job.id);
    if (existing) {
      // Replace the slot via index assignment rather than Object.assign on
      // the existing reference. Svelte 5's deep-proxy tracks $state arrays
      // at index granularity, and consumers iterating `jobs` (via {#each})
      // re-render on slot replacement, but Object.assign on an outside
      // reference may not propagate when the reference is also stored in
      // _byId, leading to stale progress/status until an unrelated change.
      const idx = this.jobs.findIndex((j) => j.id === job.id);
      if (idx !== -1) {
        this.jobs[idx] = job;
      } else {
        this.jobs.push(job);
      }
      this._byId.set(job.id, job);
    } else {
      this.jobs.push(job);
      this._byId.set(job.id, job);
      if (job.file_path) {
        // First arrival only: clear exactly one matching optimistic
        // placeholder (the oldest). _applyJob also runs on every later SSE
        // progress/status update for an already-known job; re-clearing then
        // would drop a placeholder belonging to a *second* in-flight
        // same-file/same-mode submission before its own job arrives — the
        // under-count this guards against. See _removeOptimistic.
        this._removeOptimistic(job.file_path, job.mode);
      }
    }
  }

  _replaceAll(jobs) {
    this.jobs = jobs.slice();
    this._byId.clear();
    for (const job of jobs) this._byId.set(job.id, job);
  }

  /** Record tombstones, keeping the newest EVICTED_TOMBSTONES. */
  _rememberEvicted(ids) {
    for (const id of ids) {
      if (id == null || this._evictedIds[id]) continue;
      this._evictedIds[id] = true;
      this._evictedOrder.push(id);
    }
    const excess = this._evictedOrder.length - EVICTED_TOMBSTONES;
    if (excess > 0) {
      for (const id of this._evictedOrder.splice(0, excess)) delete this._evictedIds[id];
    }
  }

  /**
   * Hydrate / re-sync from the REST snapshot.
   *
   * Designed to be called AFTER connect() so that any terminal events
   * (`complete` / `error` / `cancelled`) delivered during snapshot fetch
   * are preserved. The `/api/jobs/events` backend stream only subscribes
   * to jobs that are QUEUED or PROCESSING at the time the loop sees
   * them, a job that goes from PROCESSING to COMPLETED *before* the
   * SSE subscribes would never emit its terminal event. Opening SSE
   * first reduces that race window to the snapshot's generation time
   * on the server.
   *
   * Reconciliation rule: SSE wins. Snapshot only fills in jobs we
   * don't already know about (the SSE never re-emits state for
   * completed/failed/cancelled jobs, so historical entries reach us
   * through the snapshot). Errors are absorbed so a transient
   * /api/jobs failure doesn't surface as an unhandled rejection on
   * page load, the SSE is already connected and the user can recover
   * by retrying any action.
   */
  async refresh() {
    this.loading = true;
    // The jobs known before the request. Only these can be inferred evicted
    // when the snapshot lacks them: one SSE created (and maybe finished) while
    // the request was in flight is simply newer than the snapshot.
    const knownBefore = Object.create(null);
    for (const job of this.jobs) knownBefore[job.id] = true;
    try {
      const data = await api.getJobs();
      if (!Array.isArray(data)) return;
      // Plain object as a transient id→true map. The svelte-eslint
      // `prefer-svelte-reactivity` rule (when present) flags raw Set
      // usage even for non-reactive locals; an object lookup avoids
      // both the rule and any reactivity overhead.
      const remoteIds = Object.create(null);
      for (const job of data) {
        // A snapshot read before a clear or eviction can still list the job.
        if (!job?.id || this._isDeleted(job.id)) continue;
        remoteIds[job.id] = true;
        const existing = this._byId.get(job.id);
        if (!existing) {
          // Unknown to us, definitely add. Most common case for
          // historical jobs (completed before SSE opened).
          this.jobs.push(job);
          this._byId.set(job.id, job);
          continue;
        }
        if (snapshotIsNewer(existing, job)) this._applyJob(job);
      }
      // Reconcile deletions. When another tab clears completed history
      // (or the backend prunes old terminal jobs on its own), /api/jobs
      // stops returning those ids, and there is no SSE event to drop
      // them client-side, so the local store would keep showing stale
      // rows indefinitely. Drop locally-known TERMINAL jobs that are
      // absent from the snapshot. Active jobs are left alone: SSE owns
      // them, and they may simply not be in the snapshot yet if they
      // were just queued between the snapshot generation and arrival.
      // Likewise any job SSE brought in after the request was sent.
      const pruned = [];
      this.jobs = this.jobs.filter((j) => {
        if (knownBefore[j.id] && TERMINAL_STATUSES.has(j.status) && !remoteIds[j.id]) {
          this._byId.delete(j.id);
          pruned.push(j.id);
          return false;
        }
        return true;
      });
      // Finished and gone from the backend: tombstone them like an eviction.
      this._rememberEvicted(pruned);
    } catch (e) {
      console.error('Job snapshot hydration failed:', e);
    } finally {
      this.loading = false;
    }
  }

  // ─── Creation ─────────────────────────────────────────────────────────
  _addOptimistic(filePath, mode) {
    if (this.creatingJobs.length >= MAX_OPTIMISTIC_PLACEHOLDERS) return;
    this.creatingJobs.push({ file_path: filePath, mode, _placeholder: true, _ts: Date.now() });
  }

  _removeOptimistic(filePath, mode) {
    // Drop a single matching placeholder (the oldest), not every one. Submitting
    // the same file+mode twice creates two indistinguishable placeholders;
    // clearing both when the first real job (or skip) resolves would under-count
    // the optimistic total and discard the second placeholder before its own
    // job arrives. Removing one per resolution keeps the count accurate.
    let removed = false;
    this.creatingJobs = this.creatingJobs.filter((p) => {
      if (!removed && p.file_path === filePath && p.mode === mode) {
        removed = true;
        return false;
      }
      return true;
    });
  }

  async create(filePath, mode, opts = {}) {
    this._addOptimistic(filePath, mode);
    try {
      const job = await api.createJob(
        filePath,
        mode,
        opts.outputDir ?? null,
        opts.compression ?? null,
        opts.deleteOnVerify ?? false,
        opts.split ?? false,
        opts.containerOnly ?? false,
      );
      this._applyJob(job);
      return job;
    } catch (e) {
      this._removeOptimistic(filePath, mode);
      throw e;
    }
  }

  async createBatch(filePaths, mode, opts = {}) {
    for (const fp of filePaths) this._addOptimistic(fp, mode);
    try {
      const answer = await api.createBatchJobs(
        filePaths,
        mode,
        opts.outputDir ?? null,
        opts.duplicateAction ?? 'skip',
        opts.compression ?? null,
        opts.deleteOnVerify ?? false,
        opts.split ?? false,
        opts.containerOnly ?? false,
      );
      // `{ jobs, skipped }` (#504); a bare job list from a server before it.
      const created = Array.isArray(answer) ? answer : (answer?.jobs ?? []);
      for (const job of created) this._applyJob(job);
      return { jobs: created, skipped: Array.isArray(answer?.skipped) ? answer.skipped : [] };
    } finally {
      // Always clear placeholders for the requested paths. _applyJob already
      // removes the placeholder for any path that came back as a real job;
      // this also drops placeholders for paths the backend skipped (e.g.
      // duplicates with duplicate_action: 'skip') so they don't linger.
      for (const fp of filePaths) this._removeOptimistic(fp, mode);
    }
  }

  /**
   * Follow job *jobId* (one a request just started) until it ends, reading
   * it from the API and adding each read to the list (#425). Since #456 the
   * job stream sends every job from its creation, so this is only a fallback
   * for a stream that is reconnecting. Independent of whatever started it: a dialog closed
   * meanwhile stops nothing. A failed read is tried again; a job the server
   * no longer has (404) ends the tracking. Resolves with the ended job, or
   * null for one the server no longer has.
   */
  async track(jobId, { interval = 500 } = {}) {
    for (;;) {
      try {
        const read = await api.getJob(jobId);
        this.adopt([read]);
        if (TERMINAL_STATUSES.has(read?.status)) return read;
      } catch (e) {
        if (e?.status === 404) return null;
        // Anything else is transient: the next read tries again.
      }
      await new Promise((resolve) => setTimeout(resolve, interval));
    }
  }

  /** Add jobs another endpoint created (Auto) to the live list. */
  adopt(created) {
    if (!Array.isArray(created)) return;
    for (const job of created) {
      // An HTTP response can be older than the SSE events that beat it here
      // (the job already finished): the same freshness rule as refresh().
      // Unknown because the backend already deleted it (it finished and was
      // evicted or cleared before this response arrived): `_applyJob` keeps
      // it out, by the same rule as every other path.
      const existing = this._byId.get(job?.id);
      if (!existing || snapshotIsNewer(existing, job)) this._applyJob(job);
    }
  }

  // ─── Cancellation / cleanup ──────────────────────────────────────────
  async cancel(jobId) {
    const result = await api.cancelJob(jobId);
    const existing = this._byId.get(jobId);
    if (existing) {
      // Optimistically flip QUEUED jobs to cancelled, the backend
      // dequeues them immediately and there is no worker to wait for.
      //
      // For PROCESSING jobs, the backend only sets the cancel event
      // and leaves status=processing with message="Cancelling..." until
      // the worker exits and emits the terminal `cancelled` SSE event.
      // Forcing status=cancelled here can race the in-flight SSE update
      // and leave counts/history wrong if the SSE arrives reordered or
      // the stream is briefly reconnecting. Update the message field
      // only and trust the terminal SSE to flip status.
      if (existing.status === 'queued') {
        this._applyJob({ ...existing, status: 'cancelled' });
      } else if (existing.status === 'processing') {
        this._applyJob({ ...existing, message: 'Cancelling…' });
      }
    }
    return result;
  }

  async cancelAll() {
    this.cancellingAll = true;
    try {
      const res = await api.cancelAllJobs();
      // SSE will deliver the actual cancelled events; refresh just in case.
      await this.refresh();
      return res;
    } finally {
      this.cancellingAll = false;
    }
  }

  async clearCompleted() {
    this.clearingCompleted = true;
    try {
      const res = await api.deleteCompletedJobs();
      // Remove and tombstone exactly the jobs the backend says it deleted, not
      // the ones terminal here. Local status is a later moment than the one
      // the backend cleared at: a job that finished over SSE while this
      // request was in flight was still running there, so it was kept, and
      // dropping it would hide a live row. A deleted job our stream had not
      // finished yet goes too. The tombstones then keep every late copy out
      // (see `_isDeleted`). No `deleted` list proves nothing was deleted.
      const deleted = Array.isArray(res?.deleted) ? res.deleted : [];
      const gone = Object.create(null);
      for (const id of deleted) gone[id] = true;
      this._rememberEvicted(deleted);
      this.jobs = this.jobs.filter((j) => !gone[j.id]);
      this._byId.clear();
      for (const job of this.jobs) this._byId.set(job.id, job);
      return res;
    } finally {
      this.clearingCompleted = false;
    }
  }

  // ─── SSE lifecycle ────────────────────────────────────────────────────
  connect() {
    if (this._unsubscribe) return;
    this._unsubscribe = api.subscribeToJobs(
      ({ type, data }) => {
        switch (type) {
          // Hydration emission from the backend on (re)connection. Same
          // shape as progress; _applyJob is idempotent so re-running on
          // reconnect just refreshes the slot.
          case 'snapshot':
          // falls through
          case 'progress':
            if (data?.job) this._applyJob(data.job);
            // A check Rule's run sends each Image's new check state on its
            // progress events, as each settles (#305).
            verification.applyJobEvent(data);
            break;
          case 'cancelled':
          case 'error':
            if (data?.job) this._applyJob(data.job);
            verification.applyJobEvent(data);
            break;
          case 'complete':
            if (data?.job) this._applyJob(data.job);
            // Job completion carries verified / source_deleted alongside
            // the job payload (job_manager._notify_subscribers): the
            // verification store applies them.
            verification.applyJobEvent(data);
            break;
          case 'status':
            // Status pulses don't carry a job payload in some emit paths.
            if (data?.job) this._applyJob(data.job);
            break;
          default:
            break;
        }
      },
      {
        onOpen: () => {
          ui.reportConnection('open');
          // Reconcile rows against the backend on every (re)connect: jobs
          // cleared or past the history cap while we were away go.
          this.refresh().catch(() => {});
          // Re-sync verified state on every SSE (re)connect. Terminal
          // job snapshots emitted at reconnect only carry the `job`
          // payload, they drop the `verified` and `source_deleted`
          // side-effect flags that the live `complete` event uses to
          // mutate verification.statuses, so the OK badge cache could
          // drift after a brief backend outage. Reloading from
          // /api/verified is the cheapest way to re-establish truth.
          verification.loadVerified();
        },
        onReconnecting: () => ui.reportConnection('reconnecting'),
      },
    );
    // Lightweight polling fallback for jobs queued by OTHER clients.
    // The SSE feed does not emit anything for the QUEUED state, its
    // first frame for a normal job is the PROCESSING transition, so
    // a second tab/user's job sitting in the queue behind a long-
    // running local conversion stays invisible without this poll.
    // We DON'T skip while hasActive is true: the queued job from the
    // other client is exactly the case this exists to surface, and
    // it's invisible precisely when we have an active conversion of
    // our own. refresh() is idempotent, SSE-derived state always
    // wins for jobs that overlap. Only skip while a modal is open so
    // background entry swaps don't race user-driven actions.
    if (!this._pollTimer) {
      this._pollTimer = setInterval(() => {
        if (ui.anyEntryModalOpen) return;
        this.refresh().catch(() => {});
      }, 30000);
    }
  }

  dispose() {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }
}

export const jobs = new JobsStore();
