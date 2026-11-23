// How a completed job's running toast resolves. Kept out of jobToasts.svelte.js
// so it can be tested on its own (tests/test_frontend_job_toast_306.py).
//
// A completed job that did less than asked carries `job.warning` (a
// delete-source run that kept its Source because another job was reading it,
// #306; a tool's caveat, #323): it resolves as a warning naming it, never as a
// plain success. The job row shows the same warning (jobDetail.js).
//
// A success says what the job did (#595): its new file, whether that passed a
// check, and the space saved, or how much smaller it is with the original kept.

import { baseName, formatSize } from '../api/format.js';
import { jobChecked, jobKeptDelta, jobSaved, savedPhrase } from './runResult.js';

// "saved 2 MB" or "1 MB larger" (signed, as the Ledger); with the original
// kept, "2 MB smaller, original kept". Null when sizes aren't known.
function sizeNote(job) {
  const saved = savedPhrase(jobSaved(job));
  if (saved) return saved;
  const kept = jobKeptDelta(job);
  if (kept > 0) return `${formatSize(kept)} smaller, original kept`;
  if (kept < 0) return `${formatSize(-kept)} larger, original kept`;
  return null;
}

/**
 * @param {any} job
 * @returns {{ kind: 'success' | 'warning', description: string, duration: number }}
 */
export function completionToast(job) {
  if (job?.warning) {
    return { kind: 'warning', description: job.warning, duration: 8000 };
  }
  // The new file's name, not its full path inside the container.
  const output = job?.output_path ? `→ ${baseName(job.output_path)}` : 'Completed';
  const description = [output, jobChecked(job) ? 'checked' : null, sizeNote(job)]
    .filter(Boolean).join(' · ');
  return { kind: 'success', description, duration: 4000 };
}
