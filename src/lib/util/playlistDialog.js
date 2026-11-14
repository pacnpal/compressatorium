// Pure state rules for the Create .m3u dialog (PlaylistModal.svelte).

/**
 * Whether Create may be pressed. It must write the name the user sees, so the
 * shown title has to be the one the current preview was made for (the preview
 * lags typing by a debounce), and nothing may be loading, writing or invalid.
 */
export function canCreate({ preview, title, previewedTitle, loading, busy, titleError }) {
  return !!preview && !loading && !busy && !titleError && title === previewedTitle;
}
