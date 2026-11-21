// DAT matching store, wraps /api/dat endpoints. Caches per-path match
// results so FileList rows can render badges without per-row fetches.

import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { LatestSaveQueue } from '$lib/stores/latestSaveQueue.js';

// How long a match attempt suppresses a retry for the same path. Must exceed
// the backend's Hasheous cooldown (60s) so the retry lands after the service
// has had a chance to recover, not during the outage.
const ATTEMPT_RETRY_MS = 90_000;

// How many times a hydration cycle will re-check itself on a timer before
// giving up until something else (navigation, a finished job, a DAT change)
// triggers it. Expiring the guard is not enough on its own: nothing reactive
// changes when the window elapses, so a static page would sit there unmatched.
// Bounded because a permanently-skipped file (over MATCH_MAX_FILE_SIZE) would
// otherwise re-spawn a no-op job every interval for as long as the tab is open.
const MAX_AUTO_RETRIES = 2;

// How often the workspace re-asks which sources can answer a hash lookup, so
// a provider toggled in another tab (or through the API) reaches this one.
// Both directions matter, so this runs for as long as the workspace is
// mounted: one small /dat/stats a minute, against a change that otherwise
// needs a reload to notice.
const AVAILABILITY_POLL_MS = 60_000;

// A job in one of these has ended.
const TERMINAL_JOB_STATUSES = ['completed', 'failed', 'cancelled'];

class DATMatchingStore {
  matches = new SvelteMap();
  matchingAvailable = $state(false);
  panelOpen = $state(false);
  importingDat = $state(false);
  redump = $state(null);
  redumpSyncing = $state(false);
  syncing = $state(false);
  syncStatus = $state(null);
  dats = $state([]);
  datsLoading = $state(false);
  datsError = $state(null);
  /** True once the user has saved a DAT order (false: family defaults apply). */
  datsRankingCustom = $state(false);
  // The DAT order's generation, bumped when a load starts and when a save
  // starts and lands (the LibrariesStore settings-generation pattern). A load
  // adopts its answer only if nothing began since, so a load older than a save
  // (or than another load) never puts the old order back.
  #orderGeneration = 0;
  // DAT order saves, one at a time in click order; only the latest is adopted.
  #orderSaves = new LatestSaveQueue();
  stats = $state(null);

  // Paths we've already kicked a match-job for, mapped to WHEN. The backend
  // may complete a job without caching a result, which would otherwise make
  // hydrate() see the same path as uncached forever and re-spawn jobs on every
  // hydration cycle. Plain object map (not Set/Map) for the membership lookup
  // so the svelte/prefer-svelte-reactivity rule doesn't flag it as a candidate
  // for SvelteSet; this guard is purely internal and reloading the page resets
  // it.
  //
  // The timestamps matter: an uncached result means either a permanent skip
  // (over MATCH_MAX_FILE_SIZE) or a *transient* failure (a Hasheous timeout,
  // which is deliberately non-cacheable). The client can't tell them apart, so
  // a permanent guard would strand the transient ones until a page reload —
  // exactly the files the retry is for. Entries expire instead, which retries
  // the transient ones and costs a permanently-skipped file one cheap no-op
  // job per interval (it re-checks the size cap without hashing).
  _attemptedPaths = Object.create(null);
  // Match jobs startMatchJob() started, job id -> { paths, reading }, every
  // path submitted, kept until hydrateFinishedJobs() has read them back. A
  // plain object, for the reason _attemptedPaths is one.
  _submittedJobs = Object.create(null);
  // How often, and how far apart, a finished job's read is tried before it is
  // given up on (the file list's own hydration still covers the visible rows).
  _jobHydrateAttempts = 3;
  _jobHydrateRetryMs = 2000;
  _retryTimer = null;
  _retryPaths = null;
  // Runs only while nothing can answer a lookup (see watchMatchingAvailability).
  _availabilityTimer = null;
  _autoRetries = 0;
  // The path set the last match job was started for, so a re-run of the same
  // visible page doesn't count as a new hydration and refill the retry budget.
  _lastJobPaths = null;
  // Bumped whenever the matching policy changes: a provider toggle (here or
  // in another tab), a DAT import, delete or finished sync. One counter, not
  // two: both the availability refresh and the cache hydration have to
  // discard responses that were in flight across a change, and giving them
  // separate counters was how the second case got missed.
  //
  // Reactive, and read through `policyGeneration` by the file list's effect.
  // Every one of those changes clears `matches`, but clearing it is invisible
  // to that effect by design -- hydrate() snapshots the map under untrack so
  // the effect does not subscribe to the cache it fills. With local DATs
  // present `matchingAvailable` never flips either, so a provider enabled in
  // another tab cleared the badges and nothing re-hydrated them until
  // navigation. This is the dependency that makes the clear self-healing.
  _generation = $state(0);
  // Hydration ordering. _hydrateSeq numbers each request as it goes out;
  // _appliedSeqByPath records, per path, the newest one that has actually
  // written that path. Per path, not one counter for the map: a read of a
  // whole job's paths answering after the visible page's read was otherwise
  // dropped whole, off-page paths included (Codex on PR #442).
  // Two counters, not one: discarding on "a newer request exists" would throw
  // away an in-order answer whenever two hydrations overlap, while discarding
  // on "a newer answer already applied" only drops the responses that are
  // genuinely stale.
  _hydrateSeq = 0;
  _appliedSeqByPath = Object.create(null);
  // Orders /dat/stats responses against each other without claiming the
  // matching policy changed. See refreshMatchingAvailability().
  _statsSeq = 0;

