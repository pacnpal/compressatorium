// The detail lines under a job row, by state. Kept out of JobRow.svelte so it
// can be tested on its own (tests/test_frontend_job_warning.py).
//
// A completed job shows its output path, then its warning if it has one: the
// backend keeps a tool's caveat on `job.warning` (an eXoDOS conf that points
// outside its game, a Source kept for a reader), so a finished job that did
// less than asked does not read as a plain success.

/**
 * @param {any} job
 * @returns {{ kind: 'message' | 'error' | 'output' | 'warning', text: string }[]}
 */
export function jobDetailLines(job) {
  const status = job?.status;
  if (status === 'queued' || status === 'processing') {
    return job?.message ? [{ kind: 'message', text: job.message }] : [];
  }
  if (status === 'failed') {
    return job?.error_message ? [{ kind: 'error', text: job.error_message }] : [];
  }
  if (status === 'completed') {
    const lines = [];
    if (job?.output_path) lines.push({ kind: 'output', text: `→ ${job.output_path}` });
    if (job?.warning) lines.push({ kind: 'warning', text: job.warning });
    return lines;
  }
  return [];
}
