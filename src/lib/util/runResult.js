// What a finished conversion did, in numbers (#595): the space it saved and
// what deleting a kept original would still save. Pure (no DOM, no Svelte),
// so the Node tests load it as it ships.

import { formatSize } from '../api/format.js';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

// A completed job whose sizes are both known: the bytes it made smaller.
function shrink(job) {
  if (job?.status !== 'completed') return 0;
  if (typeof job.input_bytes !== 'number' || typeof job.output_size !== 'number') return 0;
  return Math.max(0, job.input_bytes - job.output_size);
}

function deletedOriginal(job) {
  return Array.isArray(job?.deleted_paths) && job.deleted_paths.length > 0;
}

/** Space a job saved: the Ledger's definition, a completed job that deleted its original. */
export function jobSaved(job) {
  return deletedOriginal(job) ? shrink(job) : 0;
}

/** What deleting a job's kept original would still save. */
export function jobStillToSave(job) {
  return deletedOriginal(job) ? 0 : shrink(job);
}

/** Whether the job's new file passed a check. */
export function jobChecked(job) {
  return !!job?.check_passed && job.check_passed !== 'none';
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
 * "2 games converted and checked · saved 4 MB · 1 failed".
 */
export function runResult(jobs) {
  const list = (jobs ?? []).filter(Boolean);
  if (!list.length || !list.every((job) => TERMINAL.has(job.status))) return null;
  const done = list.filter((job) => job.status === 'completed');
  const failed = list.filter((job) => job.status === 'failed').length;
  const cancelled = list.filter((job) => job.status === 'cancelled').length;
  const saved = sum(done, jobSaved);
  const more = sum(done, jobStillToSave);
  const parts = [done.length ? convertedHead(done) : 'Nothing converted'];
  if (saved > 0) parts.push(`saved ${formatSize(saved)}`);
  if (more > 0) parts.push(`${formatSize(more)} more to save by deleting the originals`);
  if (failed) parts.push(`${failed} failed`);
  if (cancelled) parts.push(`${cancelled} cancelled`);
  return parts.join(' · ');
}
