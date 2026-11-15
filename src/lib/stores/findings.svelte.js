// Findings (#304): what needs a person's decision, and the folder Platform
// mappings that settle most of them.
//
// The backend keeps one open Finding per kind and Image (Platform conflict,
// Unsorted), written by each folder Library's walk. Dismissing one hides it
// until its cause changes. Nothing here moves or deletes a file: each Finding
// links to the action that fixes it (open its folder, set the folder's
// Platform).

import { api } from '$lib/api/endpoints.js';

class FindingsStore {
  list = $state([]);
  // How many Findings a dismiss is hiding right now.
  dismissed = $state(0);
  showDismissed = $state(false);
  loading = $state(false);
  error = $state(null);
  checking = $state(false);
  platforms = $state([]);
  // The folder the mapping dialog is open on: `{ path, name }`, or null.
  mappingTarget = $state(null);
  // The open dialog's read of that folder's current Platform: `loading`,
  // `loaded` (`mappingCurrent` is the slug, or null for none set) or `error`
  // (`mappingError` says why; "couldn't load" is never shown as "none set").
  mappingCurrent = $state(null);
  mappingState = $state('idle');
  mappingError = $state(null);
  // Bumped by every opening, closing and retry: only the read that matches
  // writes, so a response (or failure) from an earlier opening is dropped.
  #mappingRead = 0;
  // Set when a mapping was saved while its Library was mid-walk: the
  // backend's note that the Findings refresh after that walk. Check now
  // clears it.
  pending = $state(null);
  // Only the latest listing request may write the list (rapid toggles).
  #request = 0;

  async load() {
    const request = ++this.#request;
    this.loading = true;
    this.error = null;
    try {
      const data = await api.listFindings(this.showDismissed);
      if (request !== this.#request) return this.list;
      this.list = Array.isArray(data?.findings) ? data.findings : [];
      this.dismissed = data?.dismissed ?? 0;
    } catch (e) {
      if (request === this.#request) this.error = e?.message ?? 'Failed to list the Findings';
    } finally {
      if (request === this.#request) this.loading = false;
    }
    return this.list;
  }

  #fingerprint(id) {
    return this.list.find((f) => f.id === id)?.fingerprint ?? null;
  }

  /**
   * A mutation succeeded: drop every listing that started before it (its
   * snapshot predates the change), then list again.
   */
  #superseded() {
    this.#request += 1;
  }

  /** Hide one Finding until its cause changes. */
  async dismiss(id) {
    await api.dismissFinding(id, this.#fingerprint(id));
    this.#superseded();
    if (this.showDismissed) {
      this.list = this.list.map((f) => (f.id === id ? { ...f, dismissed: true } : f));
    } else {
      this.list = this.list.filter((f) => f.id !== id);
    }
    this.dismissed += 1;
    await this.load();
  }

  /** Show a dismissed Finding again. */
  async undismiss(id) {
    await api.undismissFinding(id, this.#fingerprint(id));
    this.#superseded();
    this.list = this.list.map((f) => (f.id === id ? { ...f, dismissed: false } : f));
    this.dismissed = Math.max(0, this.dismissed - 1);
    await this.load();
  }

  /** Walk every folder Library now, then list what it found. */
  async check() {
    this.checking = true;
    try {
      const result = await api.checkFindings();
      this.pending = null;
      await this.load();
      return result;
    } finally {
      this.checking = false;
    }
  }

  async loadPlatforms() {
    if (this.platforms.length) return this.platforms;
    const data = await api.listPlatforms();
    this.platforms = Array.isArray(data) ? data : [];
    return this.platforms;
  }

  /** The Platform set on the folder at *path*, or null when none is. */
  async currentMapping(path) {
    const rows = await api.listPlatformMappings();
    const hit = (Array.isArray(rows) ? rows : []).find((m) => m?.path === path);
    return hit?.platform ?? null;
  }

  /** Open the mapping dialog on a folder, and read what it is set to. */
  openMapping(path, name = null) {
    if (!path) return;
    this.mappingTarget = { path, name: name ?? path.split(/[/\\]/).filter(Boolean).pop() ?? path };
    this.loadCurrentMapping();
  }

  /**
   * Read the open dialog's folder mapping (again, for Retry). Owned by this
   * opening: a later opening, a close or a retry makes this read's answer,
   * or its failure, a no-op.
   */
  async loadCurrentMapping() {
    const target = this.mappingTarget;
    if (!target) return;
    const read = ++this.#mappingRead;
    this.mappingState = 'loading';
    this.mappingCurrent = null;
    this.mappingError = null;
    try {
      const slug = await this.currentMapping(target.path);
      if (read !== this.#mappingRead) return;
      this.mappingCurrent = slug;
      this.mappingState = 'loaded';
    } catch (e) {
      if (read !== this.#mappingRead) return;
      this.mappingError = e?.message ?? "Could not read the folder's Platform";
      this.mappingState = 'error';
    }
  }

  closeMapping() {
    this.mappingTarget = null;
    this.#mappingRead += 1;
    this.mappingState = 'idle';
    this.mappingCurrent = null;
    this.mappingError = null;
  }

  /** Set (or with `platform` null, clear) a folder's Platform, then refresh the list. */
  async setMapping(path, platform) {
    const saved = await api.setPlatformMapping(path, platform);
    if (saved?.walk?.status === 'listing_in_progress') {
      this.pending = saved.walk.message ?? 'A listing is running; Findings refresh after it.';
    }
    await this.load();
    return saved;
  }
}

export const findings = new FindingsStore();
