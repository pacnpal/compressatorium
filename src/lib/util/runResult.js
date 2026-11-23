// What a finished conversion did, in numbers (#595): the space it saved, and
// what deleting a kept original would still save. Pure (no DOM, no Svelte, no
// registry), so the Node tests load it as it ships.
//
// Sizes are signed, as the Ledger's own `job_saved_bytes` keeps them: a game
// that grew counts against one that shrank, so the line agrees with History.

import { formatSize } from '../api/format.js';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

// A completed job whose sizes are both known: how much smaller it made the
// game (negative when the new file is larger). Null when it can't be told.
function delta(job) {
  if (job?.status !== 'completed') return null;
  if (typeof job.input_bytes !== 'number' || typeof job.output_size !== 'number') return null;
  return job.input_bytes - job.output_size;
}

function deletedOriginal(job) {
  return Array.isArray(job?.deleted_paths) && job.deleted_paths.length > 0;
}

/** Space a job saved, signed: the Ledger's definition, a completed job that deleted its original. */
export function jobSaved(job) {
  return deletedOriginal(job) ? (delta(job) ?? 0) : 0;
}

/**
 * What deleting a job's kept original would still save: only where its mode
 * may delete the original (`canDeleteSource(mode)`, from the registry), and
 * only when the new file is smaller. 0 otherwise.
 */
export function jobStillToSave(job, canDeleteSource = () => false) {
  if (deletedOriginal(job) || !canDeleteSource(job?.mode)) return 0;
  return Math.max(0, delta(job) ?? 0);
}

/** How much smaller (positive) or larger (negative) a kept original's copy is. */
export function jobKeptDelta(job) {
  return deletedOriginal(job) ? 0 : (delta(job) ?? 0);
}

/** Whether the job's new file passed a check. */
export function jobChecked(job) {
  return !!job?.check_passed && job.check_passed !== 'none';
}

/** "saved 2 MB" or "1 MB larger" for a signed total; null for none. */
export function savedPhrase(bytes) {
  if (bytes > 0) return `saved ${formatSize(bytes)}`;
  if (bytes < 0) return `${formatSize(-bytes)} larger`;
  return null;
}

/**
 * Keep each of a run's jobs as it finishes (#596): a batch bigger than the job
 * history loses its early jobs from the live list while later ones still run.
 * `records` (id → the finished job) is the caller's, kept across calls.
 */
export function recordFinished(ids, jobList, records) {
  const wanted = new Set(ids ?? []);
  for (const job of jobList ?? []) {
    if (wanted.has(job?.id) && TERMINAL.has(job.status)) records[job.id] = { ...job };
  }
}

/**
 * A run's jobs as they stand: each one live, else as it was kept when it
 * finished. One gone from both was finished and pruned before it was seen,
 * so it is left out rather than holding the run open.
 */
export function runJobs(ids, jobList, records = {}) {
  const live = {};
  for (const job of jobList ?? []) if (job?.id != null) live[job.id] = job;
  return (ids ?? []).map((id) => live[id] ?? records[id]).filter(Boolean);
}

function sum(list, fn) {
  return list.reduce((n, job) => n + fn(job), 0);
}

function gamesWord(n) {
  return n === 1 ? '1 game' : `${n} games`;
}

// "2 games converted and checked", or how many of them were checked.
function convertedHead(done) {
  const checked = done.filter(jobChecked).length;
  if (checked === done.length) return `${gamesWord(done.length)} converted and checked`;
  if (checked === 0) return `${gamesWord(done.length)} converted`;
  return `${gamesWord(done.length)} converted, ${checked} checked`;
}

/**
 * One line for a run once every job in it has finished, else null:
 * "2 games converted and checked · saved 4 MB · 1 failed". Deleting kept
 * originals is suggested only for modes that may delete them
 * (`canDeleteSource`); others say only the size difference.
 */
export function runResult(jobs, { canDeleteSource = () => false } = {}) {
  const list = (jobs ?? []).filter(Boolean);
  if (!list.length || !list.every((job) => TERMINAL.has(job.status))) return null;
  const done = list.filter((job) => job.status === 'completed');
  const failed = list.filter((job) => job.status === 'failed').length;
  const cancelled = list.filter((job) => job.status === 'cancelled').length;
  const more = sum(done, (job) => jobStillToSave(job, canDeleteSource));
  const parts = [
    done.length ? convertedHead(done) : 'Nothing converted',
    savedPhrase(sum(done, jobSaved)),
    more > 0 ? `${formatSize(more)} more to save by deleting the originals` : null,
    failed ? `${failed} failed` : null,
    cancelled ? `${cancelled} cancelled` : null,
  ];
  return parts.filter(Boolean).join(' · ');
}
