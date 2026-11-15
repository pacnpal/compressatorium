// Verification store, tracks verified output paths, per-file verify
// progress, and the active batch-verify run. Single-file verify uses
// EventSource (sse.js); batch verify uses POST + ReadableStream (sseFetch.js)
// via the registry's verifyBatch binding.

import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';

class VerificationStore {
  statuses = new SvelteSet();
  // path -> 'intact' | 'round_tripped': which check a stored output passed.
  // A path in `statuses` with no entry here passed the container check.
  checks = new SvelteMap();
  progress = new SvelteMap();
  batchRun = $state(null);
  _batchAbort = null;
  // One log per loadVerified() still in flight: every live write made while
  // it waits (a `verifyOne` resolved, a job event, a check run's per-Image
  // change), path -> check, or null for a removal. Replayed over that load's
  // snapshot, which is older than all of them, so an addition, a changed
  // check and a removal all survive it alike (#298, codex 4578598280).
  // A Set, so overlapping loads each keep their own log.
  _liveLogs = new Set();

  async loadVerified() {
    // A plain Map: a transient log nothing renders.
    const live = new Map();
    this._liveLogs.add(live);
    try {
      const data = await api.getVerifiedCHDs();
      this.statuses.clear();
      for (const path of data?.verified ?? []) this.statuses.add(path);
      this.checks.clear();
      for (const [path, check] of Object.entries(data?.checks ?? {})) {
        this.checks.set(path, check);
      }
      // What happened while the request was out is newer than its answer.
      for (const [path, check] of live) this._write(path, check);
    } catch (_e) {
      // non-fatal, leave whatever we had
    } finally {
      this._liveLogs.delete(live);
    }
  }

  isVerified(path) {
    return path ? this.statuses.has(path) : false;
  }

  /** 'round_tripped' | 'intact' for a path that passed a check, else null. */
  checkFor(path) {
    if (!this.isVerified(path)) return null;
    return this.checks.get(path) ?? 'intact';
  }

  // The only writers of `statuses` and `checks` outside a load: a path and
  // its check always change together, so no badge keeps a check that belongs
  // to another file or an earlier run.

  /** Record that `path` passed `check`. A manual Verify is 'intact'. */
  markChecked(path, check = 'intact') {
    if (!path) return;
    this._write(path, check);
    for (const log of this._liveLogs) log.set(path, check);
  }

  /** Drop everything known about `path` (deleted, or no longer checked). */
  forget(path) {
    if (!path) return;
    this._write(path, null);
    for (const log of this._liveLogs) log.set(path, null);
  }

  /** Set `path`'s check, or clear it with null. Every write goes through here. */
  _write(path, check) {
    if (check) {
      this.statuses.add(path);
      this.checks.set(path, check);
    } else {
      this.statuses.delete(path);
      this.checks.delete(path);
    }
  }

  /**
   * Apply what a job's event on the jobs stream says about checks.
   *
   * A conversion's `complete` carries `verified` / `check_passed` for its
   * output and `source_deleted` for its Source. A check Rule's run (#305)
   * sends one `progress` event per Image as it settles, carrying
   * `verification_changes`: that Image, by the path `/api/verified` lists it
   * under, with its check now, or null once it is no longer Intact. Applied in
   * the order they arrive, so a newer pass (a repair and a re-verify) is never
   * undone by the run's later events.
   */
  applyJobEvent(data) {
    const job = data?.job;
    if (data?.verified && job?.output_path) {
      this.markChecked(job.output_path, data.check_passed ?? 'intact');
    }
    if (data?.source_deleted && job?.file_path) this.forget(job.file_path);
    for (const [path, check] of Object.entries(data?.verification_changes ?? {})) {
      if (check) this.markChecked(path, check);
      else this.forget(path);
    }
  }

  /** Follow a rename. `keep` carries the check over (same-format rename). */
  move(oldPath, newPath, { keep }) {
    const had = this.isVerified(oldPath);
    const check = this.checks.get(oldPath) ?? 'intact';
    this.forget(oldPath);
    if (had && keep) this.markChecked(newPath, check);
  }

  progressFor(path) {
    return path ? this.progress.get(path) ?? null : null;
  }

