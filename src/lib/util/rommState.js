// What RomM's page says about its connection (#566). Kept out of the
// components so it can be tested on its own
// (tests/test_frontend_romm_unreachable_566.py).

function host(url) {
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
}

/**
 * The RomM page's one-line state: set up or not, and whether it answers.
 * Empty until the status has been read.
 *
 * @param {{ configured?: boolean, connected?: boolean } | null} status
 * @param {string} [url]
 */
export function rommWhere(status, url) {
  if (!status) return '';
  if (!status.configured) return 'Not connected';
  const at = host(url);
  if (status.connected) return at ? `Connected to ${at}` : 'Connected';
  return at ? `Not answering at ${at}` : 'Not answering';
}

/**
 * Why RomM's catalog can't be shown. The backend's error is a whole sentence
 * that names the field to fix (`_safe_error`), so it is shown as it is.
 *
 * @param {{ error?: string | null } | null} status
 */
export function rommProblem(status) {
  return status?.error ? String(status.error) : "Couldn't reach RomM.";
}
