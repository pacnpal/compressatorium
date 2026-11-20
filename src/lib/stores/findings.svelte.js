// Findings (#304): what needs a person's decision, and the folder Platform
// mappings that settle most of them.
//
// The backend keeps one open Finding per kind and Image (Platform conflict,
// Unsorted), written by each folder Library's walk. Dismissing one hides it
// until its cause changes. Nothing here moves or deletes a file: each Finding
// links to the action that fixes it (open its folder, set the folder's
// Platform).

import { api } from '$lib/api/endpoints.js';

/**
 * What labels and toasts call a Finding. A duplicate group (#388) is named
 * by its first copy's file name and how many others there are ("Game.iso
 * and 2 other copies"), never by its `raw:<sha1>` key; any other Finding by
 * its Image key.
 */
/** What to tell the user when the copy kept changed during its delete. */
export function keeperWarning(keep) {
  const name = keep?.split(/[/\\]/).filter(Boolean).pop() ?? 'The copy kept';
  return `${name}, the copy kept, changed while the other copies were deleted: check it (Verify) before relying on it.`;
}

export function findingName(row) {
  if (row?.kind !== 'duplicate') return row?.image_key ?? '';
  const copies = row.cause?.copies ?? [];
  const first = copies[0]?.path?.split(/[/\\]/).filter(Boolean).pop() ?? 'A duplicate group';
  const others = Math.max(0, copies.length - 1);
  return `${first} and ${others} other ${others === 1 ? 'copy' : 'copies'}`;
}

class FindingsStore {
  list = $state([]);
  // How many Findings a dismiss is hiding right now.
  dismissed = $state(0);
  // Each Library's last find-duplicates run (#388): what it keyed and which
  // formats it could not check, shown beside the duplicate groups.
  duplicateRuns = $state([]);
  showDismissed = $state(false);
  loading = $state(false);

  /** How many problems are open (not ignored): the sidebar's Activity badge. */
  get openCount() {
    return this.list.reduce((n, row) => (row?.dismissed ? n : n + 1), 0);
  }
  error = $state(null);
  checking = $state(false);
  platforms = $state([]);
  // The folder the mapping dialog is open on: `{ path, name }`, or null.
  mappingTarget = $state(null);
  // The open dialog's read of that folder's current Platform: `loading`,
  // `loaded` (`mappingCurrent` is the slug, or null for none set) or `error`
  // (`mappingError` says why; "couldn't load" is never shown as "none set").
  mappingCurrent = $state(null);
  mappingState = $state('idle');
  mappingError = $state(null);
  // Bumped by every opening, closing and retry: only the read that matches
  // writes, so a response (or failure) from an earlier opening is dropped.
  #mappingRead = 0;
  // Set when a mapping was saved while its Library was mid-walk: the
  // backend's note that the Findings refresh after that walk. Check now
  // clears it.
  pending = $state(null);
  // Only the latest listing request may write the list (rapid toggles).
  #request = 0;
  // The finished find-duplicates runs (#388) already accounted for, by job id;
  // null until the view first reports them.
  #runsSeen = null;

  /**
   * The view's finished find-duplicates runs. Lists again when one is new
   * since the last report (a run just ended), not on the first report (the
   * view lists on open anyway) or when one is cleared from the jobs list.
   * Returns whether it listed.
   */
  noteFinishedRuns(ids) {
    const before = this.#runsSeen;
    this.#runsSeen = new Set(ids);
    if (before === null || !ids.some((id) => !before.has(id))) return false;
    this.load();
    return true;
  }

