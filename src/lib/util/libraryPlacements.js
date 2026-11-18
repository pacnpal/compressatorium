// Where new Images go, kept with the Library they were loaded for (#464).
//
// The Rules view switches Libraries under one panel. Each answer carries the
// Library id it was asked for, and the panel shows it only while that Library
// is selected, so a switch never shows the previous Library's rows, and an
// answer for it that lands after the switch is shown nowhere.

import { LatestOnly } from './latestOnly.js';

// routes/libraries.py LISTING_IN_PROGRESS.
const LISTING_IN_PROGRESS = 'listing_in_progress';

export class PlacementsLoad {
  #latest = new LatestOnly();
  #request;

  constructor(request) {
    this.#request = request;
  }

  // Resolves to `{ id, placements, error }`, or null when there is nothing to
  // show: a later load started (only the latest counts), or another listing
  // of the folder is still running (a wait; the panel loads again once it ends).
  async load(id) {
    const result = await this.#latest.run(() => this.#request(id));
    if (!result.current) return null;
    if (result.error) {
      const e = result.error;
      if (e?.status === 409 && e?.message === LISTING_IN_PROGRESS) return null;
      return { id, placements: [], error: e?.message ?? 'Could not read where Images go' };
    }
    const data = result.value;
    const placements = Array.isArray(data?.placements) ? data.placements : [];
    return { id, placements, error: null };
  }
}

// The loaded answer when it is for `libraryId`, else null (still loading).
export function placementsOf(loaded, libraryId) {
  return loaded && loaded.id === libraryId ? loaded : null;
}
