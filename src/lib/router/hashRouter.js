// Hash-based router. Reads location.hash, moves an old route to its new home,
// and writes the result into the ui store. Components consume ui.activeView
// and its routing fields, never the URL directly. The route table is routes.js.

import { ui } from '$lib/stores/ui.svelte.js';
import { DEFAULT_HASH, legacyRedirect } from './routes.js';

function apply() {
  const hash = window.location.hash;
  // An old bookmark (or the bare URL) is replaced, not pushed, so Back never
  // returns to a route that only redirects again.
  const redirect = legacyRedirect(hash);
  if (redirect) window.history.replaceState(null, '', redirect);
  ui.applyHash(redirect ?? hash ?? DEFAULT_HASH);
}

export function startRouter() {
  if (typeof window === 'undefined') return () => undefined;
  apply();
  window.addEventListener('hashchange', apply);
  return () => window.removeEventListener('hashchange', apply);
}
