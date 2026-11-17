// Only the latest of several overlapping requests counts.
//
// Each `run(request)` starts a new request and resolves, never rejects, to
// `{ current, value }` or `{ current, error }`. `current` is false once a later
// `run` has started, so a caller drops a superseded answer (its data, its
// error and its loading state alike) however the answers arrive.

export class LatestOnly {
  #latest = 0;

  async run(request) {
    const ticket = ++this.#latest;
    try {
      const value = await request();
      return { current: ticket === this.#latest, value };
    } catch (error) {
      return { current: ticket === this.#latest, error };
    }
  }
}
