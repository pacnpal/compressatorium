// Settings saves that run one at a time per key, in the order they were asked
// for, where only the latest save's answer is adopted.
//
// Two saves in flight together can be applied by the backend in either order,
// and whichever answer arrives last would otherwise leave the screen showing
// settings older than the ones the server stored. Shared by the folder
// Library store and the RomM store.

export class LatestSaveQueue {
  // key -> ticket of the latest save asked for.
  #latest = new Map();
  // key -> tail of its queue; always resolves, so one failure does not stop it.
  #tails = new Map();

  /**
   * Queue `task(ticket)` behind *key*'s earlier saves and return its promise,
   * which settles with the task's own result or error. The queue only orders:
   * each caller still sees its own save fail.
   */
  schedule(key, task) {
    const ticket = (this.#latest.get(key) ?? 0) + 1;
    this.#latest.set(key, ticket);
    const previous = this.#tails.get(key) ?? Promise.resolve();
    const turn = previous.then(() => task(ticket));
    this.#tails.set(key, turn.catch(() => {}));
    return turn;
  }

  /** True while *ticket* is still the latest save asked for under *key*. */
  isLatest(key, ticket) {
    return this.#latest.get(key) === ticket;
  }
}
