// Which listing rows the DAT matcher can identify: the one rule the file
// list's match hydration and the RomM view's "Recognize these" share (#427).
//
// No imports, so it can be evaluated on its own; the caller passes the
// extensions the registry lets a user filter by (`registry.allFilterableExts()`).

/** Archive containers. The matcher hashes their compressed bytes, which no DAT
 * lists, so an archived ROM would be recorded as a miss (Codex on PR #442). */
export const ARCHIVE_EXTS = new Set(['.7z', '.zip']);

/**
 * Whether the DAT matcher can identify *entry*, a listing row.
 *
 * Not a folder or an archive (by type or by extension: a RomM listing reports
 * a zip as a plain file), not a folded split-ISO set (its path is only the
 * `.0` part, so a hash of it is a false miss), and an extension the matcher
 * can plausibly know (*matchableExts*): manifests, artwork and logs would be
 * hashed and stored as misses.
 *
 * @param {{ path?: string, type?: string, extension?: string | null,
 *   split_parts?: number | null }} entry
 * @param {Iterable<string>} matchableExts
 */
export function isDatMatchable(entry, matchableExts) {
  if (!entry?.path || entry.type === 'directory' || entry.type === 'archive') return false;
  if (entry.split_parts != null) return false;
  const ext = (entry.extension ?? '').toLowerCase();
  if (!ext || ARCHIVE_EXTS.has(ext)) return false;
  for (const known of matchableExts) {
    if (known.toLowerCase() === ext) return true;
  }
  return false;
}
