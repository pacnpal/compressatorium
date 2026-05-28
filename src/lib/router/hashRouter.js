// Hash-based router. Reads location.hash, normalizes, and writes to ui store.
// Components consume ui.activeView / ui.workspaceTool, never the URL directly.

import { ui } from '$lib/stores/ui.svelte.js';

function apply() {
  const hash = window.location.hash || '#/dashboard';
  ui.applyHash(hash);
}

export function startRouter() {
  if (typeof window === 'undefined') return () => undefined;
  apply();
  if (!window.location.hash) {
    // Normalize the bare URL so the back button has somewhere to return to.
    window.history.replaceState(null, '', '#/dashboard');
  }
  window.addEventListener('hashchange', apply);
  return () => window.removeEventListener('hashchange', apply);
}
