// "Compatible with..." rules (#429), shared by the compression picker and the
// Rule editor. The table itself is served by /api/compat-presets (one YAML file
// on the server); nothing here names an emulator, mode or codec. No imports.

/** The entry for `mode` in `target`, or null when the target has none. */
export function compatEntry(target, mode) {
  return target?.entries?.find((e) => e.mode === mode) ?? null;
}

/** The targets (from `table.targets`) that have an entry for `mode`. */
export function compatTargetsForMode(table, mode) {
  return (table?.targets ?? []).filter((t) => compatEntry(t, mode));
}

/**
 * The warning for codecs a target does not read, or null. Advisory only: a
 * caller never disables anything on it. "none" is never warned about, and a
 * mode the target has no entry for gives no opinion.
 *
 * An empty selection is not "nothing": the tool then uses its own codecs, which
 * the server lists per mode in `implicit` (the table's `implicit_codecs`), so
 * those are checked instead. This file keeps no list of its own.
 */
export function compatWarning(target, mode, codecs, implicit) {
  const entry = compatEntry(target, mode);
  if (!entry) return null;
  const chosen = (codecs ?? []).filter(Boolean);
  const fallback = chosen.length === 0 ? (implicit?.[mode] ?? []) : [];
  const unread = (chosen.length ? chosen : fallback).filter(
    (c) => c !== 'none' && !entry.codecs.includes(c),
  );
  if (unread.length === 0) return null;
  const list = unread.join(', ');
  const lead = fallback.length
    ? `With no codec chosen the tool uses ${fallback.join(', ')}, and ${target.label} may not read ${list}.`
    : `${target.label} may not read ${list}.`;
  return `${lead} It reads ${entry.codecs.join(', ')}.`;
}
