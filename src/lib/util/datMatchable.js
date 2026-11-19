// Which listing rows the DAT matcher can identify: the one rule the file
// list's match hydration and the RomM view's "Recognize these" share (#427).
//
// No imports, so it can be evaluated on its own; the caller passes the
// extensions the registry lets a user filter by (`registry.allFilterableExts()`).

/** Archive containers. The matcher hashes their compressed bytes, which no DAT
 * lists, so an archived ROM would be recorded as a miss (Codex on PR #442).
 * `.rar` too: a RAR set's later volume lists as a plain file row (#369), and
 * the rar tool makes `.rar` a source extension (#476). */
export const ARCHIVE_EXTS = new Set(['.7z', '.zip', '.rar']);

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
/**
 * Whether the app wants Hasheous provider ids for RomM ROMs, by RomM's
 * *settings*: the switch is on and so is re-pinning, which carries them. The
 * same gate as the backend's `repin.hasheous_write_on` (CodeRabbit on PR #442).
 *
 * @param {{ hasheous_write_ids?: boolean, repin_enabled?: boolean } | null} settings
 */
export function wantsHasheousIds(settings) {
  return settings?.hasheous_write_ids === true && settings?.repin_enabled !== false;
}

/**
 * Whether a row whose cached match is *match* should go to the match job: it
 * has no answer yet, or (when *wantsIds*) it is a Hasheous hit cached before
 * the provider ids were kept, which the job looks up again (#427). A cached
 * miss is not asked again.
 *
 * @param {{ matched?: boolean, source?: string } | null} match
 * @param {boolean} wantsIds
 */
export function needsDatMatch(match, wantsIds) {
  if (match == null) return true;
  return Boolean(
    wantsIds && match.matched && match.source === 'hasheous' && !('provider_ids' in match),
  );
}

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
