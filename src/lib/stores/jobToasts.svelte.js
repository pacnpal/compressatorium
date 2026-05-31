// Per-job toast tracker. Surfaces a live toast for every RUNNING
// (processing) job and resolves it to success/failure/cancelled when the
// job reaches a terminal state.
//
// Why a dedicated module rather than inlining in JobRow: a job row only
// exists while the Jobs panel is mounted and the job is on the visible
// page. Toasts must follow the job across navigation, pagination, and the
// queue/completed tab split — so the lifecycle is driven by reconciling
// against the jobs store from a single long-lived $effect (wired in
// App.svelte), keyed by job id so each running job owns exactly one toast.

import { toast } from 'svelte-sonner';
import { registry } from '$lib/tools/registry.js';

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);
// Mirror jobs.svelte.js: metadata_scan / dat_match are background
// housekeeping jobs the queue hides by default. They run constantly as a
// side-effect of browsing (FileList hydration kicks dat_match jobs), so
// toasting them would flood the surface. They're only toasted when the
// user has opted to show external-scan jobs in the queue.
const EXTERNAL_SCAN_MODES = new Set(['metadata_scan', 'dat_match']);

function filenameOf(job) {
  return job?.file_path?.split(/[/\\]/).pop() ?? job?.file_path ?? 'Job';
}

/** Short "Tool · Mode" subtitle, e.g. "CHDMAN · Create CD". */
function descriptorOf(job) {
  const tool = registry.toolForMode(job?.mode);
  const spec = registry.specFor(job?.mode);
  const parts = [tool?.label, spec?.label ?? job?.mode].filter(Boolean);
  return parts.join(' · ');
}

function runningDescription(job) {
  const base = descriptorOf(job);
  const pct =
    typeof job?.progress === 'number' && job.progress > 0
      ? `${Math.round(job.progress)}%`
      : null;
  // The backend message (e.g. "Cancelling…", "Verifying output") is the
  // most useful line when present; fall back to the descriptor + percent.
  // Use truthiness (not ??) for the fallbacks: `descriptorOf` returns ''
  // for an unknown mode, and '' is not nullish — so `?? 'Processing…'`
  // would surface a blank description instead of the fallback.
  const lead = job?.message || base;
  if (lead && pct) return `${lead} · ${pct}`;
  return lead || pct || 'Processing…';
}

class JobToastTracker {
  // jobId → { toastId } for jobs that currently own a live toast.
  //
  // A plain null-prototype object, NOT a Map/SvelteMap. reconcile() runs
  // inside the App.svelte $effect and both reads and writes this
  // collection: a reactive SvelteMap would subscribe the effect to its
  // own writes and loop (effect_update_depth_exceeded), while a plain Map
  // trips the svelte/prefer-svelte-reactivity lint rule. The object map is
  // the same non-reactive escape jobs.svelte.js uses for its transient
  // id→state lookups — correct here and lint-clean.
  _active = Object.create(null);

  /**
   * Reconcile the live toast set against the current jobs list.
   *
   * Called from a reactive $effect, so it re-runs on every job mutation
   * (SSE progress frame, terminal event, snapshot hydration). Idempotent:
   * re-running with unchanged input is a no-op beyond refreshing the
   * in-place loading description.
   *
   * @param {Array<any>} jobList - jobs.jobs
   * @param {{ showExternalScan?: boolean }} [opts]
   */
  reconcile(jobList, { showExternalScan = false } = {}) {
    // Transient id→true membership scratch for this pass. Plain object for
    // the same reason as _active (see the field comment).
    const present = Object.create(null);

    for (const job of jobList) {
      const id = job?.id;
      if (id == null) continue;
      if (!showExternalScan && EXTERNAL_SCAN_MODES.has(job.mode)) continue;
      present[id] = true;

      const rec = this._active[id];

      if (job.status === 'processing') {
        const title = filenameOf(job);
        const description = runningDescription(job);
        if (rec) {
          // Refresh the existing loading toast in place (progress bump,
          // message change like "Cancelling…").
          toast.loading(title, { id: rec.toastId, description });
        } else {
          const toastId = toast.loading(title, {
            description,
            duration: Number.POSITIVE_INFINITY,
          });
          this._active[id] = { toastId };
        }
      } else if (TERMINAL_STATUSES.has(job.status) && rec) {
        this._resolve(job, rec.toastId);
        delete this._active[id];
      }
      // queued / unknown: no toast yet — a job earns its toast when it
      // starts processing, and historical terminal jobs we never tracked
      // (rec === undefined) are skipped so a page reload doesn't replay
      // "completed" toasts for old history.
    }

    // A running job can vanish from the list without a terminal frame —
    // another tab clears it, or the backend prunes it. Dismiss its stale
    // loading toast so it doesn't spin forever.
    for (const id of Object.keys(this._active)) {
      if (!present[id]) {
        toast.dismiss(this._active[id].toastId);
        delete this._active[id];
      }
    }
  }

  _resolve(job, toastId) {
    const title = filenameOf(job);
    if (job.status === 'completed') {
      toast.success(title, {
        id: toastId,
        description: job.output_path ? `→ ${job.output_path}` : 'Completed',
        duration: 4000,
      });
    } else if (job.status === 'failed') {
      toast.error(title, {
        id: toastId,
        description: job.error_message || 'Failed',
        duration: 6000,
      });
    } else {
      toast.warning(title, {
        id: toastId,
        description: 'Cancelled',
        duration: 3000,
      });
    }
  }

  /** Dismiss every live toast (called on app teardown). */
  dispose() {
    for (const id of Object.keys(this._active)) {
      toast.dismiss(this._active[id].toastId);
      delete this._active[id];
    }
  }
}

export const jobToasts = new JobToastTracker();