  matchFor(path) {
    return this.matches.get(path) ?? null;
  }

  /**
   * The current matching policy, as a counter that changes when it does.
   *
   * Read this from an `$effect` that must re-run when the policy changes --
   * the file list's hydration does. Exposed as a getter so the reason to read
   * it lives here rather than in an underscore-prefixed field access.
   */
  get policyGeneration() {
    return this._generation;
  }

  /**
   * Keep re-checking availability for as long as nothing can answer a lookup.
   *
   * `refreshMatchingAvailability()` already knows how to absorb a provider
   * flip made elsewhere -- another tab, an API client -- but on the workspace
   * nothing was driving it after mount. A DAT-less install whose operator
   * enabled Hasheous in a second tab sat at `matchingAvailable === false`, so
   * every hydration returned before starting a job, until this tab happened to
   * visit the DAT view or was reloaded.
   *
   * Runs for as long as the workspace is mounted, in both directions. An
   * earlier version stopped itself once something could answer a lookup, on
   * the reasoning that `matchingAvailable === false` is the only state a stale
   * reading changes behaviour in. That was wrong: a provider *disabled*
   * elsewhere makes this tab merge the backend's now-unmatched rows into
   * `matches`, and stopping meant a later re-enable was never noticed -- those
   * paths count as known and are never matched again. One transition healed,
   * the other made permanent.
   *
   * A field plus methods rather than a closure returning its own teardown --
   * the same shape `_retryTimer` already uses, so both of this store's timers
   * are torn down the same way.
   */
  watchMatchingAvailability(intervalMs = AVAILABILITY_POLL_MS) {
    this.stopWatchingAvailability();
    this._availabilityTimer = setInterval(() => this._recheckAvailability(), intervalMs);
    this._recheckAvailability();
  }

  /** Stop the availability watch. Idempotent; the workspace calls it on unmount. */
  stopWatchingAvailability() {
    if (this._availabilityTimer) clearInterval(this._availabilityTimer);
    this._availabilityTimer = null;
  }

  async _recheckAvailability() {
    // Swallowed: /dat/stats failing is not this poll's problem to report, and
    // refreshMatchingAvailability() has already recorded what it could.
    await this.refreshMatchingAvailability().catch(() => {});
  }

