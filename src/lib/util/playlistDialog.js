// Pure state rules for the Create .m3u dialog (PlaylistModal.svelte).

/**
 * Whether Create may be pressed. It must write the name the user sees, so the
 * shown title has to be the one the current preview was made for (the preview
 * lags typing by a debounce), and nothing may be loading, writing or invalid.
 */
export function canCreate({ preview, title, previewedTitle, loading, busy, titleError }) {
  return !!preview && !loading && !busy && !titleError && title === previewedTitle;
}

/**
 * Sort a failed preview. `invalid_name` (the playlist name was refused) keeps
 * the name field, prefilled with the refused title or what the user typed;
 * anything else is a selection error with nothing to edit.
 * @returns {{kind: 'name'|'selection', message: string, title: string|null}}
 */
export function classifyPreviewError(err, requestedTitle) {
  const message = err?.message ?? 'Failed to preview the playlist';
  if (err?.detail?.code === 'invalid_name') {
    return { kind: 'name', message, title: requestedTitle ?? err.detail.title ?? '' };
  }
  return { kind: 'selection', message, title: null };
}

/**
 * The folder of a server path. Paths come from a POSIX server, so only `/`
 * separates folders: a backslash is an ordinary file name character.
 */
export function parentDir(path) {
  return path.slice(0, path.lastIndexOf('/'));
}

/**
 * The paths a playlist can be made from: two or more, all in one folder.
 * Whether they are disc Images is checked by the caller (registry discImages)
 * and, finally, by the server.
 */
export function playlistPaths(paths) {
  return paths.length >= 2 && new Set(paths.map(parentDir)).size === 1 ? paths : [];
}
