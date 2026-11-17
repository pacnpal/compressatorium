// Single source of truth for the job-status / job-mode sets shared across the
// job stores (jobs.svelte.js and jobToasts.svelte.js). Each store previously
// defined its own copy of TERMINAL_STATUSES / EXTERNAL_SCAN_MODES (jobToasts
// even commented "Mirror jobs.svelte.js"); a drift between the copies would
// desync which jobs the queue and the toast tracker treat as finished or hide.

// Statuses a job never leaves — used to resolve a toast, prune a finished job,
// or prefer the incoming copy during reconciliation.
export const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);

// Statuses a job is still working through.
export const ACTIVE_STATUSES = new Set(['queued', 'processing']);

// Background housekeeping modes the queue and the toast surface hide by
// default: they run constantly as a side-effect of browsing (FileList
// hydration kicks dat_match jobs), so surfacing them would flood the UI.
// Shown only when the user opts into external-scan jobs.
export const EXTERNAL_SCAN_MODES = new Set(['metadata_scan', 'dat_match']);

// Display names for the externally-managed job modes, which have no registry
// spec. A check Rule's run (#305) and a find-duplicates run (#388) are visible
// by default; the two scans above stay behind the toggle.
export const EXTERNAL_MODE_LABELS = Object.freeze({
  metadata_scan: 'Metadata scan',
  dat_match: 'DAT match',
  check: 'Check',
  // A Library's find-duplicates run (#388), started from the Rules view.
  find_duplicates: 'Find duplicates',
  // A confirmed delete of a duplicate group's other copies (#389).
  delete_duplicates: 'Delete duplicates',
});