  // ─── Single-file verify ───────────────────────────────────────────────
  // `onProgress` (optional) is forwarded the same { percent, message }
  // shape stored in the progress map, so a caller can drive a live toast
  // without subscribing to the store. Progress reporting is best-effort:
  // a callback that throws must not fail the verify or strand the
  // progress entry, so every forward is swallowed.
  async verifyOne(toolId, path, { onProgress } = {}) {
    const tool = registry.forTool(toolId);
    if (!tool) throw new Error(`Unknown tool: ${toolId}`);
    const starting = { percent: 0, message: 'Starting…' };
    this.progress.set(path, starting);
    try {
      onProgress?.(starting);
    } catch (_e) {
      // a broken progress callback shouldn't break verification
    }
    try {
      const result = await tool.verify(path, {
        onProgress: (data) => {
          const next = {
            percent: typeof data?.progress === 'number' ? data.progress : null,
            message: data?.message ?? '',
          };
          this.progress.set(path, next);
          try {
            onProgress?.(next);
          } catch (_e) {
            // a broken progress callback shouldn't break verification
          }
        },
      });
      this.progress.delete(path);
      if (result?.valid) this.markChecked(path);
      return result;
    } catch (e) {
      this.progress.delete(path);
      throw e;
    }
  }

  // ─── Batch verify ────────────────────────────────────────────────────
  async verifyBatch(toolId, paths) {
    const tool = registry.forTool(toolId);
    if (!tool) throw new Error(`Unknown tool: ${toolId}`);
    if (this._batchAbort) {
      // Refuse to start a second batch concurrently.
      throw new Error('Another batch verification is already running');
    }
    this._batchAbort = new AbortController();
    this.batchRun = {
      toolId,
      total: paths.length,
      done: 0,
      verified: 0,
      failed: 0,
      currentPath: null,
      currentFilename: null,
      currentPercent: null,
      message: '',
    };

    try {
      const result = await tool.verifyBatch(paths, {
        signal: this._batchAbort.signal,
        onProgress: (evt) => this._handleBatchProgress(evt),
        onFileComplete: (data) => {
          if (data?.path && data?.valid) this.markChecked(data.path);
        },
      });
      if (this.batchRun) {
        // A batch the backend stopped early did not process every file, so it
        // must not be shown as 100% of the list done — the counts it did reach
        // stand, the progress it never made does not.
        const aborted = result?.aborted === true;
        this.batchRun = {
          ...this.batchRun,
          done: aborted ? this.batchRun.done : this.batchRun.total,
          verified: result?.verified ?? this.batchRun.verified,
          failed: result?.failed ?? this.batchRun.failed,
          currentPath: null,
          currentFilename: null,
          currentPercent: aborted ? this.batchRun.currentPercent : 100,
          // A batch the backend stopped early is not a completed batch: say so
          // rather than reporting 'Batch complete' over an unfinished list.
          message: aborted ? (result.message ?? 'Batch stopped') : 'Batch complete',
        };
      }
      return result;
    } finally {
      this._batchAbort = null;
    }
  }

  cancelBatch() {
    if (this._batchAbort) {
      this._batchAbort.abort();
      this._batchAbort = null;
    }
    this.batchRun = null;
  }

  /**
   * Drop the batchRun summary left behind after a run completes
   * normally. cancelBatch() also aborts the in-flight fetch, which is
   * wrong for an already-finished run; this is the no-abort variant
   * the UI calls when dismissing a completed batch. Anything observing
   * `batchRun` as a "batch is active" signal (e.g. the auto-refresh
   * gate in App.svelte) needs this cleared or it stays blocked forever.
   */
  clearBatch() {
    this.batchRun = null;
  }

  _handleBatchProgress(evt) {
    if (!this.batchRun) return;
    switch (evt.type) {
      case 'start':
        this.batchRun = {
          ...this.batchRun,
          total: evt.total ?? this.batchRun.total,
        };
        break;
      case 'progress':
        this.batchRun = {
          ...this.batchRun,
          done: typeof evt.index === 'number' ? evt.index : this.batchRun.done,
          verified: evt.verified ?? this.batchRun.verified,
          failed: evt.failed ?? this.batchRun.failed,
          currentPath: evt.path ?? null,
          currentFilename: evt.filename ?? null,
        };
        break;
      case 'file_progress':
        this.batchRun = {
          ...this.batchRun,
          currentPercent: typeof evt.progress === 'number' ? evt.progress : null,
          message: evt.message ?? '',
        };
        break;
      case 'file_complete':
        this.batchRun = {
          ...this.batchRun,
          done: (typeof evt.index === 'number' ? evt.index : this.batchRun.done) + 1,
          verified: evt.verified ?? this.batchRun.verified,
          failed: evt.failed ?? this.batchRun.failed,
          currentPercent: 100,
        };
        break;
      default:
        break;
    }
  }
}

export const verification = new VerificationStore();
