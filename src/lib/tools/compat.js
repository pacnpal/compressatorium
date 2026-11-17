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
 * An empty selection is not "nothing": the tool then uses its own codecs. The
 * served `table` lists them per mode (`implicit_codecs`), so those are checked;
 * a mode whose defaults depend on the input (`implicit_codecs_unknown`) cannot
 * be checked, so the warning says so and suggests choosing codecs. This file
 * keeps no list of its own.
 */
export function compatWarning(target, mode, codecs, table) {
  const entry = compatEntry(target, mode);
  if (!entry) return null;
  const chosen = (codecs ?? []).filter(Boolean);
  if (chosen.length === 0 && table?.implicit_codecs_unknown?.includes(mode)) {
    return `With no codec chosen, whether ${target.label} can read the result can't be confirmed: the tool picks codecs from the input. Choose codecs explicitly (it reads ${entry.codecs.join(', ')}).`;
  }
  const fallback = chosen.length === 0 ? (table?.implicit_codecs?.[mode] ?? []) : [];
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