  async refreshMatchingAvailability() {
    // App.svelte and DATView both refresh on mount, so one of those can be
    // in flight when a toggle PUT lands. Its older /dat/stats answer would
    // otherwise arrive last and overwrite the authoritative state the PUT
    // just applied -- the panel snaps back to "off" and matchingAvailable
    // stays false, so browsing schedules nothing until some later refresh.
    // Two counters, and deliberately not one. `_statsSeq` orders *these*
    // requests against each other; `_generation` means "the matching policy
    // changed, discard work decided under the old one". Bumping the generation
    // on every call conflated them, which was harmless while this ran once at
    // mount and stopped being harmless the moment it became a poll: a tick
    // landing mid-`startMatchJob()` invalidated that job's results even though
    // nothing about the provider had changed, and with no terminal job event
    // coming, the badge stayed blank until unrelated work re-hydrated it.
    const seq = ++this._statsSeq;
    const generation = this._generation;
    try {
      const stats = await api.getDATStats();
      // Still discarded across a real policy change -- a toggle PUT applying
      // while this was in flight is what the generation check was for.
      if (seq !== this._statsSeq || generation !== this._generation) {
        return this.matchingAvailable;
      }
      const wasEnabled = this.stats?.hasheous_enabled;
      this.stats = stats;
      // Another tab (or an API client) can flip the provider under us. The
      // backend then withholds cached misses that predate the stronger
      // source, but hydrate() only adds rows and never removes them, so those
      // paths would sit in `matches` as known-unmatched and never be
      // scheduled. Same reset setHasheousEnabled() does for a local toggle.
      if (wasEnabled !== undefined && wasEnabled !== stats?.hasheous_enabled) {
        // Bump first: a hydrate already in flight answered under the old
        // policy and must not repopulate what this clear is dropping.
        this._generation += 1;
        this.matches.clear();
        this._resetAttempts();
      }
      // /api/dat/stats returns total_dats (legacy UI checked this exact field);
      // `total` / `imported_count` are not part of the response.
      //
      // This gate is "can anything answer a hash lookup?", NOT "are there
      // DATs" -- with Hasheous enabled a library with zero imported DATs still
      // matches, and gating on total_dats alone would mean ordinary browsing
      // never kicks a match job in exactly that case.
      this.matchingAvailable =
        (stats?.total_dats ?? 0) > 0 || Boolean(stats?.hasheous_enabled);
      return this.matchingAvailable;
    } catch (_e) {
      if (seq !== this._statsSeq || generation !== this._generation) {
        return this.matchingAvailable;
      }
      this.matchingAvailable = false;
      return false;
    }
  }

  /**
   * Flip the Hasheous fallback and re-read availability.
   *
   * Enabling it can turn previously-unmatched files into matches, so the
   * cached badges and the session's "already attempted" set are both dropped
   * — the backend re-checks those rows against the newly available source
   * (see cached_result_usable), but only if the client asks again.
   */
  async setHasheousEnabled(enabled) {
    const state = await api.setHasheousEnabled(enabled);
    // Apply the authoritative response NOW, before the counters refresh.
    // That refresh swallows its own errors, so if /dat/stats happened to fail
    // right after a successful PUT we would report success while the panel
    // still showed the old state and matchingAvailable stayed stale -- with
    // the backend already switched over.
    this._applyHasheousState(state);
    // Bump first: a hydrate already in flight answered under the old
    // policy and must not repopulate what this clear is dropping.
    this._generation += 1;
    this.matches.clear();
    this._resetAttempts();
    // Identity, not a flag: `refreshMatchingAvailability()` assigns a freshly
    // deserialized `stats` object on every success, so an unchanged reference
    // means it brought nothing back -- /dat/stats failed, or its answer was
    // discarded as stale.
    const before = this.stats;
    await this.refreshMatchingAvailability();
    // ...and re-apply it only then. The re-apply exists because that refresh
    // swallows a failed /dat/stats and falls back to
    // `matchingAvailable = false`, which would bury the state the PUT just
    // established -- reporting success while the panel shows off and browsing
    // stays gated, with the backend already switched over.
    //
    // Unconditionally, though, it did the opposite harm: another tab flipping
    // the provider while the refresh was in flight makes that refresh the
    // NEWER truth, and re-applying this PUT's response on top restored the
    // older one for up to a poll interval -- gating valid matching, or
    // attempting work the backend had just disabled.
    if (this.stats === before) this._applyHasheousState(state);
    return state;
  }

  /** Merge a /dat/hasheous response into the cached stats. */
  _applyHasheousState(state) {
    if (!state) return;
    this.stats = {
      ...(this.stats ?? {}),
      hasheous_enabled: state.enabled,
      hasheous_url: state.url,
      hasheous_overridden: state.overridden,
      hasheous_env_default: state.env_default,
    };
    this.matchingAvailable =
      (this.stats.total_dats ?? 0) > 0 || Boolean(state.enabled);
  }

  async testHasheous() {
    return api.testHasheous();
  }

