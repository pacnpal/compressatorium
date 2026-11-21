<script>
  /**
   * One console's settings past the three in its row (RuleTable): how it
   * compresses, where new files go, which games, when it runs, and its nightly
   * re-check. Every change goes out through `onupdate(patch, delay)` (the
   * console's conversion Rule) or `oncheck(patch, delay)` (its re-check Rule,
   * null to remove it); the table saves them.
   *
   * @typedef {Object} Props
   * @property {object} source the Library's rules source
   * @property {object} platform
   * @property {object} rule
   * @property {object|null} check
   * @property {(patch: object, delay?: number) => void} onupdate
   * @property {(patch: object|null, delay?: number) => void} oncheck
   */
  import { toast } from 'svelte-sonner';
  import { registry } from '$lib/tools/registry.js';
  import { fromRule, toRule } from '$lib/tools/compression.js';
  import { api } from '$lib/api/endpoints.js';
  import { numberValue } from '$lib/rules/ruleTable.js';
  import { safetyControls } from '$lib/util/safetyOptions.js';
  import { CHECK_LEVEL } from '$lib/util/checkLevels.js';
  import { DAY_LABELS, ORDER_LABELS } from '$lib/stores/romm.svelte.js';
  import CompressionPicker from '$lib/components/panels/CompressionPicker.svelte';
  import FolderPickerModal from '$lib/components/modals/FolderPickerModal.svelte';
  import BaseModal from '$lib/components/modals/BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Select from '$lib/components/ui/Select.svelte';

  /** @type {Props} */
  let { source, platform, rule, check, onupdate, oncheck } = $props();

  const TEXT_DELAY = 600;
  const spec = $derived(rule.mode ? registry.specFor(rule.mode) : null);
  const compresses = $derived(!!(spec?.supportsCompression || spec?.supportsCompressionLevel));
  const quick = $derived(
    safetyControls(spec ? [spec] : [], { deleteOriginal: !!rule.delete_on_verify }).showQuick,
  );

  const DUPLICATE_LABELS = { skip: 'Skip it', rename: 'Keep both', overwrite: 'Replace it' };
  const duplicateOptions = $derived(
    (source.ruleOptions?.duplicate_actions ?? ['skip']).map(
      (value) => ({ value, label: DUPLICATE_LABELS[value] ?? value }),
    ),
  );
  const orderOptions = $derived(
    (source.ruleOptions?.orders ?? []).map((o) => ({ value: o, label: ORDER_LABELS[o] ?? o })),
  );

  let pickingFolder = $state(false);
  let confirmForget = $state(false);
  let forgetting = $state(false);

  /** *days* with *day* switched on or off, in weekday order. */
  function toggledDays(days, day) {
    const list = days ?? [];
    const next = list.includes(day) ? list.filter((d) => d !== day) : [...list, day];
    return next.sort((a, b) => a - b);
  }

  /** The two "which games" filters can't both be on: ticking one clears the other. */
  function setIdFilter(field, value) {
    const other = field === 'only_matched' ? 'only_unmatched' : 'only_matched';
    onupdate({ [field]: value, ...(value ? { [other]: false } : {}) });
  }

  /** A number field's change: saved only when it holds a number. */
  function setNumber(field, e, change = onupdate) {
    const value = numberValue(e.currentTarget.value);
    if (value !== null) change({ [field]: value });
  }

  // Name filters as typed. A pattern the server can't read is shown with
  // why and never saved: the server would null it and pause the console.
  // The server checks it with the same compile a save runs (Python's `re`,
  // not the browser's), once typing stops; only an answer for the text still
  // in the box counts, and only a valid one is saved.
  let drafts = $state({});
  let errors = $state({});
  const timers = {};
  const patternError = (field) => errors[field] ?? null;
  function setPattern(field, text) {
    drafts = { ...drafts, [field]: text };
    clearTimeout(timers[field]);
    if (!text.trim()) {
      errors = { ...errors, [field]: null };
      onupdate({ [field]: null });
      return;
    }
    timers[field] = setTimeout(() => checkPattern(field, text), TEXT_DELAY);
  }
  async function checkPattern(field, text) {
    let error;
    try {
      const answer = await api.checkRulePattern(text);
      error = answer?.ok ? null : (answer?.error ?? 'not a valid pattern');
    } catch (e) {
      error = `couldn't check it: ${e?.message ?? 'the server did not answer'}`;
    }
    if (drafts[field] !== text) return;
    errors = { ...errors, [field]: error };
    if (!error) onupdate({ [field]: text });
  }
  const patternValue = (field) => drafts[field] ?? rule[field] ?? '';

  async function forget() {
    forgetting = true;
    try {
      await source.forgetConverted([String(platform.id)]);
      toast.success(`Every ${platform.name} game is looked at again on the next run`);
      confirmForget = false;
    } catch (e) {
      toast.error(e?.message ?? "Couldn't clear it");
    } finally {
      forgetting = false;
    }
  }

  /**
   * "Last run 3 Jun, 01:00 · queued 4", from a Rule's run state: the latest
   * of its scheduled and manual runs (a re-check keeps both).
   */
  function lastRun(state, verb, countKey) {
    const stamps = [state?.last_run_at, state?.last_manual_run_at]
      .map((s) => (s ? new Date(s) : null))
      .filter((d) => d && !Number.isNaN(d.getTime()));
    if (!stamps.length) return 'Never run yet';
    const when = new Date(Math.max(...stamps.map((d) => d.getTime())));
    return `Last run ${when.toLocaleString()} · ${verb} ${state?.[countKey] ?? 0}`;
  }

  // Releases the failed holds only: what was converted stays remembered, so
  // a rename or overwrite console never converts it again (#541).
  async function retryFailed() {
    try {
      await source.forgetConverted([String(platform.id)], { failedOnly: true });
      toast.success('The next run tries those games again');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't release them");
    }
  }
