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

  const num = (e) => Number(e.currentTarget.value);

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

  async function retryFailed() {
    try {
      await source.forgetConverted([String(platform.id)]);
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
          onchange={(e) => onupdate({ min_size_mb: num(e) })} />
      </label>
      <label class="field">
        <span>and (MB, 0 = any)</span>
        <input type="number" min="0" value={rule.max_size_mb ?? 0}
          onchange={(e) => onupdate({ max_size_mb: num(e) })} />
      </label>
    </div>
    <label class="field">
      <span>Only names matching</span>
      <input type="text" placeholder="e.g. \(USA\)" value={rule.include_pattern ?? ''}
        oninput={(e) => onupdate({ include_pattern: e.currentTarget.value || null }, TEXT_DELAY)} />
    </label>
    <label class="field">
      <span>Skip names matching</span>
      <input type="text" placeholder="e.g. \(Beta\)" value={rule.exclude_pattern ?? ''}
        oninput={(e) => onupdate({ exclude_pattern: e.currentTarget.value || null }, TEXT_DELAY)} />
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
        onchange={(e) => onupdate({ interval_minutes: num(e) })} />
    </label>
    {@render schedule(rule, onupdate)}
    <div class="pair">
      <label class="field">
        <span>At most, per run</span>
        <input type="number" min="1" max="1000" value={rule.max_per_run}
          onchange={(e) => onupdate({ max_per_run: num(e) })} />
      </label>
      <label class="field">
        <span>Priority</span>
        <input type="number" min="-100" max="100" value={rule.priority}
          onchange={(e) => onupdate({ priority: num(e) })} />
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
      <label class="field">
        <span>Up to (GB a night)</span>
        <input type="number" min="1" value={check.check_budget_gb}
          onchange={(e) => oncheck({ check_budget_gb: num(e) })} />
      </label>
      {@render schedule(check, oncheck)}
    {/if}
  </fieldset>

  <footer class="foot">
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

{#snippet schedule(target, change)}
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
  .foot {
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    justify-content: flex-end;
  }
</style>
