// One Library's Rules, as the Rules editor (RuleEditor.svelte) reads them.
//
// Shared by every Library: the RomM store extends it, and so does a folder
// Library's FolderRulesSource (libraries.svelte.js). Only the endpoints differ,
// and they come in through the constructor:
//
//   { getRules(), saveRules(rules, checkRules), forgetConverted(platformIds),
//     preview(body), run(body), recommended(), loadError }
//
// Every edit saves on its own (#541): the table calls `scheduleSave(delay)`
// after it changes a Rule, and `saveState` says how that went. Preview, Run
// now and turning automation on `flush()` first, so they never act on a
// configuration the server hasn't got.
//
// A subclass adds its Platforms and the members the editor reads beside the
// Rules: platforms, imageCount(platform), platformName(id), emptyTitle,
// emptyHint, scheduleEnabled, setScheduleEnabled(enabled), esFrontend,
// setEsFrontend(enabled) (the "used by an EmulationStation frontend" switch,
// #386), gamelistsFolder, setGamelistsFolder(path) (its ES-DE gamelists
// folder, #387), and optionally
// automatableModes, losesDatMatch(spec), imageNouns, the filter labels, and
// afterRun(result).
//
// A Platform has up to two Rules (#305): its conversion Rule in `rules` and
// its check Rule in `checkRules`, each with its own run state. They load and
// save together, and one never replaces the other.
//
// No imports on purpose: the store tests evaluate this file with Node.

// `invalid_window` belongs to the server: `normalize_rule` recomputes it from
// the window on every save, so the editor never sends it (Codex 4577383258: a
// sent flag kept a corrected window paused for good). It is dropped on screen
// as soon as the window or Enabled changes, so a stale error does not linger
// until the save answers. Any other edit leaves the server's error showing.
const WINDOW_FIELDS = ['window_start', 'window_end', 'enabled'];

function withoutStaleWindowFlag(previous, rule) {
  if (!rule?.invalid_window) return rule;
  if (previous && WINDOW_FIELDS.every((f) => previous[f] === rule[f])) return rule;
  const { invalid_window: _stale, ...rest } = rule;
  return rest;
}

function withoutWindowFlags(rules) {
  return Object.fromEntries(Object.entries(rules ?? {}).map(([key, rule]) => {
    if (!rule || !('invalid_window' in rule)) return [key, rule];
    const { invalid_window: _server, ...rest } = rule;
    return [key, rest];
  }));
}

export class RulesSource {
  rules = $state({});
  checkRules = $state({});
  ruleDefaults = $state(null);
  ruleOptions = $state({ orders: [], duplicate_actions: [], days: [] });
  ruleState = $state({});
  checkRuleState = $state({});
  rulesLoading = $state(false);
  rulesSaving = $state(false);
  rulesError = $state(null);
  // True once the Rules have been read: until then the table holds none of
  // them, and saving it would replace the Library's with that.
  rulesLoaded = $state(false);
  // Unsaved edits live here, next to the rules they describe, not in the
  // editor component: the editor unmounts every time the operator looks
  // elsewhere, and a component-local flag came back false while `rules` still
  // held the edits — so the save bar vanished and Preview / Run now silently
  // reported on the *server's* rules while showing the new ones.
  rulesDirty = $state(false);
  sweepResult = $state(null);
  sweeping = $state(false);
  // The autosave: 'idle', 'saving', 'saved' or 'error' (with `saveError`).
  saveState = $state('idle');
  saveError = $state(null);

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
  // The pending autosave's timer; see `scheduleSave`.
  #saveTimer = null;
  // The consoles changed since the last save, conversion and check Rules
  // apart. A save sends only these (null for one removed) and the server
  // merges them (#541, Codex 4653298940): a table holding none or some of
  // the Rules can never wipe the others.
  #changedRules = new Set();
  #changedChecks = new Set();

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

  /** What another converter does to a Platform (#385). Only RomM has one. */
  converterNoteFor() {
    return null;
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
        this.checkRules = data?.check_rules ?? {};
      }
      this.rulesLoaded = true;
      this.ruleDefaults = data?.defaults ?? null;
      this.ruleOptions = data?.options ?? this.ruleOptions;
      this.ruleState = data?.state ?? {};
      this.checkRuleState = data?.check_state ?? {};
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

  /** The check Rule for *platformId*, or null when it has none. */
  checkRuleFor(platformId) {
    const rule = this.checkRules[String(platformId)];
    return rule ? { ...rule } : null;
  }

  /** The check Rule's run state: last run, how many it checked. */
  lastCheckFor(platformId) {
    return this.checkRuleState[String(platformId)] ?? null;
  }

  setCheckRule(platformId, rule) {
    const key = String(platformId);
    this.checkRules = { ...this.checkRules, [key]: withoutStaleWindowFlag(this.checkRules[key], rule) };
    this.#changedChecks.add(key);
    this.rulesDirty = true;
    this.#rulesRevision += 1;
  }

