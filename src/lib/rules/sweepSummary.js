// What a Preview or Run now did, in one line (#520). An empty run says why
// nothing ran, never just "0 queued".

const STOPPED = {
  no_rules: 'no Rules are set up yet: pick a target for a console',
  not_configured: 'this Library is not set up to be read',
  library_unresponsive: "the Library's folder did not answer",
  queue_full: 'the queue is full',
  stopped: 'the run was stopped',
};

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/** Why a run queued and checked nothing. */
export function nothingReason(r) {
  const stopped = STOPPED[r?.stopped_reason];
  if (stopped) return stopped;
  const considered = r?.considered ?? 0;
  const paused = r?.skipped_paused ?? 0;
  if (r?.errors?.length) return 'a Rule could not run (see below)';
  if (paused && !considered) {
    return `${plural(paused, 'Rule is', 'Rules are')} off: tick Enabled on it`;
  }
  if (!considered) return 'no files match the Rules';
  if ((r?.skipped_existing ?? 0) === considered) {
    return `all ${considered} are already converted`;
  }
  if ((r?.skipped_active ?? 0) === considered) {
    return `all ${considered} are already in the queue`;
  }
  return 'every file was skipped (see the counts)';
}

/** "Queued 3 conversions, checking 12 Images", or why there are none. */
export function sweepToast(r, queuedVerb, checkVerb) {
  const parts = [];
  const n = r?.queued ?? 0;
  const c = r?.checks ?? 0;
  if (n) parts.push(`${queuedVerb} ${plural(n, 'conversion', 'conversions')}`);
  if (c) parts.push(`${checkVerb.toLowerCase()} ${plural(c, 'Image', 'Images')}`);
  if (!parts.length) return `Nothing to convert: ${nothingReason(r)}`;
  const text = parts.join(', ');
  return text[0].toUpperCase() + text.slice(1);
}
