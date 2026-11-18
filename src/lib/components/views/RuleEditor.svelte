<script>
  /**
   * Per-platform Rules for one Library.
   *
   * Each Platform of the Library gets its own Rule — target format, compression, output
   * location, schedule, queueing limits and selection filters — because a
   * library is not homogeneous and one global setting cannot describe it.
   *
   * The editor renders from the schema the backend ships alongside the rules
   * (`defaults` + `options`), so the UI never carries a second copy of what a
   * valid rule looks like.
   *
   * `source` is the Library's rules source (see `libraries.svelte.js`): the
   * RomM store for RomM, a `FolderRulesSource` for a folder Library.
   *
   * A Platform can also have a check Rule beside its conversion Rule (#305):
   * no target format, a nightly byte budget, a window and weekdays. See
   * `$lib/rules/checkRule.js`.
   */
  import { onMount, untrack } from 'svelte';
  import { followSaved } from '$lib/util/followSaved.js';
  import { SvelteSet } from 'svelte/reactivity';
  import { toast } from 'svelte-sonner';
  import { DAY_LABELS, ORDER_LABELS } from '$lib/stores/romm.svelte.js';
  import { ruleNotices } from '$lib/stores/ruleNotices.svelte.js';
  import { CHECK_LEVEL, CHECK_LEVEL_LABELS } from '$lib/util/checkLevels.js';
  import { DELETE_CHECK_HINTS, deleteCheckFor } from '$lib/util/deleteCheck.js';
  import { checkSummary, newCheckRule } from '$lib/rules/checkRule.js';
  import { registry } from '$lib/tools/registry.js';
  import { clampLevel, fitLevel } from '$lib/tools/levels.js';
  import { compat } from '$lib/stores/compat.svelte.js';
  import { compatEntry, compatWarning } from '$lib/tools/compat.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Play from '@lucide/svelte/icons/play';
  import Eye from '@lucide/svelte/icons/eye';
  import Save from '@lucide/svelte/icons/save';
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import Info from '@lucide/svelte/icons/info';
  import XIcon from '@lucide/svelte/icons/x';

  let { source } = $props();

  const platforms = $derived(source.platforms);

  // The one-time notice about the upgrade to Round-trip (#302). The server
  // says whether to show it, so this asks each time the editor opens.
  onMount(() => {
    ruleNotices.load();
    compat.load();
  });

  async function dismissCheckLevelNotice() {
    try {
      await ruleNotices.dismissCheckLevel();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to dismiss the notice');
    }
  }

  function ruleCount(n) {
    return n === 1 ? '1 rule' : `${n} rules`;
  }
  const autoOn = $derived(source.scheduleEnabled);
  const esOn = $derived(source.esFrontend);

  /** The Library's "used by an EmulationStation frontend" switch (#386). */
  async function toggleEsFrontend(enabled) {
    try {
      await source.setEsFrontend(enabled);
      toast.success(enabled ? 'Conversions now update gamelist.xml' : 'gamelist.xml is left alone');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save');
    }
  }

  // The Library's ES-DE gamelists folder (#387): edited here, saved on Save.
  // Follows the saved value whenever it changes (a save, another tab).
  // `followSaved` adopts a new saved folder only while the field holds no
  // edit: another setting's save, or the answer to an earlier save, never
  // throws away what the operator typed since.
  let gamelistsDraft = $state('');
  let gamelistsSeen = null; // the saved folder the field last adopted
  let gamelistsSaving = $state(false);
  $effect(() => {
    const saved = source.gamelistsFolder ?? '';
    untrack(() => {
      const prior = gamelistsSeen === null ? null : { draft: gamelistsDraft, seen: gamelistsSeen };
      ({ draft: gamelistsDraft, seen: gamelistsSeen } = followSaved(prior, saved));
    });
  });

  async function saveGamelistsFolder() {
    gamelistsSaving = true;
    try {
      await source.setGamelistsFolder(gamelistsDraft.trim());
      toast.success(gamelistsDraft.trim() ? 'Gamelists folder saved' : 'Gamelists folder cleared');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save');
    } finally {
      gamelistsSaving = false;
    }
  }

  // SvelteSet: mutation must be reactive so an expanded row re-renders.
  const expanded = new SvelteSet();
  // The store owns this: the tab unmounts whenever the operator switches to
  // the library, and a local flag came back false while the edits themselves
  // survived in `source.rules` — hiding the save bar and letting Preview / Run
  // now report on the server's rules while the editor showed the new ones.
  const dirty = $derived(source.rulesDirty);

  // The convertible targets one platform can actually use. Built from the
  // registry so a new tool or mode appears here with no change, then narrowed
  // by the tool ids the backend derived for this platform's slug — the same
  // narrowing the conversion path applies, so the editor cannot offer a
  // GameCube library a PS2-only mode.
  function modeOptionsFor(platform) {
    const allowedTools = platform?.tool_ids;
    // Narrowed per mode as well as per tool. A composite tool belongs to no
    // single system — the chain tool has a GameCube mode and a PS2 mode — so
    // the tool list keeps it on both and only the mode list can tell them
    // apart. Both come from the backend registry rather than a second copy of
    // the platform table here.
    const allowedModes = platform?.mode_ids;
    const out = [{ value: '', label: 'Off — do not convert this platform' }];
    for (const tool of registry.all()) {
      // No opinion from the backend (unknown slug, older server) keeps every
      // tool, matching narrow_to_platform's conservative contract.
      if (Array.isArray(allowedTools) && !allowedTools.includes(tool.id)) continue;
      for (const m of tool.modes ?? []) {
        if (m.kind === 'extract') continue; // automation converts, not unpacks
        // ...and a mode that turns its own product into a differently named
        // file never terminates. That one is the backend's answer, because it
        // needs the tool's own output_path; the backend refuses such a rule
        // too, for a blob written straight into the database.
        if (!source.modeIsAutomatable(m.mode)) continue;
        if (Array.isArray(allowedModes) && !allowedModes.includes(m.mode)) continue;
        out.push({ value: m.mode, label: `${tool.label} → ${m.label}` });
      }
    }
    return out;
  }

  function toggle(platformId) {
    if (expanded.has(platformId)) expanded.delete(platformId);
    else expanded.add(platformId);
  }

  // The zone the schedule window is evaluated in. Sent with every rule so a
  // window entered as "22:00" means 22:00 where the operator is, not UTC.
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  function update(platformId, patch) {
    const current = source.ruleFor(platformId);
    // A platform with no stored rule is being created right now, so it adopts
    // the browser's zone. `ruleFor` falls back to the backend default, whose
    // timezone is the truthy string "UTC" — testing `current.timezone ||`
    // therefore always kept UTC and a 22:00–04:00 window written in Berlin ran
    // at 22:00 UTC. A rule the operator has already saved keeps its own zone.
    const isNew = !source.rules[String(platformId)];
    const rule = {
      ...current,
      timezone: isNew ? browserZone : (current.timezone || browserZone),
      // Changing the target invalidates everything that described the old one.
      // Carrying `compression: "max"` from a CSO rule into a CHDMAN one passes
      // normalization and then fails every queued job on `chdman -c max`.
      ...(patch.mode !== undefined && patch.mode !== current.mode
        ? { compression: null, compression_level: null, split: false }
        : {}),
      ...patch,
    };
    // `setRule` / `removeRule` raise the store's dirty flag themselves.
    if (!rule.mode) source.removeRule(platformId);
    else source.setRule(platformId, rule);
  }

  /**
   * The Platform's check Rule (#305), kept apart from its conversion Rule.
   * A new one starts from the backend's defaults in the browser's zone.
   */
  function updateCheck(platformId, patch) {
    const current = source.checkRuleFor(platformId)
      ?? { ...newCheckRule(source.ruleDefaults), timezone: browserZone };
    source.setCheckRule(platformId, { ...current, ...patch });
  }

  function setChecking(platformId, on) {
    if (on) updateCheck(platformId, { enabled: true });
    else source.removeCheckRule(platformId);
  }

  /**
   * The two identification filters are mutually exclusive.
   *
   * The backend collapses "both ticked" to "neither" (it would otherwise mean
   * "nothing matches"), so leaving both tickable in the editor would show a
   * state that silently becomes a different one on save. Ticking one clears
   * the other here instead.
   */
  function setIdFilter(platformId, field, value) {
    const other = field === 'only_matched' ? 'only_unmatched' : 'only_matched';
    update(platformId, { [field]: value, ...(value ? { [other]: false } : {}) });
  }

  /** *days* with *day* switched on or off, in weekday order. */
  function toggledDays(days, day) {
    const next = new SvelteSet(days ?? []);
    if (next.has(day)) next.delete(day);
    else next.add(day);
    return [...next].sort((a, b) => a - b);
  }

  function specFor(mode) {
    return mode ? registry.specFor(mode) : null;
  }

  // The check a delete-source job from this Rule passes before its Source goes
  // (#302, #423): the helper the convert panel reads, so the two never differ.
  // A Recognized mode ignores the stored container-only level.
  function ruleDeleteCheck(spec, rule) {
    return deleteCheckFor({
      supportsRoundTrip: !!spec?.supportsRoundTrip,
      containerOnly: rule.check_level === CHECK_LEVEL.containerOnly,
      deleteProof: spec?.deleteProof,
    });
  }

  function toolFor(mode) {
    return mode ? registry.toolForMode(mode) : null;
  }

  /**
   * What "leave it to the tool" actually resolves to, named where the operator
   * picks it.
   *
   * A compression level rides on the same wire string as the codec, so a level
   * with no codec serialises as ":19". For a mode that offers no codec choice
   * (nsz's dropdown picks a solid/block layout) the tool reads that as its own
   * default and honours the level. For a mode that does offer codecs the
   * backend resolves the tool's default rather than send a blank one, and this
   * says which — otherwise "Tool default" plus a level looks like it means
   * "no compression".
   */
  function defaultCodecLabel(mode) {
    const spec = specFor(mode);
    const fallback = registry.defaultCompressionForMode(mode)?.[0];
    if (!spec?.supportsCompression || !fallback) return 'Tool default';
    return `Tool default (${fallback})`;
  }

  // --- conversion options, straight off the registry descriptor ----------
  //
  // Same source of truth CompressionPicker reads for the manual panel, so a
  // new tool declaring `compressionCodecs` / `compressionStyle` in
  // registry.js appears here with no edit. The rule stores one string, which
  // is what the job pipeline takes: a comma list for chdman's multi style,
  // a single codec elsewhere (the level rides separately and the backend
  // joins them as `codec:level`).

  function codecsFor(mode) {
    // The mode's own codecs where it names them (WIA refuses zstd), else the tool's.
    return registry.codecsForMode(mode);
  }

  function codecStyleFor(mode) {
    return toolFor(mode)?.compressionStyle ?? 'none';
  }

  /**
   * The level range of the codec a Rule runs with (its own, or the mode's
   * default when blank), or null when that codec takes no level (WIA's none)
   * and the editor shows no level box. Dolphin's lzma2 takes 1-9, zstd 1-22.
   * The same `registry.levelRangeFor` the convert panel reads (#363).
   */
  function ruleLevelRange(rule, codec = rule.compression) {
    return registry.levelRangeFor(rule.mode, codec);
  }

  /**
   * Set a single-codec Rule's codec. A level the new codec does not take
   * (19, from zstd, on lzma2; any level on none) is cleared, so the Rule runs
   * at that codec's own default level rather than one dolphin-tool refuses.
   */
  function setRuleCodec(platformId, rule, codec) {
    const range = ruleLevelRange(rule, codec);
    const level = rule.compression_level;
    const fits = level == null || (range && fitLevel(range, level) === level);
    update(platformId, fits ? { compression: codec } : { compression: codec, compression_level: null });
  }

  /**
   * Set a Rule's level from the box: pulled into the codec's range (19 on
   * lzma2 is 9), the same clamp as the convert panel; blank is null, which
   * runs the codec's default. A mode change already clears codec and level.
   */
  function setRuleLevel(platformId, rule, value) {
    const range = ruleLevelRange(rule);
    update(platformId, { compression_level: range ? clampLevel(range, value) : null });
  }

  // "Compatible with...": the emulator picked for each Platform's Rule, kept in
  // the editor only (the Rule saves its codecs, not the target). The table is
  // served by the server; choosing a target selects its preferred codecs, and
  // a codec it does not read only shows a warning.
  let compatPick = $state({});

  function chooseCompat(platformId, rule, targetId) {
    compatPick = { ...compatPick, [platformId]: targetId };
    const entry = compatEntry(compat.target(targetId), rule.mode);
    if (!entry) return;
    if (codecStyleFor(rule.mode) === 'multi') {
      update(platformId, { compression: entry.preferred.join(',') });
    } else {
      setRuleCodec(platformId, rule, entry.preferred[0]);
    }
  }

  /** The codecs the Rule would run with, for the compatibility warning. */
  function runningCodecs(rule) {
    if (codecStyleFor(rule.mode) === 'multi') return selectedCodecs(rule);
    return [rule.compression || registry.defaultCompressionForMode(rule.mode)?.[0]];
  }

  /**
   * The codecs a rule currently has selected, as a list: only those its mode
   * offers (#474), the narrowing the convert panel sends by. A codec the mode
   * can't use has no chip, so counting it said "(1 selected)" with nothing
   * pressed; the next toggle drops it.
   */
  function selectedCodecs(rule) {
    const listed = (rule.compression ?? '').split(',').map((c) => c.trim()).filter(Boolean);
    return registry.offeredCodecs(rule.mode, listed);
  }

  /** Toggle one codec in a multi-codec (chdman) rule.
   *
   * The cap comes from the registry, the same place the manual picker reads
   * it: chdman takes at most four codecs in `-c`, and a fifth here would save
   * happily and then fail every job the rule ever queued.
   */
  function toggleCodec(platformId, rule, value) {
    const current = selectedCodecs(rule);
    if (!current.includes(value) && current.length >= registry.maxCodecsFor(rule.mode)) {
      toast.error(`Up to ${registry.maxCodecsFor(rule.mode)} codecs at once.`);
      return;
    }
    const next = current.includes(value)
      ? current.filter((c) => c !== value)
      : [...current, value];
    update(platformId, { compression: next.join(',') || null });
  }

  const duplicateLabels = {
    skip: 'Skip it — leave the existing file alone',
    overwrite: 'Overwrite it',
    rename: 'Write alongside it (Game_1.rvz)',
  };

  const duplicateOptions = $derived(
    (source.ruleOptions?.duplicate_actions ?? ['skip', 'overwrite', 'rename']).map(
      (value) => ({ value, label: duplicateLabels[value] ?? value }),
    ),
  );

  async function save() {
    try {
      await source.saveRules();
      toast.success('Automation rules saved');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save rules');
    }
  }

  /**
   * Persist unsaved edits before a sweep.
   *
   * The sweep runs server-side against the *stored* rules, so previewing or
   * running with a dirty editor would report on rules the operator can no
   * longer see. Saving first keeps the backend the single source of truth
   * instead of teaching the sweep endpoints to accept a rule set inline.
   */
  async function commitPendingEdits() {
    if (!dirty) return true;
    try {
      await source.saveRules();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save rules');
      return false;
    }
    if (source.rulesDirty) {
      // The save was superseded: an edit landed while it was in flight, so the
      // store deliberately kept the newer rule and left it unsaved. Proceeding
      // would run the action against the *older* server-side snapshot while
      // the editor shows the newer one -- including a platform just switched
      // off, or a delete-after-verify just cleared. Save again rather than act
      // on a configuration nobody is looking at.
      try {
        await source.saveRules();
      } catch (e) {
        toast.error(e?.message ?? 'Failed to save rules');
        return false;
      }
      if (source.rulesDirty) {
        toast.warning('Your edits are still saving — try that again in a moment');
        return false;
      }
    }
    return true;
  }

  async function preview() {
    if (!(await commitPendingEdits())) return;
    try {
      const r = await source.previewSweep();
      toast.info(sweepToast(r, 'Would queue', 'Would check'));
    } catch (e) {
      toast.error(e?.message ?? 'Preview failed');
    }
  }

  async function runNow() {
    if (!(await commitPendingEdits())) return;
    try {
      const r = await source.runSweep();
      if (r?.queued || r?.checks) toast.success(sweepToast(r, 'Queued', 'Checking'));
      else toast.info('Nothing to convert or check right now');
    } catch (e) {
      toast.error(e?.message ?? 'Run failed');
    }
  }

  /** "Queued 3 conversions, checking 12 Images", or what is empty. */
  function sweepToast(r, queuedVerb, checkVerb) {
    const parts = [];
    const n = r?.queued ?? 0;
    const c = r?.checks ?? 0;
    if (n) parts.push(`${queuedVerb} ${n} conversion${n === 1 ? '' : 's'}`);
    if (c) parts.push(`${checkVerb.toLowerCase()} ${c} Image${c === 1 ? '' : 's'}`);
    if (!parts.length) return 'Nothing to convert or check';
    const text = parts.join(', ');
    return text[0].toUpperCase() + text.slice(1);
  }

  async function toggleAuto(enabled) {
    // Save first, exactly as Preview and Run now do. The scheduler reads the
    // server's rules, so enabling it with unsaved edits starts unattended runs
    // against the *previous* configuration — which may still have the platform
    // enabled, or delete-on-verify on, while the editor shows the safer one
    // the operator just typed. (Turning it off needs no such care: stopping is
    // safe whatever is pending, and committing edits nobody saved would be the
    // surprise.)
    if (enabled && !(await commitPendingEdits())) return;
    try {
      await source.setScheduleEnabled(enabled);
      toast.success(enabled ? 'Automatic conversion on' : 'Automatic conversion off');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save');
    }
  }

  /** Clear one platform's converted-id history (see the store for why). */
  async function forgetConverted(platformId) {
    try {
      await source.forgetConverted([String(platformId)]);
      toast.success('Conversion history cleared for this platform');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to clear the conversion history');
    }
  }

  /**
   * Release the Images held after a failed conversion, so the next run tries
   * them again. The same call as Forget history, which clears holds too.
   */
  async function retryFailed(platformId) {
    try {
      await source.forgetConverted([String(platformId)]);
      toast.success('The next run tries those Images again');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to release the held Images');
    }
  }

  /** Why a platform produced nothing, in the operator's terms.
   *
   * These reasons exist precisely because the sweep refuses to queue work it
   * knows will fail, so saying nothing would leave a rule that converts
   * nothing and never explains itself.
   */
  const SWEEP_ERROR_LABELS = {
    tool_not_ready: 'the tool for this format is not installed here — skipped',
    tool_wrong_for_platform: 'the saved format is not for this system — skipped',
    output_dir_outside_volumes:
      'its output folder is outside the configured volumes — skipped',
    unreadable: 'the Library could not list this platform',
    root_unavailable:
      'the folder could not be reached inside the configured volumes (a mount may '
      + 'be down) — the next run checks again',
    listing_in_progress: 'a listing of this folder is still running, try again shortly',
    queue_failed: 'the conversion queue rejected this batch',
    destination_claimed:
      'another job took one of the output paths — nothing was queued, the next '
      + 'run picks it up',
    library_unresponsive:
      'the library stopped responding while reading this platform — stopped '
      + 'partway',
  };

  function sweepErrorLabel(code) {
    return SWEEP_ERROR_LABELS[code] ?? code;
  }

  function lastRunLabel(platformId) {
    const state = source.lastRunFor(platformId);
    if (!state?.last_run_at) return 'never run';
    const when = new Date(state.last_run_at);
    if (Number.isNaN(when.getTime())) return 'never run';
    return `last run ${when.toLocaleString()} · queued ${state.last_queued ?? 0}`;
  }

  /**
   * A check Rule's last run: the nightly one or a Run now, whichever is later.
   * "picked": the count is what the sweep chose, before the checks ran.
   */
  function lastCheckLabel(platformId) {
    const state = source.lastCheckFor(platformId);
    const stamps = [state?.last_run_at, state?.last_manual_run_at]
      .map((s) => (s ? new Date(s) : null))
      .filter((d) => d && !Number.isNaN(d.getTime()));
    if (!stamps.length) return 'never run';
    const when = new Date(Math.max(...stamps.map((d) => d.getTime())));
    return `last run ${when.toLocaleString()} · picked ${state.last_checks ?? 0}`;
  }