</script>

<div class="advanced">
  {#if compresses}
    <fieldset>
      <legend>Compression</legend>
      <CompressionPicker
        mode={rule.mode}
        value={fromRule(rule)}
        compact
        onchange={(v) => onupdate(toRule(rule.mode, v))}
      />
    </fieldset>
  {/if}

  {#if quick}
    <fieldset>
      <legend>Check</legend>
      <Checkbox
        checked={rule.check_level === CHECK_LEVEL.containerOnly}
        label="Quick check only (faster, less thorough)"
        description="Checks the new file's structure instead of comparing it to the original."
        onchange={(v) => onupdate({
          check_level: v ? CHECK_LEVEL.containerOnly : CHECK_LEVEL.roundTrip,
        })}
      />
    </fieldset>
  {/if}

  <fieldset>
    <legend>Files</legend>
    <div class="radios" role="radiogroup" aria-label="Where the new files go">
      <label>
        <input
          type="radio" name={`out-${platform.id}`} checked={!rule.output_dir}
          onchange={() => onupdate({ output_dir: null })}
        />
        Next to the original
      </label>
      <label>
        <input
          type="radio" name={`out-${platform.id}`} checked={!!rule.output_dir}
          onchange={() => (pickingFolder = true)}
        />
        In another folder…
      </label>
      {#if rule.output_dir}
        <span class="chosen">
          <code>{rule.output_dir}</code>
          <Button size="sm" variant="ghost" onclick={() => (pickingFolder = true)}>Change</Button>
        </span>
      {/if}
    </div>
    <Select
      label="If the new file already exists"
      value={rule.duplicate_action ?? 'skip'}
      options={duplicateOptions}
      onchange={(v) => onupdate({ duplicate_action: v })}
    />
    {#if spec?.supportsSplit}
      <Checkbox
        checked={!!rule.split}
        label="Split into 4 GB parts"
        description="For FAT32 drives."
        onchange={(v) => onupdate({ split: v })}
      />
    {/if}
  </fieldset>

  <fieldset>
    <legend>Which games</legend>
    <div class="pair">
      <label class="field">
        <span>Only games between (MB)</span>
        <input type="number" min="0" value={rule.min_size_mb ?? 0}
          onchange={(e) => setNumber('min_size_mb', e)} />
      </label>
      <label class="field">
        <span>and (MB, 0 = any)</span>
        <input type="number" min="0" value={rule.max_size_mb ?? 0}
          onchange={(e) => setNumber('max_size_mb', e)} />
      </label>
    </div>
    <label class="field">
      <span>Only names matching</span>
      <input type="text" placeholder="e.g. \(USA\)" value={patternValue('include_pattern')}
        aria-invalid={!!patternError('include_pattern')}
        oninput={(e) => setPattern('include_pattern', e.currentTarget.value)} />
      {#if patternError('include_pattern')}
        <span class="bad" role="alert">Not saved: {patternError('include_pattern')}</span>
      {/if}
    </label>
    <label class="field">
      <span>Skip names matching</span>
      <input type="text" placeholder="e.g. \(Beta\)" value={patternValue('exclude_pattern')}
        aria-invalid={!!patternError('exclude_pattern')}
        oninput={(e) => setPattern('exclude_pattern', e.currentTarget.value)} />
      {#if patternError('exclude_pattern')}
        <span class="bad" role="alert">Not saved: {patternError('exclude_pattern')}</span>
      {/if}
    </label>
    <Checkbox
      checked={!!rule.only_matched}
      label={source.matchedLabel}
      onchange={(v) => setIdFilter('only_matched', v)}
    />
    <Checkbox
      checked={!!rule.only_unmatched}
      label={source.unmatchedLabel}
      onchange={(v) => setIdFilter('only_unmatched', v)}
    />
  </fieldset>

  <fieldset>
    <legend>When</legend>
    <label class="field">
      <span>Every (minutes)</span>
      <input type="number" min="5" max="10080" value={rule.interval_minutes}
        onchange={(e) => setNumber('interval_minutes', e)} />
    </label>
    {@render schedule(rule, onupdate, false)}
    <div class="pair">
      <label class="field">
        <span>At most, per run</span>
        <input type="number" min="1" max="1000" value={rule.max_per_run}
          onchange={(e) => setNumber('max_per_run', e)} />
      </label>
      <label class="field">
        <span>Priority</span>
        <input type="number" min="-100" max="100" value={rule.priority}
          onchange={(e) => setNumber('priority', e)} />
      </label>
    </div>
    {#if orderOptions.length}
      <Select label="Order" value={rule.order} options={orderOptions}
        onchange={(v) => onupdate({ order: v })} />
    {/if}
  </fieldset>

  <fieldset>
    <legend>Re-check this console</legend>
    <Checkbox
      checked={!!check?.enabled}
      label="Re-check games every night"
      description="Reads converted games back to catch damage early. Nothing is changed."
      onchange={(v) => oncheck(v ? { enabled: true } : (check ? { enabled: false } : null))}
    />
    {#if check}
      {#if check.invalid_window}
        <p class="bad-line" role="alert">
          Only one end of the time window is set, so the re-check is paused.
        </p>
      {/if}
      <label class="field">
        <span>Up to (GB a night)</span>
        <input type="number" min="1" value={check.check_budget_gb}
          onchange={(e) => setNumber('check_budget_gb', e, oncheck)} />
      </label>
      {@render schedule(check, oncheck, true)}
    {/if}
  </fieldset>

  <footer class="foot">
    <span class="muted last">{lastRun(source.lastRunFor(platform.id), 'queued', 'last_queued')}</span>
    {#if check}
      <span class="muted last">Re-check: {lastRun(source.lastCheckFor(platform.id), 'checked', 'last_checks')}</span>
    {/if}
    {#if source.heldCountFor(platform.id) > 0}
      {@const held = source.heldCountFor(platform.id)}
      <span class="muted">
        {held} {held === 1 ? 'game' : 'games'} held after a failed conversion. Each is
        tried again when it changes.
      </span>
      <Button size="sm" variant="ghost" onclick={retryFailed}>Try them again</Button>
    {/if}
    <Button size="sm" variant="ghost" onclick={() => (confirmForget = true)}>
      Forget what's been converted
    </Button>
  </footer>
</div>

{#snippet schedule(target, change, equalIsAllDay)}
  <div class="pair">
    <label class="field">
      <span>Only between</span>
      <input type="time" value={target.window_start ?? ''}
        onchange={(e) => change({ window_start: e.currentTarget.value || null })} />
    </label>
    <label class="field">
      <span>and</span>
      <input type="time" value={target.window_end ?? ''}
        onchange={(e) => change({ window_end: e.currentTarget.value || null })} />
    </label>
  </div>
  <!-- A re-check reads the same start and end as the whole day; a
       conversion is paused for it (RuleTable says so). -->
  <span class="muted">
    Times are {target.timezone || 'UTC'}.
    {#if equalIsAllDay}Leave both blank, or give the same start and end, to run at any hour.{:else}Leave both blank to run at any hour.{/if}
  </span>
  <div class="days" role="group" aria-label="On these days">
    {#each DAY_LABELS as label, day (label)}
      <button
        type="button" class="day" class:on={(target.days ?? []).includes(day)}
        aria-pressed={(target.days ?? []).includes(day)}
        onclick={() => change({ days: toggledDays(target.days, day) })}
      >{label}</button>
    {/each}
  </div>
  {#if (target.days ?? []).length === 0}
    <span class="muted">No days picked, so it only runs when you press Run now.</span>
  {/if}
{/snippet}

<FolderPickerModal
  open={pickingFolder}
  start={rule.output_dir}
  title="Where the new files go"
  confirmLabel="Put them here"
  onClose={() => (pickingFolder = false)}
  onpick={(path) => { pickingFolder = false; onupdate({ output_dir: path }); }}
/>

<BaseModal
  open={confirmForget}
  onClose={() => (confirmForget = false)}
  title="Forget what's been converted?"
  description={`Compressatorium will look at every ${platform.name} game again on the next run. Nothing on disk changes.`}
  size="sm"
>
  {#snippet footer()}
    <Button variant="secondary" onclick={() => (confirmForget = false)}>Cancel</Button>
    <Button variant="primary" onclick={forget} loading={forgetting}>Forget</Button>
  {/snippet}
</BaseModal>

<style>
  .advanced {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
    padding: var(--space-4);
    background: var(--surface-2);
    border-radius: var(--radius-md);
  }
  @media (min-width: 900px) {
    .advanced { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  fieldset {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }
  legend {
    padding: 0;
    margin-bottom: var(--space-2);
    font-size: var(--text-sm);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .field { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .field span { font-size: var(--text-xs); color: var(--text-2); }
  .field .bad { color: var(--error); }
  .field input {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2);
    min-width: 0;
  }
  .pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
  .radios { display: flex; flex-direction: column; gap: var(--space-2); font-size: var(--text-sm); }
  .radios label { display: inline-flex; gap: var(--space-2); align-items: center; }
  .chosen { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
  .chosen code { overflow-wrap: anywhere; font-size: var(--text-xs); }
  .days { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .day {
    padding: var(--space-1) var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--surface-1);
    color: var(--text-2);
    font: inherit;
    font-size: var(--text-xs);
    cursor: pointer;
  }
  .day.on { background: var(--accent-muted); color: var(--accent); border-color: var(--accent); }
  .muted { color: var(--text-2); font-size: var(--text-sm); }
  .bad-line { margin: 0; color: var(--error); font-size: var(--text-sm); }
  .last { margin-right: auto; }
  .foot {
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    justify-content: flex-end;
  }
</style>
