// What a batch delete's answer tells the person (#563). Kept out of
// BulkDeleteModal.svelte so it can be tested on its own
// (tests/test_frontend_delete_outcome_563.py).
//
// /files/delete-batch answers { total, success, failed, results }. A file it
// keeps (a sheet or playlist still needs it, a check that couldn't be made)
// carries the reason in its result's `error`, and that reason is the news: a
// refused delete names it rather than counting failures.

const SHOWN_REASONS = 3;

function sentence(text) {
  return /[.!?…]$/.test(text) ? text : `${text}.`;
}

/**
 * @param {any} result - the delete-batch answer
 * @param {string[]} requested - the paths that were asked for
 * @returns {{ kind: 'success' | 'warning', title: string, description: string | null,
 *   removed: string[] }}
 */
export function deleteOutcome(result, requested) {
  const results = Array.isArray(result?.results) ? result.results : null;
  const failed = result?.failed ?? 0;
  // `deleted` doesn't exist in the answer, so a partial success is counted
  // from `success`, never assumed to be every path that was asked for.
  const success = typeof result?.success === 'number' ? result.success : requested.length - failed;
  // What went, so its check badge is forgotten. An answer without results
  // (an older shape) forgets every requested path.
  const removed = results ? results.filter((r) => r?.success).map((r) => r.path) : [...requested];
  if (failed === 0) {
    return {
      kind: 'success',
      title: `Deleted ${success} file${success === 1 ? '' : 's'}`,
      description: null,
      removed,
    };
  }
  const reasons = [...new Set(
    (results ?? []).filter((r) => r && !r.success && r.error).map((r) => String(r.error)),
  )];
  const lines = reasons.slice(0, SHOWN_REASONS);
  if (reasons.length > SHOWN_REASONS) lines.push(`…and ${reasons.length - SHOWN_REASONS} more`);
  return {
    kind: 'warning',
    title: success > 0
      ? `Deleted ${success}; kept ${failed}`
      : `Kept ${failed} file${failed === 1 ? '' : 's'}`,
    description: lines.length ? lines.map(sentence).join(' ') : null,
    removed,
  };
}