  removeCheckRule(platformId) {
    const next = { ...this.checkRules };
    delete next[String(platformId)];
    this.checkRules = next;
    this.#changedChecks.add(String(platformId));
    this.rulesDirty = true;
    this.#rulesRevision += 1;
  }

  setRule(platformId, rule) {
    const key = String(platformId);
    this.rules = { ...this.rules, [key]: withoutStaleWindowFlag(this.rules[key], rule) };
    this.#changedRules.add(key);
    this.rulesDirty = true;
    this.#rulesRevision += 1;
  }

  /**
   * Add a Rule for each Platform in *modes* (Platform id -> mode) that has no
   * Rule yet: the backend defaults with that mode, in the browser's zone, off
   * as every new Rule is. A Platform that has a Rule, saved or not, keeps it.
   * Nothing is saved here. Returns how many were added.
   */
  addRules(modes) {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    let added = 0;
    for (const [platformId, mode] of Object.entries(modes ?? {})) {
      if (!mode || this.rules[String(platformId)]) continue;
      this.setRule(platformId, { ...this.ruleFor(platformId), mode, timezone: zone });
      added += 1;
    }
    return added;
  }

  removeRule(platformId) {
    const next = { ...this.rules };
    delete next[String(platformId)];
    this.rules = next;
    this.#changedRules.add(String(platformId));
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
    // Only what changed: each console's Rule as it is now, null when removed.
    const changed = [...this.#changedRules];
    const changedChecks = [...this.#changedChecks];
    this.#changedRules.clear();
    this.#changedChecks.clear();
    const patch = (keys, from) => withoutWindowFlags(
      Object.fromEntries(keys.map((k) => [k, from[k] ?? null])),
    );
    let data;
    try {
      data = await this.#endpoints.saveRules(
        patch(changed, this.rules),
        changedChecks.length ? patch(changedChecks, this.checkRules) : undefined,
      );
    } catch (e) {
      // Not saved: they go with the next save.
      for (const k of changed) this.#changedRules.add(k);
      for (const k of changedChecks) this.#changedChecks.add(k);
      throw e;
    }
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
    this.checkRules = data?.check_rules ?? this.checkRules;
    this.rulesDirty = false;
    return this.rules;
  }

  /**
   * Save the Rules after *delay* ms, restarting the wait on every call: a
   * text field passes 600 so a save goes out once typing stops, a switch 0.
   */
  scheduleSave(delay = 0) {
    clearTimeout(this.#saveTimer);
    this.saveState = 'saving';
    this.#saveTimer = setTimeout(() => {
      this.#saveTimer = null;
      this.#autosave();
    }, delay);
  }

  async #autosave() {
    this.saveState = 'saving';
    try {
      await this.saveRules();
      // A later edit has its own save coming; that one reports.
      if (!this.#saveTimer && this.#savesPending === 0) {
        this.saveState = this.rulesDirty ? 'saving' : 'saved';
      }
      this.saveError = null;
    } catch (e) {
      this.saveState = 'error';
      this.saveError = e?.message ?? "Couldn't save";
    }
  }

  /**
   * Send any edit still waiting, now. True once the server holds what the
   * table shows. An edit made while a save was out leaves the Rules dirty
   * (see `#saveNow`), so it saves once more before giving up.
   */
  async flush() {
    if (this.#saveTimer) {
      clearTimeout(this.#saveTimer);
      this.#saveTimer = null;
    }
    for (let attempt = 0; attempt < 2 && this.rulesDirty; attempt += 1) {
      await this.#autosave();
      if (this.saveState === 'error') return false;
    }
    return !this.rulesDirty;
  }

  /**
   * Apply recommended: give each console with games and no target the format
   * the backend picks for it, on, with *fieldsFor(mode)* (the saved safety
   * defaults) on top. A console that has a target keeps it. Saves, and
   * returns `{ added: [platform ids], skipped: [{platform_id, name, reason}] }`.
   */
  async applyRecommended(fieldsFor = () => ({})) {
    // Merged into Rules never read, a save would replace the Library's own
    // with these few (CodeRabbit 4652729322).
    if (!this.rulesLoaded || this.rulesError) {
      throw new Error("This Library's settings haven't loaded yet");
    }
    const plan = await this.#endpoints.recommended();
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    const added = [];
    for (const [platformId, entry] of Object.entries(plan?.rules ?? {})) {
      if (!entry?.mode || this.rules[String(platformId)]?.mode) continue;
      this.setRule(platformId, {
        ...this.ruleFor(platformId),
        compression: null,
        compression_level: null,
        split: false,
        timezone: zone,
        ...fieldsFor(entry.mode),
        mode: entry.mode,
        enabled: true,
      });
      added.push(String(platformId));
    }
    // A save that fails is no set-up (CodeRabbit 4652729388): the draft
    // stays, dirty, for the table's Try again.
    if (added.length && !(await this.flush())) {
      throw new Error(this.saveError ?? "Couldn't save");
    }
    return { added, skipped: plan?.skipped ?? [] };
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
  async forgetConverted(platformIds = null, { failedOnly = false } = {}) {
    const data = await this.#endpoints.forgetConverted(platformIds, failedOnly);
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
