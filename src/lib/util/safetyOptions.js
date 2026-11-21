// What the safety choices (check each new file, delete the original, quick
// check) offer for a set of mode specs, and how one change moves the others.
// SafetyOptions.svelte draws it; pure, so the Node tests load it as it ships.
//
// A spec may carry `games` (how many games of the plan use it); it counts 1
// otherwise.

const KEPT_FALLBACK = "this format can't be checked well enough";

/**
 * @param {object[]} specs
 * @param {{ deleteOriginal: boolean }} state
 */
export function safetyControls(specs, { deleteOriginal = false } = {}) {
  const list = (specs ?? []).filter(Boolean);
  const canDelete = list.filter((s) => s.supportsDeleteOnVerify);
  const kept = list.filter((s) => !s.supportsDeleteOnVerify);
  const keptGames = kept.reduce((n, s) => n + (s.games ?? 1), 0);
  return {
    showCheck: list.some((s) => s.supportsVerify || s.supportsDeleteOnVerify),
    showDelete: canDelete.length > 0,
    // Some can delete and some can't: say which are kept, and why.
    kept: canDelete.length > 0 && kept.length > 0
      ? { games: keptGames, reason: kept.find((s) => s.sourceKeptReason)?.sourceKeptReason ?? KEPT_FALLBACK }
      : null,
    showQuick: deleteOriginal && list.some((s) => s.supportsRoundTrip && !s.roundTripRequired),
    // Deleting always checks first, so the check can't be turned off then.
    checkLocked: deleteOriginal,
  };
}

/**
 * The state after one change. Turning delete on turns the check on; turning
 * delete off drops the quick check, which only means anything with delete on.
 */
export function applySafety(state, patch) {
  const next = { ...state, ...patch };
  if (next.deleteOriginal) next.checkAfter = true;
  else next.quickCheck = false;
  return next;
}
