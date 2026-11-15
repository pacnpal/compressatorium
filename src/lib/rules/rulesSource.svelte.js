// One Library's Rules, as the Rules editor (RuleEditor.svelte) reads them.
//
// Shared by every Library: the RomM store extends it, and so does a folder
// Library's FolderRulesSource (libraries.svelte.js). Only the endpoints differ,
// and they come in through the constructor:
//
//   { getRules(), saveRules(rules), forgetConverted(platformIds),
//     preview(body), run(body), loadError }
//
// A subclass adds its Platforms and the members the editor reads beside the
// Rules: platforms, imageCount(platform), platformName(id), emptyTitle,
// emptyHint, scheduleEnabled, setScheduleEnabled(enabled), and optionally
// automatableModes, losesDatMatch(spec), imageNouns, the filter labels, and
// afterRun(result).
//
// No imports on purpose: the store tests evaluate this file with Node.

export class RulesSource {
  rules = $state({});
  ruleDefaults = $state(null);
  ruleOptions = $state({ orders: [], duplicate_actions: [], days: [] });
  ruleState = $state({});
  rulesLoading = $state(false);
  rulesSaving = $state(false);
  rulesError = $state(null);
  // Unsaved edits live here, next to the rules they describe, not in the
  // editor component: the editor unmounts every time the operator looks
  // elsewhere, and a component-local flag came back false while `rules` still
  // held the edits — so the save bar vanished and Preview / Run now silently
  // reported on the *server's* rules while showing the new ones.
  rulesDirty = $state(false);
  sweepResult = $state(null);
  sweeping = $state(false);

  // [one, many]: what an Image is called in this Library's counts.
  imageNouns = ['Image', 'Images'];
  matchedLabel = 'Only Recognized Images';
  unmatchedLabel = 'Only Images a match says are not Recognized';

  #endpoints;
  // Bumped by every rule edit; see `saveRules`. Not $state -- nothing renders
  // it, and it changes on every keystroke in the editor.
  #rulesRevision = 0;
  // Bumped by every save the backend accepted; see `loadRules`.
  #saveGeneration = 0;
  // Saves run one at a time, in the order they were asked for; see `saveRules`.
  #saveChain = Promise.resolve();
  #savesPending = 0;

  constructor(endpoints) {
    this.#endpoints = endpoints;
  }

  /** "1 Image", "3 Images". */
  countLabel(n) {
    const [one, many] = this.imageNouns;
    return `${n} ${n === 1 ? one : many}`;
  }

  /** Modes that may run unattended, from the backend; null means no opinion. */
  get automatableModes() {
    return null;
  }

  /**
   * May this mode run unattended?
   *
   * The backend derives it (a mode that turns its own product into a
   * differently named file never terminates -- CHDMAN `copy` writes
   * `Game_copy.chd`, then `Game_copy_copy.chd`, once per sweep until the
   * volume fills) and serves the list. Absent -- an older server --
   * everything is offered: the editor's own `kind === 'extract'` exclusion
   * still applies either way.
   */
  modeIsAutomatable(mode) {
    const allowed = this.automatableModes;
    return Array.isArray(allowed) ? allowed.includes(mode) : true;
  }

  /** Whether a mode loses this Library's metadata match. Only RomM's can. */
  losesDatMatch() {
    return false;
  }

  /** Called after a real run, before the state reload (RomM: the re-pin badge). */
  afterRun() {}

  async loadRules() {
    this.rulesLoading = true;
    this.rulesError = null;
    const generation = this.#saveGeneration;
    try {
      const data = await this.#endpoints.getRules();
      // Server truth, unless the editor is holding edits nobody has saved: a
      // reload can be triggered by something else entirely (a settings save on
      // the next tab, a sweep finishing), and overwriting there would discard
      // what the operator typed with no way back. Nor when a save landed while
      // this read was in flight: the read is older than the saved Rules, and
      // adopting it would put the previous configuration back in the editor
      // for the next save to write over the new one.
      if (!this.rulesDirty && generation === this.#saveGeneration) {
        this.rules = data?.rules ?? {};
      }
      this.ruleDefaults = data?.defaults ?? null;
      this.ruleOptions = data?.options ?? this.ruleOptions;
      this.ruleState = data?.state ?? {};
    } catch (e) {
      // Surfaced, not swallowed: without this the editor renders an empty rule
      // set that looks like "nothing is configured" when the request failed.
      this.rulesError = e?.message ?? this.#endpoints.loadError ?? 'Failed to load the Rules';
      throw e;
    } finally {
      this.rulesLoading = false;
    }
    return this.rules;
  }

