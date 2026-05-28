// Verification store — tracks verified output paths, per-file verify
// progress, and the active batch-verify run. Single-file verify uses
// EventSource (sse.js); batch verify uses POST + ReadableStream (sseFetch.js)
// via the registry's verifyBatch binding.

import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';

class VerificationStore {
  statuses = new SvelteSet();
  progress = new SvelteMap();
  batchRun = $state(null);
  _batchAbort = null;

  async loadVerified() {
    try {
      const data = await api.getVerifiedCHDs();
      this.statuses.clear();
      for (const path of data?.verified ?? []) this.statuses.add(path);
    } catch (_e) {
      // non-fatal — leave whatever we had
    }
  }

  isVerified(path) {
    return path ? this.statuses.has(path) : false;
  }

  progressFor(path) {
    return path ? this.progress.get(path) ?? null : null;
  }

  // ─── Single-file verify ───────────────────────────────────────────────
  async verifyOne(toolId, path) {
    const tool = registry.forTool(toolId);
    if (!tool) throw new Error(`Unknown tool: ${toolId}`);
    this.progress.set(path, { percent: 0, message: 'Starting…' });
    try {
      const result = await tool.verify(path, {
        onProgress: (data) => {
          this.progress.set(path, {
            percent: typeof data?.progress === 'number' ? data.progress : null,
            message: data?.message ?? '',
          });
        },
      });
      this.progress.delete(path);
      if (result?.valid) this.statuses.add(path);
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
          if (data?.path && data?.valid) this.statuses.add(data.path);
        },
      });
      if (this.batchRun) {
        this.batchRun = {
          ...this.batchRun,
          done: this.batchRun.total,
          verified: result?.verified ?? this.batchRun.verified,
          failed: result?.failed ?? this.batchRun.failed,
          currentPath: null,
          currentFilename: null,
          currentPercent: 100,
          message: 'Batch complete',
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
