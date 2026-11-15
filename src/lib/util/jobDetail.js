// The detail lines under a job row, by state. Kept out of JobRow.svelte so it
// can be tested on its own (tests/test_frontend_job_warning.py).
//
// A completed job shows its output path, then its warning if it has one: the
// backend keeps a tool's caveat on `job.warning` (an eXoDOS conf that points
// outside its game, a Source kept for a reader), so a finished job that did
// less than asked does not read as a plain success.
//
// A delete-on-verify job that finished without deleting anything kept its
// Source, and its message says why (#295). Every such ending now also sets
// `job.warning` (#306, so the CLI reports it too), so the reason is shown once:
// as the warning, or, for a job without one, as the kept line.

/**
 * @param {any} job
 * @returns {{ kind: 'message' | 'error' | 'output' | 'warning' | 'kept', text: string }[]}
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
    else if (sourceKept(job)) lines.push({ kind: 'kept', text: job.message });
    return lines;
  }
  return [];
}

/** Delete-on-verify finished but deleted nothing: the Source was kept. */
function sourceKept(job) {
  return !!job?.delete_on_verify && !(job?.deleted_paths?.length) && !!job?.message;
}