  async load() {
    const request = ++this.#request;
    this.loading = true;
    this.error = null;
    try {
      const data = await api.listFindings(this.showDismissed);
      if (request !== this.#request) return this.list;
      this.list = Array.isArray(data?.findings) ? data.findings : [];
      this.dismissed = data?.dismissed ?? 0;
      this.duplicateRuns = Array.isArray(data?.duplicate_runs) ? data.duplicate_runs : [];
    } catch (e) {
      if (request === this.#request) this.error = e?.message ?? 'Failed to list the Findings';
    } finally {
      if (request === this.#request) this.loading = false;
    }
    return this.list;
  }

  #fingerprint(id) {
    return this.list.find((f) => f.id === id)?.fingerprint ?? null;
  }

  /**
   * A mutation succeeded: drop every listing that started before it (its
   * snapshot predates the change), then list again.
   */
  #superseded() {
    this.#request += 1;
  }

  /** Hide one Finding until its cause changes. */
  async dismiss(id) {
    await api.dismissFinding(id, this.#fingerprint(id));
    this.#superseded();
    if (this.showDismissed) {
      this.list = this.list.map((f) => (f.id === id ? { ...f, dismissed: true } : f));
    } else {
      this.list = this.list.filter((f) => f.id !== id);
    }
    this.dismissed += 1;
    await this.load();
  }

  /** Show a dismissed Finding again. */
  async undismiss(id) {
    await api.undismissFinding(id, this.#fingerprint(id));
    this.#superseded();
    this.list = this.list.map((f) => (f.id === id ? { ...f, dismissed: false } : f));
    this.dismissed = Math.max(0, this.dismissed - 1);
    await this.load();
  }

  // The open "delete the other copies" dialog (#389): the group, the copy to
  // keep, and the plan the backend made for it (`null` while it loads).
  cleanup = $state(null);
  // Only the latest plan request may write it (a keeper picked twice fast).
  #planRequest = 0;
  // Deletes still running, by Finding id: `{ keep, plan, jobId }`. Kept apart
  // from the dialog, so closing it stops nothing and reopening that Finding
  // shows the delete as running, its keeper fixed, until its job ends.
  #inflight = new Map();
  // The delete jobs (#389) this page started and has not seen end, by job id:
  // the view settles them from the jobs store (`noteFinishedDeletes`).
  runningDeletes = $state([]);

  /** Open the delete dialog on a duplicate group, keeping *keep*. */
  async openCleanup(row, keep = row?.keeper) {
    if (!row || !keep) return;
    // The keeper is fixed while its delete runs: a new plan would drop it.
    const running = this.#inflight.get(row.id);
    if (running) {
      if (this.cleanup?.row?.id !== row.id) {
        this.#planRequest += 1;
        this.cleanup = {
          row, keep: running.keep, plan: running.plan, error: null, deleting: true,
          jobId: running.jobId,
        };
      }
      return;
    }
    if (this.cleanup?.deleting) return;
    const request = ++this.#planRequest;
    this.cleanup = { row, keep, plan: null, error: null, deleting: false };
    try {
      const plan = await api.duplicateDeletePlan(row.id, row.fingerprint, keep);
      if (request !== this.#planRequest || !this.cleanup) return;
      this.cleanup = { ...this.cleanup, plan };
    } catch (e) {
      if (request !== this.#planRequest || !this.cleanup) return;
      this.cleanup = { ...this.cleanup, error: e?.message ?? 'Could not plan the delete' };
    }
  }

  /** Whether the open dialog is the one for Finding *id*. */
  #showing(id) {
    return this.cleanup?.row?.id === id;
  }

  closeCleanup() {
    this.#planRequest += 1;
    this.cleanup = null;
  }

  /**
   * Start deleting exactly what the open plan listed (#389). The backend
   * answers `{ job_id }` at once and deletes in a background job; the dialog
   * stays "deleting", its keeper fixed, until the view reports that job's end
   * (`noteFinishedDeletes`). Returns the backend's answer, or null when there
   * is nothing to confirm.
   */
  async confirmCleanup() {
    const open = this.cleanup;
    const plan = open?.plan;
    if (!plan || plan.blocked || open.deleting || this.#inflight.has(open.row.id)) return null;
    const id = open.row.id;
    const paths = plan.items.flatMap((item) => item.delete_paths ?? []);
    this.#inflight.set(id, { keep: open.keep, plan, jobId: null });
    this.cleanup = { ...open, deleting: true };
    // The answer belongs to this Finding: it writes only to a dialog open on
    // it now (reopened meanwhile included), never to one for another Finding.
    try {
      const started = await api.deleteDuplicates(id, open.row.fingerprint, open.keep, paths);
      const jobId = started?.job_id ?? null;
      this.#inflight.set(id, { keep: open.keep, plan, jobId });
      if (jobId) this.runningDeletes = [...this.runningDeletes, jobId];
      if (this.#showing(id)) this.cleanup = { ...this.cleanup, jobId };
      return started;
    } catch (e) {
      this.#inflight.delete(id);
      if (this.#showing(id)) {
        this.cleanup = { ...this.cleanup, deleting: false, error: e?.message ?? 'Could not delete the copies' };
      }
      await this.load();
      throw e;
    }
  }

  /**
   * The view's finished delete jobs (#389). Each one this page started
   * settles its Finding's delete: a dialog open on that Finding closes on a
   * plain success, stays done with a warning when the copy kept changed
   * meanwhile (Codex 4593499350), or shows why the job failed or was
   * cancelled. Lists again when any settled. Returns `[{ job, result }]` for
   * those it settled; a job it did not start, or already settled, is ignored.
   */
  noteFinishedDeletes(jobs) {
    const settled = [];
    for (const job of jobs ?? []) {
      const entry = [...this.#inflight].find(([, running]) => running.jobId === job?.id);
      if (!entry) continue;
      const [id, running] = entry;
      this.#inflight.delete(id);
      settled.push({ job, result: job.result ?? null });
      if (!this.#showing(id)) continue;
      if (job.status === 'completed') {
        this.cleanup = job.result?.keeper_changed
          ? { ...this.cleanup, deleting: false, done: true, warning: keeperWarning(running.keep) }
          : null;
      } else {
        const why = job.error_message || job.message || 'The delete did not finish';
        this.cleanup = { ...this.cleanup, deleting: false, error: why };
      }
    }
    if (!settled.length) return settled;
    const ended = new Set(settled.map((s) => s.job.id));
    this.runningDeletes = this.runningDeletes.filter((jobId) => !ended.has(jobId));
    this.#superseded();
    this.load();
    return settled;
  }

  /** Walk every folder Library now, then list what it found. */
  async check() {
    this.checking = true;
    try {
      const result = await api.checkFindings();
      this.pending = null;
      await this.load();
      return result;
    } finally {
      this.checking = false;
    }
  }

  async loadPlatforms() {
    if (this.platforms.length) return this.platforms;
    const data = await api.listPlatforms();
    this.platforms = Array.isArray(data) ? data : [];
    return this.platforms;
  }

  /** The Platform set on the folder at *path*, or null when none is. */
  async currentMapping(path) {
    const rows = await api.listPlatformMappings();
    const hit = (Array.isArray(rows) ? rows : []).find((m) => m?.path === path);
    return hit?.platform ?? null;
  }

  /** Open the mapping dialog on a folder, and read what it is set to. */
  openMapping(path, name = null) {
    if (!path) return;
    this.mappingTarget = { path, name: name ?? path.split(/[/\\]/).filter(Boolean).pop() ?? path };
    this.loadCurrentMapping();
  }

  /**
   * Read the open dialog's folder mapping (again, for Retry). Owned by this
   * opening: a later opening, a close or a retry makes this read's answer,
   * or its failure, a no-op.
   */
  async loadCurrentMapping() {
    const target = this.mappingTarget;
    if (!target) return;
    const read = ++this.#mappingRead;
    this.mappingState = 'loading';
    this.mappingCurrent = null;
    this.mappingError = null;
    try {
      const slug = await this.currentMapping(target.path);
      if (read !== this.#mappingRead) return;
      this.mappingCurrent = slug;
      this.mappingState = 'loaded';
    } catch (e) {
      if (read !== this.#mappingRead) return;
      this.mappingError = e?.message ?? "Could not read the folder's Platform";
      this.mappingState = 'error';
    }
  }

  closeMapping() {
    this.mappingTarget = null;
    this.#mappingRead += 1;
    this.mappingState = 'idle';
    this.mappingCurrent = null;
    this.mappingError = null;
  }

  /** Set (or with `platform` null, clear) a folder's Platform, then refresh the list. */
  async setMapping(path, platform) {
    const saved = await api.setPlatformMapping(path, platform);
    if (saved?.walk?.status === 'listing_in_progress') {
      this.pending = saved.walk.message ?? 'A listing is running; Findings refresh after it.';
    }
    await this.load();
    return saved;
  }
}

export const findings = new FindingsStore();
