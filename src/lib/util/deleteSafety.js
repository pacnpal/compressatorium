// What the delete dialogs say and allow (#593). Pure (no DOM, no Svelte), so
// the Node tests load it as it ships.
//
// An original whose new copy exists but hasn't passed a check is the one a
// delete can't be sure about. By default the dialogs refuse it and offer to
// check the copies; only the Settings → Defaults opt-out "Allow deleting
// originals whose new copy isn't checked" brings back the old acknowledgement.

import { formatSize } from '../api/format.js';

/**
 * The originals among *entries* whose existing copy hasn't passed a check:
 * `[{ source, outputs: [path] }]`. `isChecked(path)` answers for one copy.
 */
export function uncheckedCopies(entries, isChecked) {
  const out = [];
  for (const e of entries ?? []) {
    if (!e || typeof e !== 'object' || !e.path) continue;
    const outputs = (Array.isArray(e.outputs) ? e.outputs : [])
      .filter((o) => o?.exists && o.path && !isChecked(o.path))
      .map((o) => o.path);
    if (outputs.length) out.push({ source: e.path, outputs });
  }
  return out;
}

/**
 * Whether a delete may go ahead: `clear` (nothing unsure), `refuse` (an
 * unchecked copy and no opt-out) or `ask` (the opt-out is on: the old
 * acknowledgement decides).
 */
export function deleteGate({ unchecked = 0, allowUnchecked = false, acknowledged = false } = {}) {
  if (!unchecked) return { state: 'clear', canDelete: true };
  if (!allowUnchecked) return { state: 'refuse', canDelete: false };
  return { state: 'ask', canDelete: !!acknowledged };
}

/** A path as the dialogs show it: the file's name, and the folder it is in. */
export function splitPath(path) {
  const text = String(path ?? '');
  const cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
  return cut < 0
    ? { name: text, folder: '' }
    : { name: text.slice(cut + 1), folder: text.slice(0, cut) };
}

/** The bytes a delete frees: the sizes the listing knows, added up. */
export function freedBytes(entries) {
  let total = 0;
  for (const e of entries ?? []) {
    if (typeof e?.size === 'number' && e.size > 0) total += e.size;
  }
  return total;
}

/**
 * "Its DOSZ copy checked out (400 B), so the game stays in your library."
 * `copy` is `{ format, size }` for a copy that passed its check, else null.
 */
export function checkedCopyLine(copy) {
  if (!copy) return null;
  const size = typeof copy.size === 'number' ? ` (${formatSize(copy.size)})` : '';
  return `Its ${copy.format ?? 'new'} copy checked out${size}, so the game stays in your library.`;
}
