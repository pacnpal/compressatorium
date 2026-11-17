// The "Compatible with…" table (#429), fetched once from the server, which owns
// it (app/services/compat_targets.yml); the frontend keeps no copy. Shared by
// the compression picker and the Rule editor.

import { api } from '$lib/api/endpoints.js';

class CompatTargetsStore {
  targets = $state([]);
  #loading = null;

  /** Load the table once; a failure leaves it empty and the next call retries. */
  load() {
    this.#loading ??= api.getCompatTargets()
      .then((data) => { this.targets = data?.targets ?? []; })
      .catch(() => { this.#loading = null; });
    return this.#loading;
  }

  /** The entry of target `id` that covers `mode`, or null. */
  entryFor(id, mode) {
    const target = this.targets.find((t) => t.id === id);
    return target?.entries.find((e) => e.modes.includes(mode)) ?? null;
  }
}

export const compatTargets = new CompatTargetsStore();

/**
 * Why `codecs` may not load on `target` in `mode`, or null when nothing is
 * known against them. `codecs` is what the job runs with, or null for "the
 * tool's own default", which the table cannot vouch for. Never blocks: the
 * caller shows it as a warning.
 */
export function compatWarning(target, entry, codecs) {
  if (!target) return null;
  if (!entry) return `No cited source says ${target.label} reads this mode.`;
  const readable = entry.codecs ?? [];
  if (!readable.length) return null;
  if (codecs == null) {
    return `${target.label} is only confirmed to read ${readable.join(', ')}; the tool's default isn't checked.`;
  }
  const outside = codecs.filter((c) => !readable.includes(c));
  if (!outside.length) return null;
  return `${target.label} isn't known to read ${outside.join(', ')}; it reads ${readable.join(', ')}.`;
}
