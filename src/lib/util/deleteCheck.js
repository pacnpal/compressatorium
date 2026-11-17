// Which check a delete-source job must pass before its source is removed.
// Mirrors JobManager._delete_check on the backend (round_trip.delete_check_level):
// a mode whose proof is Recognized (#423) looks its output's hash up in the
// DATs and Hasheous, whatever the container-only box says; otherwise the
// Round-trip runs only when the mode supports it and the job did not take the
// container-only opt-out. The convert panel and the delete confirmation both
// read this, so neither can promise a check the job will not run.

/**
 * @param {{ supportsRoundTrip: boolean, containerOnly: boolean, deleteProof?: string }} opts
 * @returns {'recognized' | 'round_tripped' | 'intact'}
 */
export function deleteCheckFor({ supportsRoundTrip, containerOnly, deleteProof }) {
  if (deleteProof === 'recognized') return 'recognized';
  return supportsRoundTrip && !containerOnly ? 'round_tripped' : 'intact';
}

// What the delete option says each check means, in the user's words.
export const DELETE_CHECK_HINTS = Object.freeze({
  recognized:
    'The output must be Recognized (its hash is in one of your DATs or in Hasheous) '
    + 'before sources are deleted. It can\'t be Round-tripped, so if nothing recognizes '
    + 'it, the source is kept and the job says so.',
  round_tripped:
    'The output must be Round-tripped (it decodes to the same content as the source) '
    + 'before sources are deleted. Sources are kept if it is not.',
  intact:
    'The output must be Intact (it passes its own container check) before sources '
    + 'are deleted. Sources are kept if it is not.',
});
