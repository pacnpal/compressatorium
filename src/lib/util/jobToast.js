// How a completed job's running toast resolves. Kept out of jobToasts.svelte.js
// so it can be tested on its own (tests/test_frontend_job_toast_306.py).
//
// A completed job that did less than asked carries `job.warning` (a
// delete-source run that kept its Source because another job was reading it,
// #306; a tool's caveat, #323): it resolves as a warning naming it, never as a
// plain success. The job row shows the same warning (jobDetail.js).

/**
 * @param {any} job
 * @returns {{ kind: 'success' | 'warning', description: string, duration: number }}
 */
export function completionToast(job) {
  const output = job?.output_path ? `→ ${job.output_path}` : 'Completed';
  if (job?.warning) {
    return { kind: 'warning', description: job.warning, duration: 8000 };
  }
  return { kind: 'success', description: output, duration: 4000 };
}
