// Which check a delete-source job must pass before its source is removed.
// Mirrors JobManager._delete_check on the backend: the Round-trip runs only
// when the mode supports it and the job did not take the container-only
// opt-out. The convert panel and the delete confirmation both read this, so
// neither can promise a check the job will not run.

/**
 * @param {{ supportsRoundTrip: boolean, containerOnly: boolean }} opts
 * @returns {'round_tripped' | 'intact'}
 */
export function deleteCheckFor({ supportsRoundTrip, containerOnly }) {
  return supportsRoundTrip && !containerOnly ? 'round_tripped' : 'intact';
}