</script>

<div class="automation">
  <section class="master">
    <div class="master-text">
      <h2>Automatic conversion</h2>
      <p>
        Each platform runs on its own schedule. An Image is only queued when its
        target format is genuinely missing, so a sweep that runs twice — or
        after a restart — never duplicates work.
      </p>
    </div>
    <div class="master-actions">
      <Checkbox
        checked={autoOn}
        label="Run automatically"
        onchange={(v) => toggleAuto(v)}
      />
      <Button variant="secondary" onclick={preview} loading={source.sweeping} icon={eyeIcon}>
        Preview
      </Button>
      <Button onclick={runNow} loading={source.sweeping} icon={playIcon}>Run now</Button>
    </div>
  </section>

  <section class="master frontend">
    <div class="master-text">
      <h2>EmulationStation frontend</h2>
      <p>
        For ES-DE, Batocera, Knulli or muOS. When a conversion replaces a file, the
        matching entry in the Platform folder's <code>gamelist.xml</code> is pointed at
        the new file before the old one is deleted, so its artwork, favourite and play
        count stay. A one-time <code>gamelist.xml.bak</code> is kept. Close ES-DE first:
        it rewrites its gamelist when it exits, which would undo the change.
      </p>
      <label class="field gamelists">
        <span>ES-DE gamelists folder (optional)</span>
        <span class="gamelists-row">
          <input
            type="text"
            placeholder="/roms/ES-DE/gamelists"
            bind:value={gamelistsDraft}
            onkeydown={(e) => e.key === 'Enter' && saveGamelistsFolder()}
          />
          <Button
            variant="secondary"
            onclick={saveGamelistsFolder}
            loading={gamelistsSaving}
            disabled={gamelistsDraft.trim() === (source.gamelistsFolder ?? '')}
          >
            Save
          </Button>
        </span>
        <span class="hint">
          Where ES-DE keeps its own gamelists (<code>ES-DE/gamelists</code>, one folder per
          system, such as <code>ps2</code>). It must be inside a Volume. When set,
          <code>&lt;folder&gt;/&lt;system&gt;/gamelist.xml</code> is updated too. Leave it
          empty for gamelists kept in the Platform folders.
        </span>
      </label>
    </div>
    <div class="master-actions">
      <Checkbox
        checked={esOn}
        label="Used by an EmulationStation frontend"
        onchange={(v) => toggleEsFrontend(v)}
      />
    </div>
  </section>

  {#if ruleNotices.checkLevel?.show}
    <section class="notice" role="status">
      <Info size={16} aria-hidden="true" />
      <div class="notice-text">
        <strong>
          {ruleCount(ruleNotices.checkLevel.switched)} that
          {ruleNotices.checkLevel.switched === 1 ? 'deletes its' : 'delete their'}
          source now Round-trip first.
        </strong>
        Before a sweep deletes a source, it checks that the new file decodes to the
        same content, and keeps the source if it does not. Each file is read once
        more, so those sweeps take longer.
        {#if ruleNotices.checkLevel.container_check}
          {ruleCount(ruleNotices.checkLevel.container_check)} on formats without
          a Round-trip yet keep the container check.
        {/if}
        To go back to the faster check, tick <em>Container check only</em> under
        a rule's delete setting.
      </div>
      <Button variant="ghost" size="sm" onclick={dismissCheckLevelNotice} icon={xIcon}>
        Dismiss
      </Button>
    </section>
  {/if}

  {#if source.sweepResult}
    {@const r = source.sweepResult}
    <section class="sweep">
      <strong>{r.dry_run ? 'Preview' : 'Last run'}:</strong>
      {r.queued ?? 0} queued
      {#if r.checks !== undefined}· {r.checks} to check{/if}
      <span class="muted">
        · {r.skipped_existing ?? 0} already converted
        · {r.skipped_filtered ?? 0} filtered out
        {#if r.skipped_active}· {r.skipped_active} already in the queue{/if}
        {#if r.skipped_missing}· {r.skipped_missing} listed but gone from disk{/if}
        {#if r.skipped_unconvertible}
          · {r.skipped_unconvertible} the target format cannot take
        {/if}
        {#if r.skipped_failed}
          · {r.skipped_failed} held after a failed conversion
        {/if}
        {#if r.skipped_unsafe}
          · {r.skipped_unsafe} not converted: a track sheet names a file outside the
          volumes
        {/if}
        {#if r.stopped_reason === 'limit'}· stopped at the per-run limit{/if}
        {#if r.stopped_reason === 'queue_full'}· stopped, queue full{/if}
        {#if r.stopped_reason === 'no_rules'}· no rules configured{/if}
        {#if r.stopped_reason === 'not_configured'}
          · stopped, this Library is not set up to be read
        {/if}
      </span>
      {#if r.errors?.length}
        <ul class="sweep-errors">
          {#each r.errors as e, i (i)}
            <li>{source.platformName(e.platform_id)}: {sweepErrorLabel(e.error)}</li>
          {/each}
        </ul>
      {/if}
      {#each r.platforms ?? [] as p (`${p.platform_id}:${p.check ? 'check' : 'convert'}`)}
        {#if p.candidates?.length}
          <details class="candidates">
            <summary>
              {source.platformName(p.platform_id)}: {p.check ? `${p.checks} to check` : p.queued}
              {#if p.check_level}
                <span class="muted">· {CHECK_LEVEL_LABELS[p.check_level] ?? p.check_level}</span>
              {/if}
            </summary>
            <!-- Keyed by index: the backend reduces candidates to basenames, so
                 two ROMs in different folders can both be "Game.iso" and a
                 value key would throw mid-render. -->
            <ul>{#each p.candidates as c, i (i)}<li>{c}</li>{/each}</ul>
          </details>
        {/if}
      {/each}
    </section>
  {/if}

  {#if source.rulesError}
    <div class="rules-error" role="alert">
      Could not load the automation rules: {source.rulesError}
    </div>
  {/if}

  {#if source.rulesLoading}
    <div class="loading"><Spinner /> Loading rules…</div>
  {:else if platforms.length === 0}
    <EmptyState
      title={source.emptyTitle}
      description={source.emptyHint}
    />
  {:else}
    <div class="rules">
      {#each platforms as platform (platform.id)}
        {@const rule = source.ruleFor(platform.id)}
        {@const configured = !!source.rules[String(platform.id)]}
        {@const check = source.checkRuleFor(platform.id)}
        {@const spec = specFor(rule.mode)}
        {@const open = expanded.has(platform.id)}
        {@const converterNote = source.converterNoteFor(platform, configured ? rule : null)}
        <article class="rule" class:configured={configured || !!check}>
          <header>
            <!-- data-platform-id: a stable handle (the label is a display name). -->
            <button
              class="row-toggle" type="button" data-platform-id={platform.id}
              onclick={() => toggle(platform.id)}
            >
              <ChevronDown size={16} class={open ? 'chev open' : 'chev'} />
              <span class="pname">{platform.name}</span>
              {#if source.imageCount(platform)}
                <span class="muted">{source.countLabel(source.imageCount(platform))}</span>
              {/if}
            </button>
            <div class="row-summary">
              {#if configured && rule.enabled}
                <Badge tone="success">{spec?.label ?? rule.mode}</Badge>
                <span class="muted">every {rule.interval_minutes} min</span>
              {:else if configured}
                <Badge tone="neutral">paused</Badge>
              {/if}
              {#if check?.enabled}
                <Badge tone="success">Check</Badge>
                <span class="muted">{checkSummary(check)}</span>
              {:else if check}
                <Badge tone="neutral">check paused</Badge>
              {/if}
              {#if !configured && !check}
                <span class="muted">not configured</span>
              {/if}
              <!-- RomM's own converter (#385): a flag, never a block. -->
              {#if converterNote?.kind === 'converts'}
                <Badge
                  tone={configured ? 'warning' : 'neutral'}
                  title="RomM's own converter also converts this Platform"
                >
                  RomM → {converterNote.target.toUpperCase()}
                </Badge>
              {:else if converterNote?.kind === 'cannot'}
                <Badge tone="info" title="RomM's converter has no format for this Platform">
                  RomM can't convert
                </Badge>
              {/if}
            </div>
          </header>

          {#if open}
            <div class="body">
              <div class="grid">
                <label class="field wide">
                  <span>Convert to</span>
                  <Select
                    value={rule.mode ?? ''}
                    options={modeOptionsFor(platform)}
                    ariaLabel="Target format"
                    onchange={(v) => update(platform.id, { mode: v })}
                  />
                </label>
              </div>

              {#if configured && converterNote?.kind === 'converts'}
                <div class="warn converter-conflict" role="note">
                  <p>RomM's own converter also converts this Platform.</p>
                  <dl class="targets">
                    <div><dt>RomM</dt><dd>{converterNote.target.toUpperCase()}</dd></div>
                    <div>
                      <dt>This Rule</dt>
                      <dd>
                        {spec?.label ?? rule.mode}{spec?.outputExt ? ` (${spec.outputExt})` : ''}
                      </dd>
                    </div>
                  </dl>
                  <p>
                    {#if converterNote.agrees}
                      Both write the same format, so whichever runs first converts
                      a game and the other finds nothing left to do.
                    {:else}
                      They write different formats, so the same game can be
                      converted twice.
                    {/if}
                    RomM deletes its sources without checking the output. Switch
                    one of them off; nothing here is blocked.
                  </p>
                </div>
              {:else if converterNote?.kind === 'cannot'}
                <p class="muted converter-cannot">
                  RomM's converter can't convert this Platform, so only
                  Compressatorium does.
                </p>
              {/if}

              {#if rule.mode}
                {@const codecs = codecsFor(rule.mode)}
                {@const style = codecStyleFor(rule.mode)}
                {@const range = ruleLevelRange(rule)}
                {@const chosen = selectedCodecs(rule)}

                <div class="grid">
                  {#if (spec?.supportsCompression || spec?.supportsCompressionLevel) && codecs.length > 0}
                    {@const compatTargets = compat.targetsForMode(rule.mode)}
                    {#if compatTargets.length > 0}
                      {@const picked = compatTargets.find((t) => t.id === compatPick[platform.id]) ?? null}
                      {@const compatNote = compatWarning(picked, rule.mode, runningCodecs(rule), compat.table)}
                      <div class="field wide">
                        <span>Compatible with…</span>
                        <Select
                          value={picked?.id ?? ''}
                          options={[
                            { value: '', label: 'Any emulator' },
                            ...compatTargets.map((t) => ({ value: t.id, label: t.label })),
                          ]}
                          ariaLabel="Compatible with"
                          onchange={(v) => chooseCompat(platform.id, rule, v)}
                        />
                        {#if compatNote}
                          <p class="warn" role="status">{compatNote} The Rule still runs.</p>
                        {/if}
                      </div>
                    {/if}
                    {#if style === 'multi'}
                      <div class="field wide">
                        <span>Compression</span>
                        <div class="chips" role="group" aria-label="Codec selection">
                          {#each codecs as codec (codec.value)}
                            {#if codec.value !== 'none'}
                              <button
                                type="button"
                                class="chip"
                                class:active={chosen.includes(codec.value)}
                                aria-pressed={chosen.includes(codec.value)}
                                title={codec.hint ?? ''}
                                onclick={() => toggleCodec(platform.id, rule, codec.value)}
                              >{codec.label}</button>
                            {/if}
                          {/each}
                        </div>
                        <span class="hint">
                          Leave all off to use {spec?.label ?? 'the tool'}'s default.
                          {#if Number.isFinite(registry.maxCodecsFor(rule.mode))}
                            Up to {registry.maxCodecsFor(rule.mode)} at once
                            ({chosen.length} selected).
                          {/if}
                        </span>
                      </div>
                    {:else}
                      <label class="field">
                        <span>Compression</span>
                        <Select
                          value={rule.compression ?? ''}
                          options={[
                            { value: '', label: defaultCodecLabel(rule.mode) },
                            ...codecs.map((c) => ({ value: c.value, label: c.label })),
                          ]}
                          ariaLabel="Compression codec"
                          onchange={(v) => setRuleCodec(platform.id, rule, v || null)}
                        />
                      </label>
                    {/if}
                  {/if}

                  {#if range}
                    <label class="field">
                      <span>Compression level</span>
                      <input
                        type="number" min={range.min} max={range.max}
                        placeholder={String(range.default)}
                        value={rule.compression_level ?? ''}
                        onchange={(e) => setRuleLevel(platform.id, rule, e.currentTarget.value)}
                      />
                      <span class="hint">{range.min}–{range.max}. Blank uses {range.default}.</span>
                    </label>
                  {/if}

                  <label class="field wide">
                    <span>If the output already exists</span>
                    <Select
                      value={rule.duplicate_action ?? 'skip'}
                      options={duplicateOptions}
                      ariaLabel="Existing output policy"
                      onchange={(v) => update(platform.id, { duplicate_action: v })}
                    />
                  </label>
                </div>

                {#if spec?.supportsSplit}
                  <div class="toggles">
                    <Checkbox
                      checked={rule.split}
                      label="Split into 4 GB parts (FAT32)"
                      description="Writes Game.iso.0, Game.iso.1, … so the image fits on FAT32."
                      onchange={(v) => update(platform.id, { split: v })}
                    />
                  </div>
                {/if}

                <div class="grid">
                  <label class="field">
                    <span>Run every (minutes)</span>
                    <input
                      type="number" min="5" max="10080"
                      value={rule.interval_minutes}
                      onchange={(e) => update(platform.id, {
                        interval_minutes: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Max jobs per run</span>
                    <input
                      type="number" min="1" max="1000"
                      value={rule.max_per_run}
                      onchange={(e) => update(platform.id, {
                        max_per_run: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  {@render schedule(rule, (patch) => update(platform.id, patch), false)}

                  <label class="field">
                    <span>Convert in this order</span>
                    <Select
                      value={rule.order}
                      options={(source.ruleOptions.orders ?? []).map((o) => ({
                        value: o, label: ORDER_LABELS[o] ?? o,
                      }))}
                      ariaLabel="Order"
                      onchange={(v) => update(platform.id, { order: v })}
                    />
                  </label>

                  <label class="field">
                    <span>Priority</span>
                    <input
                      type="number" min="-100" max="100"
                      value={rule.priority}
                      onchange={(e) => update(platform.id, {
                        priority: Number(e.currentTarget.value),
                      })}
                    />
                    <span class="hint">Lower runs first when several are due.</span>
                  </label>

                  <label class="field wide">
                    <span>Output folder</span>
                    <input
                      type="text"
                      placeholder="Leave blank to write beside the source"
                      value={rule.output_dir ?? ''}
                      onchange={(e) => update(platform.id, {
                        output_dir: e.currentTarget.value || null,
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Smallest Image (MB)</span>
                    <input
                      type="number" min="0"
                      value={rule.min_size_mb}
                      onchange={(e) => update(platform.id, {
                        min_size_mb: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Largest Image (MB, 0 = any)</span>
                    <input
                      type="number" min="0"
                      value={rule.max_size_mb}
                      onchange={(e) => update(platform.id, {
                        max_size_mb: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Only names matching</span>
                    <input
                      type="text" placeholder="e.g. \\(USA\\)"
                      value={rule.include_pattern ?? ''}
                      onchange={(e) => update(platform.id, {
                        include_pattern: e.currentTarget.value || null,
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Skip names matching</span>
                    <input
                      type="text" placeholder="e.g. \\(Beta\\)"
                      value={rule.exclude_pattern ?? ''}
                      onchange={(e) => update(platform.id, {
                        exclude_pattern: e.currentTarget.value || null,
                      })}
                    />
                  </label>
                </div>

                <div class="toggles">
                  <Checkbox
                    checked={rule.enabled}
                    label="Enabled"
                    onchange={(v) => update(platform.id, { enabled: v })}
                  />
                  <Checkbox
                    checked={rule.only_matched}
                    label={source.matchedLabel}
                    onchange={(v) => setIdFilter(platform.id, 'only_matched', v)}
                  />
                  <Checkbox
                    checked={rule.only_unmatched}
                    label={source.unmatchedLabel}
                    onchange={(v) => setIdFilter(platform.id, 'only_unmatched', v)}
                  />
                  <!-- Two capabilities, not one. A mode can be verifiable and
                       still refuse to delete its source (PS3 folder → ISO), so
                       gating the check on the delete flag hid it exactly where
                       verification is the only safety net on offer. -->
                  {#if spec?.supportsDeleteOnVerify || spec?.supportsVerify}
                    <Checkbox
                      checked={rule.verify_after || rule.delete_on_verify}
                      disabled={rule.delete_on_verify}
                      label="Verify each converted file"
                      description={spec?.supportsDeleteOnVerify
                        ? 'Deleting the source already verifies first.'
                        : `This format is checked after conversion; its source is never deleted.${spec?.sourceKeptReason ? ` ${spec.sourceKeptReason}` : ''}`}
                      onchange={(v) => update(platform.id, { verify_after: v })}
                    />
                  {/if}
                  {#if spec?.supportsDeleteOnVerify}
                    <Checkbox
                      checked={rule.delete_on_verify}
                      label="Delete the source after the new file verifies"
                      description={DELETE_CHECK_HINTS[ruleDeleteCheck(spec, rule)]}
                      onchange={(v) => update(platform.id, { delete_on_verify: v })}
                    />
                    <!-- The Rule's check level (#302). Shown where it decides
                         something, as in the convert panel: a Round-trip mode
                         only, never a Recognized one (#423). The stored level
                         is kept either way. -->
                    {#if rule.delete_on_verify && spec?.supportsRoundTrip}
                      <Checkbox
                        checked={rule.check_level === CHECK_LEVEL.containerOnly}
                        label="Container check only (faster, weaker)"
                        description="Skips the content comparison and deletes once the new file is Intact. It proves the file reads back cleanly, not that it holds the same disc."
                        onchange={(v) => update(platform.id, { check_level: v ? CHECK_LEVEL.containerOnly : CHECK_LEVEL.roundTrip })}
                      />
                    {/if}
                  {/if}
                </div>

                {#if rule.invalid_output_dir}
                  <p class="warn" role="alert">
                    <strong>{rule.invalid_output_dir}</strong> is outside the configured
                    volumes, so it was not saved and this rule is paused. Pick a folder
                    inside a mounted volume, or clear the field to write beside each source.
                  </p>
                {/if}

                {#if rule.invalid_window && rule.window_start && rule.window_start === rule.window_end}
                  <p class="warn" role="alert">
                    The time window starts and ends at the same time, so this
                    rule is paused. Give it a different end, or leave both blank
                    to convert at any hour.
                  </p>
                {:else if rule.invalid_window}
                  <p class="warn" role="alert">
                    Only one end of the time window is set, so this rule is
                    paused. A window needs both a start and an end — half of one
                    would have run all day on the days selected.
                  </p>
                {/if}
                {#if rule.invalid_pattern}
                  <p class="warn" role="alert">
                    A name filter could not be read as a regular expression, so it was
                    not saved and this rule is paused — running it unfiltered would
                    convert the whole platform.
                  </p>
                {/if}

                {#if rule.dropped_codecs?.length}
                  <p class="warn" role="status">
                    {spec?.label ?? 'This mode'} can't use
                    <strong>{rule.dropped_codecs.join(', ')}</strong> compression, so this
                    Rule runs without {rule.dropped_codecs.length === 1 ? 'it' : 'them'}.
                  </p>
                {/if}

                {#if rule.unsafe_delete_on_verify}
                  <p class="warn" role="alert">
                    Deleting the source was switched off: with this compression setting
                    the verification is only structural, which is not enough to justify
                    removing the original.
                  </p>
                {/if}

                {#if source.losesDatMatch(spec)}
                  <p class="warn">
                    RomM cannot hash-match <strong>{spec?.outputExt}</strong>, so
                    these ROMs need their metadata re-applied after RomM rescans.
                    Compressatorium saves it first and restores it for you.
                    {#if spec?.supportsSplit && rule.split}
                      <strong>Except where splitting kicks in:</strong> past 4 GB
                      this writes numbered parts instead of one file, and RomM
                      matches a ROM on a single file's hash — so those need
                      re-identifying by hand. Anything under 4 GB is restored
                      normally.
                    {/if}
                  </p>
                {/if}

                <p class="last-run muted">{lastRunLabel(platform.id)}</p>

                <!-- Whatever the policy: a held Image is skipped until one of
                     its files changes, and this is how to retry it sooner. -->
                {#if source.heldCountFor(platform.id) > 0}
                  {@const held = source.heldCountFor(platform.id)}
                  <p class="last-run muted forget">
                    <span>
                      {source.countLabel(held)} held after a failed conversion. They are
                      tried again when one of their files changes.
                    </span>
                    <Button
                      variant="ghost"
                      onclick={() => retryFailed(platform.id)}
                    >Retry failed</Button>
                  </p>
                {/if}

                {#if rule.duplicate_action !== 'skip'}
                  {@const remembered = source.convertedCountFor(platform.id)}
                  {#if remembered > 0}
                    <p class="last-run muted forget">
                      <span>
                        Remembers {remembered}
                        {remembered === 1 ? 'Image' : 'Images'} it has already converted, so
                        {duplicateLabels[rule.duplicate_action] ?? rule.duplicate_action}
                        happens once per Image instead of every run.
                      </span>
                      <Button
                        variant="ghost"
                        onclick={() => forgetConverted(platform.id)}
                      >Forget history</Button>
                    </p>
                  {/if}
                {/if}
              {:else}
                <p class="muted">Pick a target format to configure this platform.</p>
              {/if}

              <!-- The Platform's check Rule (#305), beside its conversion Rule. -->
              <section class="check-rule" aria-label="Check Rule">
                <Checkbox
                  checked={!!check}
                  label="Check these Images are Intact"
                  description="Each night, read the stalest Images through their tool's Verify. Nothing is converted, moved or deleted."
                  onchange={(v) => setChecking(platform.id, v)}
                />
                {#if check}
                  <div class="grid">
                    <label class="field">
                      <span>Check up to (GB a night)</span>
                      <input
                        type="number" min="1"
                        value={check.check_budget_gb}
                        onchange={(e) => updateCheck(platform.id, {
                          check_budget_gb: Number(e.currentTarget.value),
                        })}
                      />
                      <span class="hint">
                        Counts each Image's full size, every track.
                      </span>
                    </label>

                    {@render schedule(check, (patch) => updateCheck(platform.id, patch), true)}
                  </div>

                  <p class="hint">
                    It runs once a day, inside the window. The Images never checked
                    go first, then the ones checked longest ago, taken in that order
                    until the next one does not fit the budget.
                    An Image larger than the whole budget is checked on its own
                    rather than never. Each
                    goes through its tool's own Verify. One that fails shows up in
                    <strong>Findings</strong> as not Intact, and a later pass clears
                    it. An Image no tool here can check is skipped and costs
                    nothing. Nightly runs need <strong>Run automatically</strong>
                    on. <strong>Run now</strong> checks another budget's worth
                    straight away, on top of the nightly run.
                  </p>

                  <div class="toggles">
                    <Checkbox
                      checked={check.enabled}
                      label="Enabled"
                      onchange={(v) => updateCheck(platform.id, { enabled: v })}
                    />
                  </div>

                  {#if check.invalid_window}
                    <p class="warn" role="alert">
                      Only one end of the time window is set, so this check is
                      paused. A window needs both a start and an end.
                    </p>
                  {/if}

                  <p class="last-run muted">{lastCheckLabel(platform.id)}</p>
                {/if}
              </section>
            </div>
          {/if}
        </article>
      {/each}
    </div>

    <div class="save-bar" class:visible={dirty}>
      <span>Unsaved changes</span>
      <Button onclick={save} loading={source.rulesSaving} icon={saveIcon}>Save rules</Button>
    </div>
  {/if}
</div>

<!-- The window and weekdays, shared by the conversion and check forms. -->
<!-- equalIsAllDay: a check Rule reads the same start and end as the whole day;
     a conversion Rule is paused for it (normalize_rule). -->
{#snippet schedule(rule, change, equalIsAllDay)}
  <label class="field">
    <span>Only between</span>
    <div class="pair">
      <input
        type="time" value={rule.window_start ?? ''}
        onchange={(e) => change({ window_start: e.currentTarget.value || null })}
      />
      <span class="muted">and</span>
      <input
        type="time" value={rule.window_end ?? ''}
        onchange={(e) => change({ window_end: e.currentTarget.value || null })}
      />
    </div>
    <span class="hint">
      {#if equalIsAllDay}
        Leave blank, or give the same start and end, to run at any hour.
      {:else}
        Leave blank to run at any hour.
      {/if}
      Times are {rule.timezone || browserZone}.
    </span>
  </label>

  <div class="field wide">
    <span>On these days</span>
    <div class="days">
      {#each DAY_LABELS as label, day (label)}
        <button
          type="button"
          class="day"
          class:on={(rule.days ?? []).includes(day)}
          aria-pressed={(rule.days ?? []).includes(day)}
          onclick={() => change({ days: toggledDays(rule.days, day) })}
        >{label}</button>
      {/each}
    </div>
    <!-- An empty selection is honoured rather than widened
         back to every day, so it has to say what it means:
         the scheduler will never fire this rule. -->
    <span class="hint">
      {#if (rule.days ?? []).length === 0}
        No days selected — the scheduler will never run this
        rule. <strong>Run now</strong> still works.
      {:else}
        Scheduled runs only happen on the days selected here.
      {/if}
    </span>
  </div>
{/snippet}

{#snippet playIcon()}<Play size={14} />{/snippet}
{#snippet eyeIcon()}<Eye size={14} />{/snippet}
{#snippet xIcon()}<XIcon size={14} />{/snippet}
{#snippet saveIcon()}<Save size={14} />{/snippet}

<style>
  .sweep-errors {
    margin: 0.5rem 0 0;
    padding-left: 1.1rem;
    color: var(--warn, #b45309);
    font-size: 0.85rem;
  }

  .check-rule {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .forget {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .rules-error {
    margin: 0 0 0.75rem;
    padding: 0.6rem 0.8rem;
    border: 1px solid var(--danger, #b3261e);
    border-radius: 6px;
    color: var(--danger, #b3261e);
    font-size: 0.9rem;
  }

  .automation { display: grid; gap: var(--space-4); }
  .master {
    display: flex;
    justify-content: space-between;
    gap: var(--space-4);
    flex-wrap: wrap;
    align-items: center;
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
  }
  .master-text h2 { margin: 0; font-size: var(--text-lg); }
  .master-text p {
    margin: var(--space-1) 0 0;
    color: var(--text-2);
    font-size: var(--text-sm);
    max-width: 60ch;
  }
  .master-actions { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }

  .notice {
    display: flex;
    gap: var(--space-3);
    align-items: flex-start;
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--space-3);
    font-size: var(--text-sm);
  }
  .notice-text { flex: 1; min-width: 0; }
  .sweep {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--space-3);
  }
  .candidates { margin-top: var(--space-2); }
  .candidates ul { margin: var(--space-1) 0 0; padding-left: var(--space-4); color: var(--text-2); }

  .rules { display: grid; gap: var(--space-2); }
  .rule {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }
  .rule.configured { border-color: var(--accent); }
  .rule header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    flex-wrap: wrap;
  }
  .row-toggle {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    background: none;
    border: 0;
    color: inherit;
    font: inherit;
    cursor: pointer;
    padding: var(--space-1);
    min-width: 0;
  }
  .row-toggle :global(.chev) { transition: transform 120ms ease; }
  .row-toggle :global(.chev.open) { transform: rotate(180deg); }
  .pname { font-weight: var(--weight-semibold); }
  .row-summary { display: flex; align-items: center; gap: var(--space-2); }

  .body { padding: 0 var(--space-3) var(--space-3); display: grid; gap: var(--space-3); }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: var(--space-3);
  }
  .field { display: grid; gap: 4px; min-width: 0; }
  .field.wide { grid-column: 1 / -1; }
  .field > span:first-child { font-size: var(--text-sm); font-weight: var(--weight-medium); }
  .field input {
    padding: var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-1);
    font: inherit;
    min-width: 0;
  }
  .pair { display: flex; align-items: center; gap: var(--space-2); }
  .field.gamelists { margin-top: var(--space-3); max-width: 60ch; }
  .gamelists-row { display: flex; gap: var(--space-2); align-items: center; }
  .gamelists-row input { flex: 1; }
  .hint, .muted { color: var(--text-2); font-size: var(--text-sm); }

  /* Codec chips for the multi-codec style (chdman). Mirrors CompressionPicker's
     affordance so the rule editor and the convert panel read the same way. */
  .chips { display: flex; flex-wrap: wrap; gap: 0.35rem; }
  .chip {
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface-2);
    color: var(--text-1);
    font-size: var(--text-sm);
    cursor: pointer;
  }
  .chip:hover { border-color: var(--accent); }
  .chip.active {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-contrast, #fff);
  }

  .days { display: flex; gap: 4px; flex-wrap: wrap; }
  .day {
    padding: 4px 10px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    font-size: var(--text-sm);
  }
  .day.on { background: var(--accent); border-color: var(--accent); color: #fff; }

  .toggles { display: grid; gap: var(--space-1); }
  .warn {
    margin: 0;
    padding: var(--space-2);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    border: 1px solid var(--warning-border, var(--border-subtle));
    font-size: var(--text-sm);
  }
  .last-run { margin: 0; }
  .converter-conflict {
    display: grid;
    gap: var(--space-2);
    background: var(--warning-muted);
    border-color: var(--warning);
  }
  .converter-conflict p { margin: 0; }
  .converter-conflict .targets {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-4);
    margin: 0;
  }
  .converter-conflict .targets div { display: flex; gap: var(--space-2); }
  .converter-conflict dt { color: var(--text-2); }
  .converter-conflict dd { margin: 0; font-weight: var(--weight-semibold); }
  .converter-cannot { margin: 0; font-size: var(--text-sm); }

  .save-bar {
    position: sticky;
    bottom: var(--space-3);
    display: none;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3);
    border-radius: var(--radius-lg);
    background: var(--surface-1);
    border: 1px solid var(--accent);
    box-shadow: var(--elev-2, var(--elev-1));
  }
  .save-bar.visible { display: flex; }
</style>
