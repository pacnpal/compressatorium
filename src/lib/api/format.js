// Pure format helpers (no DOM, no network).

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

/** The last part of a path: what a toast or a job row names a file by. */
export function baseName(path) {
  const parts = String(path ?? '').split(/[/\\]/).filter(Boolean);
  return parts.at(-1) ?? '';
}

/**
 * What a job row or toast calls a job: its game's file. An external job (no
 * mode spec of its own: a watch folder's copy, a check Rule's run) has a
 * placeholder path, so its own `filename` names it (#565).
 *
 * @param {any} job
 * @param {{ external?: boolean }} [opts]
 */
export function jobName(job, { external = false } = {}) {
  if (external && job?.filename) return job.filename;
  return baseName(job?.file_path);
}

export function formatSize(bytes) {
  if (bytes == null) return '';
  if (bytes === 0) return '0 B';
  // Negative sizes are real (a delta that grew), and Math.log can't take them.
  if (bytes < 0) return `-${formatSize(-bytes)}`;
  const k = 1024;
  const i = Math.max(
    0,
    Math.min(Math.floor(Math.log(bytes) / Math.log(k)), SIZE_UNITS.length - 1),
  );
  return `${Number.parseFloat((bytes / k ** i).toFixed(2))} ${SIZE_UNITS.at(i)}`;
}