  /** A rule for *platformId*, falling back to the backend-supplied defaults. */
  ruleFor(platformId) {
    const key = String(platformId);
    if (this.rules[key]) return { ...this.rules[key] };
    return { ...(this.ruleDefaults ?? {}) };
  }

  lastRunFor(platformId) {
    return this.ruleState[String(platformId)] ?? null;
  }

  setRule(platformId, rule) {
    this.rules = { ...this.rules, [String(platformId)]: rule };
    this.rulesDirty = true;
    this.#rulesRevision += 1;
  }

  removeRule(platformId) {
    const next = { ...this.rules };
    delete next[String(platformId)];
    this.rules = next;
    this.rulesDirty = true;
    this.#rulesRevision += 1;
  }

  /**
   * Save the Rules. Saves from one source reach the backend one at a time, in
   * the order they were asked for (Save, then Preview or Run, can each ask):
   * two in flight could land out of order and leave the older configuration
   * on the server. Each submits the Rules as they are when its turn comes.
   */
  saveRules() {
    this.#savesPending += 1;
    this.rulesSaving = true;
    const turn = this.#saveChain.then(() => this.#saveNow());
    // The chain only orders; each caller still sees its own save's failure.
    this.#saveChain = turn.catch(() => {});
    return turn.finally(() => {
      this.#savesPending -= 1;
      this.rulesSaving = this.#savesPending > 0;
    });
  }

  async #saveNow() {
    // The editor stays live while this is in flight, so anything typed after
    // this point is NOT in what we just submitted.
    const revision = this.#rulesRevision;
    const data = await this.#endpoints.saveRules(this.rules);
    this.#saveGeneration += 1;
    if (revision !== this.#rulesRevision) {
      // Edited mid-save. Adopting the server's copy would replace those
      // edits with the snapshot that was submitted before them — silently,
      // and then clear the dirty flag so the Save bar stopped asking. The
      // newer edits stay, still dirty, for the next save to normalize.
      return this.rules;
    }
    // Take the server's normalized copy: it clamps numbers and drops rules
    // whose mode no longer exists, so the editor shows what will actually run.
    this.rules = data?.rules ?? this.rules;
    this.rulesDirty = false;
    return this.rules;
  }

  /** How many sources this rule remembers having produced an output for. */
  convertedCountFor(platformId) {
    const entry = this.ruleState[String(platformId)] ?? {};
    if (entry.converted && typeof entry.converted === 'object') {
      return Object.keys(entry.converted).length;
    }
    // The earlier shape, kept readable so an upgraded install still shows a
    // count (and a Forget history button) for what it recorded before.
    return Array.isArray(entry.converted_ids) ? entry.converted_ids.length : 0;
  }

  /** How many Images this platform holds after a failed conversion. */
  heldCountFor(platformId) {
    const failed = this.ruleState[String(platformId)]?.failed;
    return failed && typeof failed === 'object' ? Object.keys(failed).length : 0;
  }

  /**
   * Forget the "already converted" history (and failed holds) for one
   * platform, or all of them.
   *
   * Overwrite and rename rules cannot tell "already done" from the
   * destination alone, so they remember what they produced. Restore a library
   * from a backup, or move outputs aside by hand, and that memory is the only
   * thing standing between the operator and a rerun.
   */
  async forgetConverted(platformIds = null) {
    const data = await this.#endpoints.forgetConverted(platformIds);
    this.ruleState = data?.state ?? this.ruleState;
    return data?.cleared ?? 0;
  }

  async previewSweep(platformIds = null) {
    this.sweeping = true;
    try {
      this.sweepResult = await this.#endpoints.preview(
        platformIds ? { platform_ids: platformIds } : {},
      );
      return this.sweepResult;
    } finally {
      this.sweeping = false;
    }
  }

  async runSweep(platformIds = null) {
    this.sweeping = true;
    try {
      this.sweepResult = await this.#endpoints.run(
        platformIds ? { platform_ids: platformIds } : {},
      );
      this.afterRun(this.sweepResult);
      // The sweep already succeeded; a failing state refresh must not be
      // reported as a failed run. `rulesError` carries that failure instead.
      await this.loadRules().catch(() => {});
      return this.sweepResult;
    } finally {
      this.sweeping = false;
    }
  }
}
