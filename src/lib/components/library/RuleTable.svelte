<script>
  /**
   * A Library's consoles, one row each, with the three choices most people
   * make: what to convert to, whether to check each new file, and whether to
   * delete the original once it checks out. Everything else is under the
   * row's More settings (RuleAdvanced). Each change saves on its own through
   * the Library's rules source (`scheduleSave`), which sends saves one at a
   * time in order.
   *
   * @typedef {Object} Props
   * @property {object} source the Library's rules source
   */
  import { SvelteSet } from 'svelte/reactivity';
  import { registry } from '$lib/tools/registry.js';
  import { convertTargets, targetLabel } from '$lib/tools/convertTargets.js';
  import { newCheckRule } from '$lib/rules/checkRule.js';
  import { newRuleSafety, ruleUpdate } from '$lib/rules/ruleTable.js';
  import { CHECK_LEVEL } from '$lib/util/checkLevels.js';
  import { DELETE_CHECK_HINTS, deleteCheckFor } from '$lib/util/deleteCheck.js';
  import { defaults } from '$lib/stores/defaults.svelte.js';
  import RuleAdvanced from './RuleAdvanced.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import SlidersHorizontal from '@lucide/svelte/icons/sliders-horizontal';

  /** @type {Props} */
  let { source } = $props();

  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const open = new SvelteSet();

  /**
   * What a console can be converted to: the formats its tools make, narrowed
   * by the backend to that console (`mode_ids` / `tool_ids`) and to what may
   * run unattended. Unpacking isn't offered: automation makes games smaller.
   */
  function targetGroups(platform) {
    return convertTargets({
      perMode: true,
      modeIds: Array.isArray(platform?.mode_ids) ? platform.mode_ids : null,
      toolIds: Array.isArray(platform?.tool_ids) ? platform.tool_ids : null,
    })
      .filter((g) => g.group !== 'Recommended' && g.group !== 'Unpack')
      .map((g) => ({ ...g, options: g.options.filter((o) => source.modeIsAutomatable(o.value)) }))
      .filter((g) => g.options.length > 0);
  }

  function offers(groups, mode) {
    return groups.some((g) => g.options.some((o) => o.value === mode));
  }

  function specOf(mode) {
    return mode ? registry.specFor(mode) : null;
  }

  // The check a delete-original job from this console passes before the
  // original goes (#302, #423): the helper the Convert panel reads, so the
  // two never differ. A Recognized mode ignores the stored quick check.
  function ruleDeleteCheck(spec, rule) {
    return deleteCheckFor({
      supportsRoundTrip: !!spec?.supportsRoundTrip,
      containerOnly: rule.check_level === CHECK_LEVEL.containerOnly,
      deleteProof: spec?.deleteProof,
      roundTripRequired: !!spec?.roundTripRequired,
    });
  }

  /** Change one console's conversion Rule, then save after *delay* ms. */
  function update(platformId, patch, delay = 0) {
    const key = String(platformId);
    const isNew = !source.rules[key];
    let extra = {};
    if (isNew && patch.mode) {
      const spec = specOf(patch.mode);
      extra = newRuleSafety(spec, defaults.values);
      if (extra.delete_on_verify && defaults.values.container_only
        && spec?.supportsRoundTrip && !spec?.roundTripRequired) {
        extra.check_level = CHECK_LEVEL.containerOnly;
      }
    }
    const rule = ruleUpdate(source.ruleFor(platformId), { ...extra, ...patch }, { isNew, zone });
    if (!rule.mode) source.removeRule(platformId);
    else source.setRule(platformId, rule);
    source.scheduleSave(delay);
  }

  /** Change one console's re-check Rule (null removes it), then save. */
  function updateCheck(platformId, patch, delay = 0) {
    if (patch === null) {
      source.removeCheckRule(platformId);
    } else {
      const current = source.checkRuleFor(platformId)
        ?? { ...newCheckRule(source.ruleDefaults), timezone: zone };
      source.setCheckRule(platformId, { ...current, ...patch });
    }
    source.scheduleSave(delay);
  }

  function toggle(platformId) {
    if (open.has(platformId)) open.delete(platformId);
    else open.add(platformId);
  }

  /** The server's notes on a saved Rule, in plain words. */
  function notices(rule, spec) {
    const out = [];
    if (rule.invalid_output_dir) {
      out.push(`${rule.invalid_output_dir} isn't on a Drive, so this console is paused. Pick another folder.`);
    }
    if (rule.invalid_pattern) {
      out.push("A name filter isn't a valid pattern, so this console is paused.");
    }
    if (rule.invalid_window) {
      out.push(rule.window_start && rule.window_start === rule.window_end
        ? 'The time window starts and ends at the same time, so this console is paused.'
        : 'Only one end of the time window is set, so this console is paused.');
    }
    if (rule.unsafe_delete_on_verify) {
      out.push("Delete the original was turned off: with this compression the check can't prove an exact copy.");
    }
    if (rule.dropped_codecs?.length) {
      out.push(`${targetLabel(rule.mode)} can't use ${rule.dropped_codecs.join(', ')}, so it runs without.`);
    }
    if (rule.dropped_timezone) {
      out.push(`The time zone ${rule.dropped_timezone} isn't known here, so times are UTC.`);
    }
    if (source.losesDatMatch(spec)) {
      out.push(`RomM can't match ${spec.outputExt?.replace(/^\./, '').toUpperCase()} files by hash. `
        + "Compressatorium keeps their RomM details and puts them back after RomM's next scan."
        // A split build's numbered parts have no single file for RomM to hash.
        + (spec.supportsSplit && rule.split
          ? ' Not for a game split into 4 GB parts: RomM needs those identified by hand.'
          : ''));
    }
    return out;
  }