  async loadDATs() {
    // A later load or a save makes this read too old to adopt.
    this.#orderGeneration += 1;
    const readAt = this.#orderGeneration;
    this.datsLoading = true;
    this.datsError = null;
    try {
      // /api/dat/ranking (app/services/dat_store.py:get_ranking) lists every
      // DAT in ranked order: { custom, dats: [{ id, name, description,
      // version, imported_at, file_count, family, rank }] }.
      const data = await api.getDATRanking();
      if (readAt === this.#orderGeneration) this._applyRanking(data);
      await this.refreshMatchingAvailability();
    } catch (e) {
      this.datsError = e?.message ?? 'Failed to load DATs';
    } finally {
      this.datsLoading = false;
    }
  }

  /**
   * Fetch cached matches (never hashes) for a batch of paths.
   *
   * Resolves true once the lookup answered (applied, or dropped because the
   * policy changed meanwhile) and false when it failed, so a caller that must
   * see the answer can try again.
   */
  async hydrate(paths) {
    if (!paths?.length) return true;
    // A lookup that started before a provider change answers with the OLD
    // policy. Landing after matches.clear() it would put a stale local-only
    // miss back in the map, and hydrateAndMatch() treats any key in the map as
    // known -- so with local DATs present (matchingAvailable never flips, the
    // effect need not re-run) the newly enabled fallback stayed dead until a
    // reload.
    const generation = this._generation;
    // Responses can land out of order: hydration A reads a hit, a rescan
    // deletes it, hydration B correctly evicts it -- and then A's delayed
    // answer put the obsolete hit back. Each read takes its place in one order
    // when it is asked, and its answer writes a path only if nothing newer has
    // written it (_claimPaths), whoever wrote it: a lookup, a RomM listing or
    // a match job's answer.
    const seq = ++this._hydrateSeq;
    try {
      const data = await api.getMatchCache(paths);
      if (generation !== this._generation) return true;
      const current = this._claimPaths(seq, paths);
      const results = data?.results ?? {};
      // Reconcile every path this answer is the newest for. Overlaying the
      // returned rows is not enough: the server omits a path it no longer has
      // a usable row for -- one dropped because a rescan proved the file
      // changed, or one withheld by cached_result_usable() because it predates
      // a now-enabled lookup source -- and hydrateAndMatch() treats any key in
      // the map as known, so a replaced file kept the previous file's game. A
      // path an older read put there meanwhile is cleared too (CodeRabbit on
      // PR #442); a newer one is not this answer's to touch.
      for (const path of paths) {
        if (!current[path]) continue;
        if (Object.hasOwn(results, path)) this.matches.set(path, results[path]);
        else this.matches.delete(path);
      }
      return true;
    } catch (_e) {
      // non-fatal
      return false;
    }
  }

  /**
   * Fetch cached matches AND kick a background match job for any
   * paths the cache lookup didn't return. /api/dat/matches/lookup is
   * read-only (it never hashes), so files that have never been
   * matched by any session would otherwise never get a DAT badge.
   * The startMatchJob handler de-dupes against the existing job queue
   * server-side, so re-firing for the same path during a page
   * navigation is cheap. Skipped when no DATs are imported, the
   * match job would just no-op.
   */
  async hydrateAndMatch(paths, { fromRetry = false } = {}) {
    if (!paths?.length) return;
    // Read BEFORE the first await. $effect tracks only synchronous reads, and
    // this runs inside the file list's effect -- reading matchingAvailable
    // after `await this.hydrate()` left the effect unsubscribed from it, so
    // the false->true flip when /dat/stats resolves never re-ran it. On a
    // fresh Hasheous-only install that meant nothing matched until the user
    // navigated or reloaded. (The gate itself has to stay below the hydrate:
    // cached hits must load even when matching is unavailable.)
    const canMatch = this.matchingAvailable;
    // A pending retry belongs to the set it was armed with. _scheduleRetry()
    // retargets when it runs again, but the branch that starts a job never
    // reaches it -- so navigating from a folder whose lookups failed to one
    // with work to do left the old timer running, and it later hydrated (and
    // could start a remote match job for) files nobody is looking at any more.
    // Retiring it here covers every entry, not just the one that re-arms.
    if (!fromRetry) this._cancelRetryUnless(paths);
    await this.hydrate(paths);
    if (!canMatch) return;
    // Drop paths the backend attempted recently: if they came back uncached
    // after a completed dat_match job they were skipped (over
    // MATCH_MAX_FILE_SIZE, unreadable) or failed transiently, and re-spawning
    // immediately would loop. The guard expires so a transient failure — a
    // Hasheous outage is non-cacheable by design, and its cooldown is 60s —
    // gets retried once the service recovers, without a page reload.
    const now = Date.now();
    const stillUnknown = paths.filter((p) => !this.matches.has(p));
    const uncached = stillUnknown.filter((p) => !this._recentlyAttempted(p, now));
    if (uncached.length === 0) {
      // Everything left is inside its retry window. Come back when it expires,
      // otherwise a transient failure (a Hasheous outage) stays on screen as
      // "no badge" until the user navigates or reloads.
      if (stillUnknown.length) this._scheduleRetry(paths);
      return;
    }
    // Only a genuinely NEW path set refills the budget. `!fromRetry` alone was
    // not enough: the file list re-runs this on every dat_match completion with
    // the same visible paths, so a job outliving the 90s attempt window let the
    // terminal-job pass reset the budget and start another job, and each of
    // those completions did it again -- MAX_AUTO_RETRIES capped nothing. Keying
    // on the path set means navigating somewhere new still gets a full budget.
    if (!fromRetry && !this._samePaths(uncached, this._lastJobPaths)) {
      this._autoRetries = 0;
    } else if (!fromRetry) {
      // The same visible set again, driven by a terminal job event rather than
      // by the retry timer. `_autoRetries` is only incremented inside
      // _scheduleRetry(), so a job that outlives ATTEMPT_RETRY_MS reaches here
      // with the attempt guard already expired, skips the timer entirely, and
      // starts another full job -- for as long as the tab stays open. It is a
      // retry in everything but name, so it consumes the budget like one.
      if (this._autoRetries >= MAX_AUTO_RETRIES) return;
      this._autoRetries += 1;
    }
    this._lastJobPaths = uncached;
    const generation = this._generation;
    try {
      await this.startMatchJob(uncached);
      // Mark attempts only AFTER the backend accepted the job. A 409
      // (another match job already active) would otherwise strand the
      // paths permanently, and any other failure should also leave
      // them eligible for the next hydration to retry.
      //
      // ...and only while the verdict still belongs to the current policy.
      // startMatchJob() drops a stale response's results, but returning
      // normally is not the same as succeeding: every bump of _generation is
      // paired with a _resetAttempts() whose whole purpose is to make these
      // paths eligible again, and stamping them here wrote the suppression
      // straight back into the freshly cleared map -- for the full 90s
      // window, with no terminal job event coming to shake it loose.
      if (generation !== this._generation) return;
      for (const p of uncached) this._attemptedPaths[p] = now;
    } catch (_e) {
      // non-fatal, the file list still renders without badges. The
      // next hydration cycle will try these paths again once any
      // currently-active dat_match job has cleared.
    }
  }

  /**
   * Drop the session-scoped "already attempted" set. Called whenever
   * the DAT library state changes (import, delete, MAMERedump sync
   * finish) so files that were previously uncached against the old
   * DAT set get re-considered against the new one.
   */
  _resetAttempts() {
    this._attemptedPaths = Object.create(null);
    this._autoRetries = 0;
    this._lastJobPaths = null;
    if (this._retryTimer) {
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
      this._retryPaths = null;
    }
  }

  /**
   * Re-run hydration once the attempt window has expired.
   *
   * Only while a remote source is in play: with Hasheous off, a path still
   * uncached after a completed job is a permanent skip (size cap), not a
   * transient failure, so retrying it would never produce anything.
   */
  _scheduleRetry(paths) {
    if (!this.stats?.hasheous_enabled) return;

    // A pending timer holds the paths it was scheduled with. If the user has
    // navigated since, those are off-screen now: retarget rather than letting
    // the stale set win, or the retry rehydrates a folder nobody is looking at
    // and the visible failures never get revisited.
    if (this._retryTimer) {
      if (this._samePaths(paths, this._retryPaths)) return;
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
    }
    if (this._autoRetries >= MAX_AUTO_RETRIES) return;

    this._autoRetries += 1;
    this._retryPaths = paths;
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      this._retryPaths = null;
      this.hydrateAndMatch(paths, { fromRetry: true }).catch(() => {});
    }, ATTEMPT_RETRY_MS);
  }

  /**
   * Retire a pending retry that was armed for some other set of paths.
   *
   * `paths` is the set now on screen; a timer holding exactly that set is
   * still wanted and survives. Anything else is stale.
   */
  _cancelRetryUnless(paths) {
    if (!this._retryTimer) return;
    if (this._samePaths(paths, this._retryPaths)) return;
    clearTimeout(this._retryTimer);
    this._retryTimer = null;
    this._retryPaths = null;
    // The budget belongs to the set that just went away, not to this one.
    this._autoRetries = 0;
  }

  /**
   * Drop any pending retry outright, and hand the next visit a clean slate.
   * Called when the file list goes away: nothing is visible, so a timer that
   * fires would hydrate and potentially hash for a view that no longer exists.
   *
   * The budget goes with it, and unconditionally -- an early return on "no
   * timer" would skip exactly the case that needs it. A folder whose budget is
   * spent has no timer left, so leaving and coming back to it would find
   * `_lastJobPaths` still holding that same set and `_autoRetries` still at
   * the cap, and refuse to match. Navigating away and back is a deliberate act
   * by the operator; it refills the budget for the same reason navigating
   * somewhere new does.
   */
  cancelRetry() {
    if (this._retryTimer) clearTimeout(this._retryTimer);
    this._retryTimer = null;
    this._retryPaths = null;
    this._autoRetries = 0;
    this._lastJobPaths = null;
  }

  _samePaths(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    return a.every((p, i) => p === b[i]);
  }

  /**
   * True while `path`'s last match attempt is still recent enough to skip.
   * Comfortably longer than the backend's 60s Hasheous cooldown, so a retry
   * lands after the service has had a chance to recover rather than during
   * the outage it is waiting out.
   */
  _recentlyAttempted(path, now = Date.now()) {
    const at = this._attemptedPaths[path];
    return at !== undefined && now - at < ATTEMPT_RETRY_MS;
  }

  async matchBatch(paths) {
    if (!paths?.length) return null;
    // Its place in the read order is when it was asked (CodeRabbit on PR #442).
    const seq = ++this._hydrateSeq;
    try {
      const data = await api.matchBatch(paths);
      const results = data?.results ?? {};
      const current = this._claimPaths(seq, Object.keys(results));
      for (const [path, result] of Object.entries(results)) {
        if (current[path]) this.matches.set(path, result);
      }
      return data;
    } catch (_e) {
      return null;
    }
  }

  async startMatchJob(paths) {
    if (!paths?.length) return null;
    // Same guard as hydrate(): a provider toggle clears the map and bumps the
    // generation, and the idle response below carries verdicts reached under
    // the OLD policy. Merging them unconditionally put a local-only miss back
    // after the clear -- and because that path then counts as known and the
    // idle response fires no terminal job event, Hasheous was never tried for
    // it until some later cache reset.
    const generation = this._generation;
    // Its place in the read order is when it was asked (CodeRabbit on PR #442).
    const seq = ++this._hydrateSeq;
    const response = await api.startMatchJob(paths);
    // Remembered whatever the policy did meanwhile: hydrate() applies its own
    // generation guard when the job ends.
    if (response?.status === 'started' && response.job_id) {
      this._submittedJobs[response.job_id] = { paths: [...paths], reading: false };
    }
    if (generation !== this._generation) return response;
    // The endpoint answers {status: "idle", results} and creates NO job when
    // every path was cached between our hydrate() and this call (another
    // client, or a scan, got there first). Dropping that response left the
    // badges missing with no terminal job event to trigger another hydration,
    // so a static page stayed blank until the user navigated.
    const results = response?.results;
    if (results) {
      const current = this._claimPaths(seq, Object.keys(results));
      for (const [path, match] of Object.entries(results)) {
        if (match && current[path]) this.matches.set(path, match);
      }
    }
    return response;
  }

  /**
   * Re-read every path of each match job this store started that has ended
   * (#427, Codex on PR #442).
   *
   * The file list re-reads only the rows on screen when a match job ends, but
   * "Recognize these" submits the unidentified rows of every page, so the
   * count and the button went on describing off-page ROMs the job had already
   * done. Each job is read back once, then forgotten. Call it with the jobs
   * list from an `$effect`, so it runs as jobs change.
   */
  async hydrateFinishedJobs(jobList) {
    const ended = [];
    for (const job of jobList ?? []) {
      const tracked = this._submittedJobs[job?.id];
      if (!tracked || tracked.reading || !TERMINAL_JOB_STATUSES.includes(job.status)) continue;
      tracked.reading = true;
      ended.push(this._readBackJob(job.id, tracked.paths));
    }
    await Promise.all(ended);
  }

  /**
   * Read a finished job's paths back, retrying a read that fails a bounded
   * number of times (CodeRabbit on PR #442). Marked as being read meanwhile,
   * so another call doesn't start a second read. Forgotten once a read
   * answers; when every try fails it stays tracked, with no loop of its own,
   * and the next call (another job ending, a refresh) reads it again.
   */
  async _readBackJob(jobId, paths) {
    for (let attempt = 1; attempt <= this._jobHydrateAttempts; attempt += 1) {
      if (await this.hydrate(paths)) {
        delete this._submittedJobs[jobId];
        return;
      }
      if (attempt < this._jobHydrateAttempts) {
        await new Promise((resolve) => setTimeout(resolve, this._jobHydrateRetryMs * attempt));
      }
    }
    const tracked = this._submittedJobs[jobId];
    if (tracked) tracked.reading = false;
  }

  /**
   * The paths of *paths* an answer numbered *seq* is still the newest for,
   * which it now owns (a set as a plain object). The one ordering rule every
   * read of the match cache shares, lookups and RomM listings alike: an
   * answer for a path replaces only an older one for that same path, never
   * anything about the other paths it carries (Codex on PR #442).
   */
  _claimPaths(seq, paths) {
    const current = Object.create(null);
    for (const path of paths) {
      if (seq < (this._appliedSeqByPath[path] ?? 0)) continue;
      this._appliedSeqByPath[path] = seq;
      current[path] = true;
    }
    return current;
  }

  /**
   * A ticket for a RomM listing about to be requested: its place in the read
   * order and the matching policy it is read under. Taken before the request,
   * handed to :meth:`seedListing` with the answer.
   */
  beginListingRead() {
    return { seq: ++this._hydrateSeq, generation: this._generation };
  }

  /**
   * Put the matches a RomM listing shipped with its rows into the store
   * (#427), ordered per path against every other read (:meth:`_claimPaths`):
   * for a path no newer answer has written, the listing's match replaces what
   * is stored, and a row with none clears it. Dropped whole when the matching
   * policy changed since *ticket* was taken (:meth:`beginListingRead`).
   *
   * Seeded rather than read as a fallback beside the store: a policy change
   * clears the store and re-hydrates it, while a snapshot kept beside it
   * would bring back a miss recorded before Hasheous was switched on.
   */
  seedListing(entries, ticket) {
    if (!ticket || ticket.generation !== this._generation) return;
    const rows = (entries ?? []).filter((entry) => entry?.path);
    const current = this._claimPaths(ticket.seq, rows.map((entry) => entry.path));
    for (const entry of rows) {
      if (!current[entry.path]) continue;
      if (entry.dat_match) this.matches.set(entry.path, entry.dat_match);
      else this.matches.delete(entry.path);
    }
  }

  _applyRanking(data) {
    this.dats = Array.isArray(data?.dats) ? data.dats : [];
    this.datsRankingCustom = Boolean(data?.custom);
  }

  /**
   * Save a DAT order (ids, first wins) or, with `null`, go back to the family
   * defaults. The backend ranks matches when it serves them, so the cached
   * badges here may now name a different DAT: drop them, as deleteDAT does.
   */
  setDATRanking(order) {
    // Loads that set off from here on are older than this save.
    this.#orderGeneration += 1;
    return this.#orderSaves.schedule('order', (ticket) => this.#saveOrderNow(order, ticket));
  }

  async #saveOrderNow(order, ticket) {
    this.#orderGeneration += 1;
    let data;
    try {
      data = order === null ? await api.resetDATRanking() : await api.setDATRanking(order);
    } catch (e) {
      // An earlier save may have landed without being adopted: read it back.
      if (this.#orderSaves.isLatest('order', ticket)) await this.loadDATs();
      throw e;
    } finally {
      this.#orderGeneration += 1;
    }
    if (this.#orderSaves.isLatest('order', ticket)) this._applyRanking(data);
    this._generation += 1;
    this.matches.clear();
    this._resetAttempts();
    return data;
  }

  async deleteDAT(datId) {
    const result = await api.deleteDAT(datId);
    // The backend cascades DATMatch rows for the deleted DAT, so any
    // cached match entry that survives in the client map is now
    // stale, files that were only matched by this DAT would keep
    // showing the badge until next reload because hydrate() only adds
    // returned rows, never removes absent ones. Drop the whole cache
    // AND the session-scoped attempt set so the next FileList hydration
    // re-runs against the now-smaller DAT library and re-establishes
    // truth.
    // Bump first: a hydrate already in flight answered under the old
    // policy and must not repopulate what this clear is dropping.
    this._generation += 1;
    this.matches.clear();
    this._resetAttempts();
    await this.loadDATs();
    return result;
  }

  async loadRedump() {
    try {
      this.redump = await api.getRedump();
    } catch (_e) {
      this.redump = null;
    }
  }

  // One request; waits for redump.org, one system at a time (#524).
  async syncRedump(force = false) {
    this.redumpSyncing = true;
    try {
      const result = await api.syncRedump(force);
      // `changed`, not `imported`: a system that failed part-way may still
      // have committed some of its DATs (CodeRabbit PR #527).
      if (result?.changed) await this._afterDatChange();
      await this.loadRedump();
      return result;
    } finally {
      this.redumpSyncing = false;
    }
  }

  async _afterDatChange() {
    // Bump first: a hydrate already in flight answered under the old
    // policy and must not repopulate what this clear is dropping.
    this._generation += 1;
    this.matches.clear();
    this._resetAttempts();
    await this.loadDATs();
  }

  async importDATs(files) {
    this.importingDat = true;
    try {
      const result = await api.importDATs(files);
      // The backend's _import_dat_sync wipes the DATMatch cache because
      // newly-imported hashes may flip match results for files the
      // user already has. Mirror that on the client so stale badges
      // don't survive until the next visit, hydrate() only adds rows,
      // it never removes absent ones. Reset attempted paths too so
      // files previously deemed "uncached" against the old DAT get
      // re-considered against the new one.
      if (result?.imported?.length) await this._afterDatChange();
      return result;
    } finally {
      this.importingDat = false;
    }
  }

  async syncMAMERedump(tag = null) {
    // /api/dat/sync only schedules a background task and returns
    // immediately. Do NOT clear `syncing` in a finally, the sync is
    // still running on the backend. The store stays in the syncing
    // state until pollSyncStatus() observes the backend reporting
    // syncing=false, or cancelSync() is called explicitly.
    this.syncing = true;
    try {
      return await api.syncMAMERedump(tag);
    } catch (e) {
      // 409 means a MAMERedump sync is already running on the
      // backend. The legacy UI treated that as "good, keep polling
      // and observe progress"; clearing `syncing` here would freeze
      // any consumer poll/progress UI even though the backend is
      // actively working. Stay in the syncing state.
      if (e?.status === 409) return null;
      // Any other error means the start request itself failed,
      // there is no background work to wait for, so clear the flag
      // and re-raise.
      this.syncing = false;
      throw e;
    }
  }

  async pollSyncStatus() {
    const wasSyncing = this.syncing;
    try {
      this.syncStatus = await api.getSyncStatus();
      const stillSyncing = !!this.syncStatus?.syncing;
      this.syncing = stillSyncing;
      if (!stillSyncing && wasSyncing) {
        // Sync just finished (syncing → done transition). The backend
        // has persisted the new DAT set and may have dropped the old
        // one, so reload the full list, not just availability, and clear
        // the stale match cache (new hashes can flip prior matches).
        // Reset attempts so previously-uncached paths get tried
        // against the new DAT set. loadDATs() refreshes availability
        // internally.
        // Bump first: a hydrate already in flight answered under the old
        // policy and must not repopulate what this clear is dropping.
        this._generation += 1;
        this.matches.clear();
        this._resetAttempts();
        await this.loadDATs();
      } else if (!stillSyncing) {
        // Cold poll (e.g. on mount) with no sync running: cheap
        // An availability refresh is enough; no need to reload the whole list.
        await this.refreshMatchingAvailability();
      }
      return this.syncStatus;
    } catch (_e) {
      // Transient poll failures (brief backend restart, network blip)
      // should not tear down the polling loop while we believe a sync
      // is active. The legacy self-scheduling poller stayed armed
      // across single failures; mirror that by leaving `syncing` set
      // so the next poll attempt still fires. Only the cold-start
      // case where we never observed a sync (wasSyncing=false) flips
      // the flag off here.
      this.syncStatus = null;
      if (!wasSyncing) this.syncing = false;
      return null;
    }
  }

  async cancelSync() {
    // Don't swallow failures. /api/dat/sync/cancel returns 409 with
    // "No sync in progress" when there's nothing to cancel; swallowing
    // that would make DATView.handleCancelSync() always take the
    // success path and tell the user cancellation was requested when
    // it wasn't. Let the caller decide the UX.
    const res = await api.cancelSync();
    this.syncing = false;
    return res;
  }
}

export const datMatching = new DATMatchingStore();
