// An edit field that follows a saved value without losing what the user typed
// (#387: the ES-DE gamelists folder in the Rules editor).
//
// `state` is { draft, seen }: the field's text, and the saved value it last
// adopted (null before the first). `saved` is the saved value now. The field
// takes a new saved value only while it holds no edit of its own (its text is
// the value it last adopted). An unrelated save that re-reports the same value
// changes nothing, and the answer to an earlier save never replaces newer
// typing. Pure, so the store tests can run it with Node.

/**
 * @param {{ draft: string, seen: string } | null} state
 * @param {string} saved
 * @returns {{ draft: string, seen: string }}
 */
export function followSaved(state, saved) {
  if (state === null) return { draft: saved, seen: saved };
  if (saved === state.seen) return state;
  return { draft: state.draft === state.seen ? saved : state.draft, seen: saved };
}
