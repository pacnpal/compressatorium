<script>
  // The Convert panel: pick games, see the plan, press Convert. Each game goes
  // to its best format unless "Convert to" names one. The plan is fetched as
  // the selection changes and shows every game's new format before anything
  // runs; Convert queues exactly that plan through /api/jobs/auto.
  import { onDestroy, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { conversion, PLAN_LIVE_LIMIT } from '$lib/stores/conversion.svelte.js';
  import { defaults, SAFE_DEFAULTS } from '$lib/stores/defaults.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { convertTargets, formatOfPath, targetLabel } from '$lib/tools/convertTargets.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import CompressionPicker from './CompressionPicker.svelte';
  import SafetyOptions from './SafetyOptions.svelte';
  import ConvertPlan from './ConvertPlan.svelte';
  import BaseModal from '$lib/components/modals/BaseModal.svelte';
  import DeletePlanModal from '$lib/components/modals/DeletePlanModal.svelte';
  import FolderPickerModal from '$lib/components/modals/FolderPickerModal.svelte';
  import Play from '@lucide/svelte/icons/play';
  import Loader from '@lucide/svelte/icons/loader-circle';

  // The rows Convert takes (a folder only for a folder mode, an archive only
  // for a target that reads the archive), and their paths.
  const selected = $derived(fileBrowser.convertibleSelection);
  const paths = $derived(selected.map((e) => e.path));
  const count = $derived(fileBrowser.selectedFiles.size);
  const big = $derived(paths.length > PLAN_LIVE_LIMIT);

  // Formats a tool that can run here makes from what is selected; on a RomM
  // console, only what that console allows.
  const ready = $derived(ui.toolsKnown ? ui.visibleTools.map((t) => t.id) : null);
  const noConverters = $derived(ui.toolsKnown && ui.visibleTools.length === 0);
  // A RomM catalog offers only what its platform allows (GameCube: Dolphin,
  // never CHDMAN); while its platform or rows are still loading (a platform
  // switch leaves the old rows on screen until the new ones land) it offers
  // nothing, and Convert waits.
  const platform = $derived(
    fileBrowser.rommPlatformId === null
      ? null
      : (romm.platforms.find((p) => p.id === fileBrowser.rommPlatformId) ?? null),
  );
  const catalogLoading = $derived(
    fileBrowser.rommPlatformId !== null && (!platform || fileBrowser.loading),
  );
  const targetGroups = $derived(catalogLoading ? [] : convertTargets({
    entries: selected.length ? selected : null,
    ready,
    modeIds: platform?.mode_ids ?? null,
    toolIds: platform?.tool_ids ?? null,
  }));

  // Convert to is session-only: Best again once nothing is selected, and
  // again whenever the selection no longer offers what was picked.
  $effect(() => {
    if (count === 0) untrack(() => conversion.chooseTarget(''));
  });
  $effect(() => {
    const offered = targetGroups.flatMap((g) => g.options.map((o) => o.value));
    untrack(() => {
      if (selected.length && conversion.target && !offered.includes(conversion.target)) {
        conversion.chooseTarget('');
      }
    });
  });

  // The safety choices start from Settings → Defaults, once a session.
  $effect(() => {
    if (defaults.loaded) untrack(() => conversion.seedSafety(defaults.values));
  });
  const safety = $derived(conversion.safety ?? {
    checkAfter: SAFE_DEFAULTS.verify_after,
    deleteOriginal: SAFE_DEFAULTS.delete_on_verify,
    quickCheck: SAFE_DEFAULTS.container_only,
  });

  // Plan as the selection, Convert to, the output folder, a picked console or
  // a compression change move, debounced and abortable in the store.
  $effect(() => {
    const now = paths;
    const key = conversion.keyFor(now);
    untrack(() => {
      if (!now.length || now.length > PLAN_LIVE_LIMIT) {
        conversion.clearPlan();
        return;
      }
      if (conversion.planKey !== key) conversion.requestPlan(now);
    });
  });

  const plan = $derived(conversion.hasPlanFor(paths) ? conversion.plan : null);
  const shownPlan = $derived(plan ?? conversion.plan);
  const actionable = $derived((plan ?? []).filter((row) => row.mode));

  // The modes the plan uses, each with how many games it takes.
  const modeCounts = $derived.by(() => {
    const counts = {};
    for (const row of actionable) counts[row.mode] = (counts[row.mode] ?? 0) + 1;
    return counts;
  });
  const specs = $derived(
    Object.entries(modeCounts)
      .map(([mode, games]) => ({ ...(registry.specFor(mode) ?? { mode }), games })),
  );
  // One compression choice per tool the plan uses, labelled by its format.
  const pickers = $derived.by(() => {
    const byTool = {};
    for (const row of actionable) {
      const spec = registry.specFor(row.mode);
      const tool = registry.toolForMode(row.mode);
      if (!tool || byTool[tool.id]) continue;
      if (!spec?.supportsCompression && !spec?.supportsCompressionLevel) continue;
      byTool[tool.id] = {
        mode: row.mode,
        format: formatOfPath(row.output_name) ?? targetLabel(row.mode),
      };
    }
    return Object.values(byTool);
  });
  const offersSplit = $derived(specs.some((s) => s.supportsSplit));
  const deletes = $derived(safety.deleteOriginal && specs.some((s) => s.supportsDeleteOnVerify));
  const optionsSummary = $derived(
    [
      safety.checkAfter || deletes ? 'Check each new file' : null,
      deletes ? 'Delete originals' : 'Keep originals',
    ].filter(Boolean).join(' · '),
  );

  // Where the new files go: next to each original, or one folder.
  let pickingFolder = $state(false);
  const outputDir = $derived(conversion.outputDir);
  const outputLabel = $derived.by(() => {
    if (!outputDir) return '';
    const volume = fileBrowser.volumes
      .filter((v) => v?.path && (outputDir === v.path || outputDir.startsWith(`${v.path.replace(/\/+$/, '')}/`)))
      .sort((a, b) => b.path.length - a.path.length)[0];
    if (!volume) return outputDir;
    const rest = outputDir.slice(volume.path.replace(/\/+$/, '').length).replace(/^\/+/, '');
    return rest ? `${volume.name} › ${rest.split('/').join(' › ')}` : volume.name;
  });

  function setWhere(next) {
    if (next === 'beside') conversion.outputDir = '';
    else pickingFolder = true;
  }

  // "Pick console" in a folder Library sets the folder's console; the plan
  // is made again once that dialog closes.
  let mapping = $state(false);
  function mapFolder(list) {
    const folders = [...new Set(list.map((p) => p.split('::')[0].replace(/\/[^/]*$/, '')))];
    if (folders.length !== 1 || fileBrowser.rommPlatformId !== null) return null;
    const [folder] = folders;
    return libraries.folderLibraryFor(folder) ? folder : null;
  }
  function openMapping(folder) {
    mapping = true;
    findings.openMapping(folder);
  }
  $effect(() => {
    if (mapping && !findings.mappingTarget) {
      untrack(() => {
        mapping = false;
        conversion.requestPlan(paths, { delay: 0 });
      });
    }
  });

  // A selection too big to plan live shows its plan in a dialog on Convert.
  let planPrompt = $state(null);
  function resolvePlanPrompt(proceed) {
    const resolve = planPrompt;
    planPrompt = null;
    ui.planPromptOpen = false;
    resolve?.(proceed);
  }

  // The delete preview, same resolver pattern.
  let deletePrompt = $state(null);
  function resolveDeletePlan(proceed) {
    const resolve = deletePrompt;
    deletePrompt = null;
    ui.deletePlanPromptOpen = false;
    resolve?.(proceed);
  }

  // Leaving the view aborts a submit still waiting on its RomM snapshot, and
  // any plan still on its way.
  let submitAbort = null;
  // A dialog still open is answered "no": its flag would otherwise stay set
  // and hold App's background refreshes after the panel is gone.
  onDestroy(() => {
    submitAbort?.abort();
    submitAbort = null;
    conversion.cancelPlan();
    if (planPrompt) resolvePlanPrompt(false);
    if (deletePrompt) resolveDeletePlan(false);
  });

  const converting = $derived(conversion.converting);
  const convertCount = $derived(big && !plan ? paths.length : actionable.length);
  // From the first click until the submit settles (the plan dialog and the
  // delete preview included): a second click never starts a second run.
  let inFlight = $state(false);
  const disabled = $derived(
    convertCount === 0 || converting || conversion.planning || noConverters || catalogLoading
      || inFlight,
  );

  async function convert() {
    if (disabled) return;
    inFlight = true;
    try {
      await run();
    } finally {
      inFlight = false;
    }
  }

  // Everything the run sends is read here, before the first await: what is
  // queued is what was planned, whatever changes while a dialog is open.
  async function run() {
    const snapshot = [...paths];
    const outputDir = conversion.outputDir || null;
    const platformId = fileBrowser.rommPlatformId;
    const fromRomm = platformId !== null;
    const choice = { ...safety, split: conversion.split };
    // The plan's own inputs, so the server replans what was shown.
    const planned = {
      target: conversion.target || null,
      platformOverrides: conversion.overridesFor(snapshot),
    };
    let confirmed = plan;
    if (!confirmed) {
      ui.planPromptOpen = true;
      const waiting = new Promise((resolve) => { planPrompt = resolve; });
      confirmed = await conversion.requestPlan(snapshot, { delay: 0 });
      if (!confirmed) {
        if (planPrompt) resolvePlanPrompt(false);
        if (conversion.planError) toast.error(`Couldn't plan this: ${conversion.planError}`);
        return;
      }
      if (!(await waiting)) return;
    }
    const rows = confirmed.filter((row) => row.mode);
    const options = {
      verifyAfter: choice.checkAfter || choice.deleteOriginal,
      deleteOnVerify: choice.deleteOriginal,
      containerOnly: choice.deleteOriginal && choice.quickCheck,
      split: choice.split,
      ...planned,
    };
    if (options.deleteOnVerify) {
      const groups = {};
      for (const row of rows) {
        if (!registry.specFor(row.mode)?.supportsDeleteOnVerify) continue;
        (groups[row.mode] ??= []).push(row.file_path);
      }
      if (Object.keys(groups).length) {
        try {
          // null: a newer ask took over, and its flow shows the plan.
          if (!(await conversion.fetchDeletePlan(groups, outputDir))) return;
        } catch (_e) {
          return; // toast already raised
        }
        ui.deletePlanPromptOpen = true;
        const proceed = await new Promise((resolve) => { deletePrompt = resolve; });
        conversion.clearDeletePlan();
        if (!proceed) return;
      }
    }
    const controller = new AbortController();
    submitAbort = controller;
    try {
      await conversion.submitAuto(snapshot, {
        plan: confirmed,
        rommRepin: fromRomm,
        rommPlatformId: platformId,
        outputDir,
        signal: controller.signal,
        options,
      });
    } catch (_e) {
      return; // toast already raised
    } finally {
      if (submitAbort === controller) submitAbort = null;
      // The "Re-match in RomM" badge follows what this submit recorded.
      if (fromRomm) romm.setPendingRepins(conversion.lastRepinPending);
    }
    if (controller.signal.aborted) return;
    fileBrowser.clearSelection();
    // Back from search results to the folder, so the new files show in place.
    if (fileBrowser.searchMode && ui.searchAutoReturnToFileList) {
      fileBrowser.exitSearch();
      fileBrowser.refresh({ force: true }).catch(() => {});
    }
  }
</script>

<section class="panel" aria-labelledby="convert-title">
  <header class="head">
    <h2 id="convert-title">Convert</h2>
    <span class="count">
      {count === 0 ? 'Nothing selected' : `${count} ${count === 1 ? 'game' : 'games'} selected`}
    </span>
  </header>

  {#if noConverters}
    <EmptyState
      title="No converters are installed here"
      description="Compressatorium can still browse, check and rename. See what's missing in Settings."
      glyph="◈"
    >
      {#snippet actions()}
        <Button variant="secondary" size="sm" onclick={() => ui.navigate('settings', { tab: 'converters' })}>
          Open Settings
        </Button>
      {/snippet}
    </EmptyState>
  {:else}
    <label class="field">
      <span class="label">Convert to</span>
      <select
        value={conversion.target}
        onchange={(e) => conversion.chooseTarget(e.currentTarget.value)}
      >
        {#each targetGroups as g (g.group)}
          <optgroup label={g.group}>
            {#each g.options as o (o.value)}
              <option value={o.value}>{o.hint ? `${o.label} · ${o.hint}` : o.label}</option>
            {/each}
          </optgroup>
        {/each}
      </select>
    </label>

    {#if count === 0}
      <p class="muted">Select games to convert.</p>
    {:else if paths.length === 0}
      <p class="muted">None of these can be converted here.</p>
    {:else if big && !plan}
      <p class="muted">The plan shows when you press Convert.</p>
    {:else}
      <ConvertPlan
        plan={shownPlan}
        planning={conversion.planning}
        error={conversion.planError}
        onretry={() => conversion.requestPlan(paths, { delay: 0 })}
        onoverride={(list, slug) => conversion.setOverride(list, slug)}
        {mapFolder}
        onmap={openMapping}
      />
    {/if}

    {#if paths.length > 0}
      <details class="options">
        <summary>Options <span class="muted">{optionsSummary}</span></summary>
        <div class="options-body">
          <SafetyOptions
            {specs}
            checkAfter={safety.checkAfter}
            deleteOriginal={safety.deleteOriginal}
            quickCheck={safety.quickCheck}
            onchange={(next) => conversion.setSafety(next)}
            idPrefix="convert-safety"
          />
          {#each pickers as p (p.mode)}
            <CompressionPicker
              mode={p.mode}
              label={`${p.format} compression`}
              value={conversion.compressionFor(p.mode)}
              onchange={(v) => conversion.setCompressionFor(p.mode, v)}
            />
          {/each}
          <fieldset class="where">
            <legend class="label">Where the new files go</legend>
            <label class="radio">
              <input
                type="radio"
                name="convert-where"
                checked={!outputDir}
                onchange={() => setWhere('beside')}
              />
              Next to the original
            </label>
            <label class="radio">
              <input
                type="radio"
                name="convert-where"
                checked={!!outputDir}
                onchange={() => setWhere('folder')}
              />
              In another folder…
            </label>
            {#if outputDir}
              <p class="chosen">
                <span class="mono">{outputLabel}</span>
                <Button variant="ghost" size="sm" onclick={() => (pickingFolder = true)}>Change</Button>
              </p>
            {/if}
          </fieldset>
          {#if offersSplit}
            <Checkbox
              bind:checked={conversion.split}
              label="Split into 4 GB parts"
              description="For FAT32 drives."
            />
          {/if}
        </div>
      </details>
    {/if}

    <div class="actions">
      <Button variant="primary" fullWidth {disabled} onclick={convert}>
        {#snippet icon()}
          {#if converting}<Loader size={14} class="spin" />{:else}<Play size={14} />{/if}
        {/snippet}
        {converting ? 'Starting…' : convertCount ? `Convert ${convertCount} ${convertCount === 1 ? 'game' : 'games'}` : 'Convert'}
      </Button>
      <p class="muted small">Progress shows in Activity.</p>
    </div>
  {/if}
</section>

<BaseModal
  open={!!planPrompt}
  onClose={() => resolvePlanPrompt(false)}
  title={`Convert ${paths.length} games`}
  size="md"
>
  {#snippet body()}
    <ConvertPlan
      plan={conversion.plan}
      planning={conversion.planning}
      error={conversion.planError}
      onretry={() => conversion.requestPlan(paths, { delay: 0 })}
    />
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={() => resolvePlanPrompt(false)}>Cancel</Button>
    <Button
      variant="primary"
      disabled={!conversion.plan?.some((row) => row.mode) || conversion.planning}
      onclick={() => resolvePlanPrompt(true)}
    >Convert</Button>
  {/snippet}
</BaseModal>

<DeletePlanModal open={!!deletePrompt} onResolve={resolveDeletePlan} />

<FolderPickerModal
  open={pickingFolder}
  title="Where should the new files go?"
  start={outputDir || fileBrowser.currentPath}
  onClose={() => (pickingFolder = false)}
  onpick={(path) => {
    conversion.outputDir = path;
    pickingFolder = false;
  }}
/>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }
  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .head h2 {
    margin: 0;
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .count, .muted {
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .muted { margin: 0; }
  .small { font-size: var(--text-xs); }
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }
  .label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
    padding: 0;
  }
  .field select {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
    cursor: pointer;
    min-width: 0;
    max-width: 100%;
  }
  .field select:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .options summary {
    cursor: pointer;
    color: var(--text-1);
    font-size: var(--text-sm);
    font-weight: var(--weight-medium);
  }
  .options summary .muted { font-weight: normal; margin-left: var(--space-2); }
  .options-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    margin-top: var(--space-3);
  }
  .where {
    border: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .radio {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-1);
    cursor: pointer;
  }
  .chosen {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    flex-wrap: wrap;
    font-size: var(--text-sm);
  }
  .mono {
    font-family: var(--font-mono);
    color: var(--text-2);
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  :global(.panel .spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