</script>

<div class="table" role="table" aria-label="Consoles">
  <div class="row head" role="row">
    <span role="columnheader">Console</span>
    <span role="columnheader">Convert to</span>
    <span role="columnheader">Check after</span>
    <span role="columnheader">Delete original</span>
    <span role="columnheader"><span class="sr-only">More settings</span></span>
  </div>
  {#each source.platforms as platform (platform.id)}
    {@const key = String(platform.id)}
    {@const configured = !!source.rules[key]}
    {@const rule = source.ruleFor(platform.id)}
    {@const mode = configured ? rule.mode : ''}
    {@const spec = specOf(mode)}
    {@const groups = targetGroups(platform)}
    {@const check = source.checkRuleFor(platform.id)}
    {@const note = source.converterNoteFor(platform, configured ? rule : null)}
    {@const games = source.imageCount(platform)}
    {@const warnings = configured ? notices(rule, spec) : []}
    <div class="row" role="row" data-platform-id={platform.id}>
      <span class="console" role="cell">
        <strong>{platform.name}</strong>
        <span class="muted">{games} {games === 1 ? 'game' : 'games'}</span>
        {#if note?.kind === 'converts'}
          <Badge tone={configured ? 'warning' : 'neutral'} title="RomM's own converter also converts this console">
            RomM → {note.target.toUpperCase()}
          </Badge>
        {:else if note?.kind === 'cannot'}
          <Badge tone="info" title="RomM's own converter has no format for this console">
            RomM can't convert
          </Badge>
        {/if}
      </span>
      <span role="cell" class="target">
        <select
          aria-label={`Convert ${platform.name} to`}
          value={mode}
          onchange={(e) => update(platform.id, { mode: e.currentTarget.value })}
        >
          <option value="">Don't convert</option>
          {#if mode && !offers(groups, mode)}
            <option value={mode}>{targetLabel(mode)} (not available here)</option>
          {/if}
          {#each groups as g (g.group)}
            <optgroup label={g.group}>
              {#each g.options as o (o.value)}
                <option value={o.value}>{o.label} · {o.hint}</option>
              {/each}
            </optgroup>
          {/each}
        </select>
        {#if configured}
          <!-- Its own switch: picking the same format again isn't a retarget,
               so a console paused any way is turned back on here. -->
          <label class="on">
            <input
              type="checkbox"
              checked={!!rule.enabled}
              onchange={(e) => update(platform.id, { enabled: e.currentTarget.checked })}
            />
            <span>{rule.enabled ? 'On' : 'Paused'}</span>
          </label>
        {/if}
      </span>
      <span role="cell" class="check">
        {#if spec?.supportsVerify || spec?.supportsDeleteOnVerify}
          <label title={rule.delete_on_verify ? 'Deleting always checks first' : undefined}>
            <input
              type="checkbox"
              checked={!!(rule.verify_after || rule.delete_on_verify)}
              disabled={!!rule.delete_on_verify}
              onchange={(e) => update(platform.id, { verify_after: e.currentTarget.checked })}
            />
            <span class="label-sm">Check after</span>
          </label>
        {/if}
      </span>
      <span role="cell" class="check">
        {#if spec}
          <label title={spec.supportsDeleteOnVerify
            ? DELETE_CHECK_HINTS[ruleDeleteCheck(spec, rule)]
            : (spec.sourceKeptReason ?? "This format can't be checked well enough")}>
            <input
              type="checkbox"
              checked={!!rule.delete_on_verify}
              disabled={!spec.supportsDeleteOnVerify}
              onchange={(e) => update(platform.id, { delete_on_verify: e.currentTarget.checked })}
            />
            <span class="label-sm">Delete original</span>
          </label>
        {/if}
      </span>
      <span role="cell" class="more">
        {#if configured}
          <IconButton
            label={`More settings for ${platform.name}`}
            active={open.has(platform.id)}
            toggle
            onclick={() => toggle(platform.id)}
          >
            <SlidersHorizontal size={16} />
          </IconButton>
        {/if}
      </span>
      {#each warnings as w (w)}
        <p class="warn" role="status">{w}</p>
      {/each}
      {#if configured && open.has(platform.id)}
        <div class="expanded">
          <RuleAdvanced
            {source}
            {platform}
            {rule}
            {check}
            onupdate={(patch, delay) => update(platform.id, patch, delay)}
            oncheck={(patch, delay) => updateCheck(platform.id, patch, delay)}
          />
        </div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .table { display: flex; flex-direction: column; min-width: 0; }
  .row {
    display: grid;
    grid-template-columns: minmax(0, 1.2fr) minmax(0, 1.6fr) 8rem 9rem 2.5rem;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    padding: var(--space-3) 0;
    border-bottom: 1px solid var(--border-subtle);
  }
  .row.head {
    padding-top: 0;
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .console { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-1) var(--space-2); min-width: 0; }
  .target { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  select {
    width: 100%;
    min-width: 0;
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2);
    cursor: pointer;
  }
  .on { display: inline-flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm); color: var(--text-2); cursor: pointer; }
  .on input { accent-color: var(--accent); width: 16px; height: 16px; }
  .check label { display: inline-flex; align-items: center; gap: var(--space-2); cursor: pointer; }
  .check input { accent-color: var(--accent); width: 16px; height: 16px; }
  .check input:disabled { cursor: not-allowed; }
  .label-sm { display: none; font-size: var(--text-sm); color: var(--text-2); }
  .more { display: flex; justify-content: flex-end; }
  .muted { color: var(--text-2); font-size: var(--text-sm); }
  .warn {
    grid-column: 1 / -1;
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-1);
  }
  .expanded { grid-column: 1 / -1; }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  @media (max-width: 720px) {
    .row { grid-template-columns: minmax(0, 1fr) auto; }
    .row.head { display: none; }
    .console, .target { grid-column: 1 / -1; }
    .more { grid-column: 2; grid-row: 1; }
    .console { grid-column: 1; }
    .label-sm { display: inline; }
  }
</style>
