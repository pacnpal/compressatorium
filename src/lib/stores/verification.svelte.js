// Verification store, tracks verified output paths, per-file verify
// progress, and the active batch-verify run. Single-file verify uses
// EventSource (sse.js); batch verify uses POST + ReadableStream (sseFetch.js)
// via the registry's verifyBatch binding.

import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';

// At most this many paths per /verified/lookup (the server's MAX_VERIFIED_LOOKUP).
const LOOKUP_CHUNK = 2000;

/** A `{verified, checks}` answer as path -> its check ('intact' by default). */
function passedChecks(data) {
  const passed = Object.create(null);
  for (const path of data?.verified ?? []) {
    passed[path] = data?.checks?.[path] ?? 'intact';
  }
  return passed;
}

class VerificationStore {
  statuses = new SvelteSet();
  // path -> 'intact' | 'round_tripped' | 'recognized': which check a stored output passed.
  // A path in `statuses` with no entry here passed the container check.
  checks = new SvelteMap();
  progress = new SvelteMap();
  batchRun = $state(null);
  _batchAbort = null;
  // One ordering for every write into `statuses` / `checks` (#298, codex
  // 4578598280, 4579128146). `_seq` grows with each live write and each
  // request start; `_versions` holds, per path, the number of the write that
  // set it. A server answer carries its request's number and only replaces a
  // path set before that request, so neither an older full reload nor an
  // older lookup can undo a newer live event or a newer answer, whichever
  // lands first. Plain object: a bookkeeping table nothing renders.
  _seq = 0;
  _versions = Object.create(null);
  // The number of the newest full answer applied. A full answer speaks for
  // every path, the ones it never names too (codex 4579151542), so nothing
  // older than it may set any path afterwards.
  _floor = 0;
  // Re-reads of a check run's paths still in flight (see applyJobEvent).
  _lookups = new Set();

  /**
   * Take the server's answer: for every path, or, with `paths`, just those (a
   * check run's, through /verified/lookup, in chunks the server accepts). It
   * replaces only what is older than the request (see `_seq`).
   */
  async loadVerified(paths = null) {
    const version = ++this._seq;
    try {
      if (paths) {
        for (let i = 0; i < paths.length; i += LOOKUP_CHUNK) {
          const chunk = paths.slice(i, i + LOOKUP_CHUNK);
          const data = await api.getVerifiedFor(chunk);
          const passed = passedChecks(data);
          for (const path of chunk) this._set(path, passed[path] ?? null, version);
        }
      } else {
        const data = await api.getVerifiedCHDs();
        const passed = passedChecks(data);
        this._floor = Math.max(this._floor, version);
        // Every path either side knows: the answer's, and ours (a path the
        // server no longer lists is no longer checked).
        for (const path of [...this.statuses]) {
          if (!(path in passed)) this._set(path, null, version);
        }
        for (const [path, check] of Object.entries(passed)) {
          this._set(path, check, version);
        }
      }
    } catch (_e) {
      // non-fatal, leave whatever we had
    }
  }

  /** Resolves once every check-run re-read in flight has landed. */
  async pendingLookups() {
    await Promise.all([...this._lookups]);
  }

  isVerified(path) {
    return path ? this.statuses.has(path) : false;
  }

  /** 'round_tripped' | 'recognized' | 'intact' for a path that passed a check, else null. */
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
    this._set(path, check, ++this._seq);
  }

  /** Drop everything known about `path` (deleted, or no longer checked). */
  forget(path) {
    if (!path) return;
    this._set(path, null, ++this._seq);
  }

  /**
   * Write `path` unless something newer than `version` already spoke for it:
   * a write to that path, or a full answer, which covers every path.
   */
  _set(path, check, version) {
    if (version < Math.max(this._versions[path] ?? 0, this._floor)) return;
    this._versions[path] = version;
    this._write(path, check);
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
    // A check run's end lists the files it settled. Its progress events can
    // be dropped on a busy stream, so the database answers for those files
    // again; live events that land meanwhile still win (codex 4578664511).
    const settled = data?.verification_paths;
    if (Array.isArray(settled) && settled.length) {
      const lookup = this.loadVerified(settled);
      this._lookups.add(lookup);
      lookup.finally(() => this._lookups.delete(lookup));
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
