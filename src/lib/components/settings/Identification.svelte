<script>
  /**
   * Settings → Game identification: the game lists (DATs) that name games and
   * tell good dumps from bad, offline. Import your own, update MAME Redump,
   * remove one, and set which list names a game two of them know ("Name
   * order", #424). The online lookup is in Settings → Connections.
   */
  import { onMount } from 'svelte';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { toast } from 'svelte-sonner';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import Upload from '@lucide/svelte/icons/upload';
  import Download from '@lucide/svelte/icons/download';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import XIcon from '@lucide/svelte/icons/x';
  import ArrowUp from '@lucide/svelte/icons/arrow-up';
  import ArrowDown from '@lucide/svelte/icons/arrow-down';
  import RotateCcw from '@lucide/svelte/icons/rotate-ccw';

  // In ranked order (GET /api/dat/ranking): when two DATs match the same
  // file, the higher one names it.
  const dats = $derived(datMatching.dats);
  const rankingCustom = $derived(datMatching.datsRankingCustom);
  let savingOrder = $state(false);

  async function saveOrder(order) {
    savingOrder = true;
    try {
      await datMatching.setDATRanking(order);
      toast.success(order === null ? 'Name order back to the default' : 'Name order saved');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't save the name order");
    } finally {
      savingOrder = false;
    }
  }

  function moveDat(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= dats.length) return;
    const order = dats.map((d) => d.id);
    [order[index], order[target]] = [order[target], order[index]];
    saveOrder(order);
  }
  const loading = $derived(datMatching.datsLoading);
  const error = $derived(datMatching.datsError);
  const importing = $derived(datMatching.importingDat);
  const syncing = $derived(datMatching.syncing);
  const syncStatus = $derived(datMatching.syncStatus);
  const redumpSystems = $derived(datMatching.redump?.systems ?? []);
  const redumpSyncing = $derived(datMatching.redumpSyncing);

  let fileInputEl;
  let syncPollTimer = null;

  onMount(() => {
    datMatching.loadDATs();
    datMatching.loadRedump();
    // Poll sync status whenever syncing flips on; an $effect handles
    // restart, this onMount just primes the initial status.
    datMatching.pollSyncStatus();
    return () => {
      if (syncPollTimer) {
        clearInterval(syncPollTimer);
        syncPollTimer = null;
      }
    };
  });

  $effect(() => {
    if (syncing && !syncPollTimer) {
      syncPollTimer = setInterval(() => {
        datMatching.pollSyncStatus();
      }, 2000);
    } else if (!syncing && syncPollTimer) {
      clearInterval(syncPollTimer);
      syncPollTimer = null;
    }
  });

  // The files (or zip members) the server refused, three named in one toast.
  function reportRefused(errors) {
    if (!errors?.length) return;
    const shown = errors.slice(0, 3).map((e) => `${e.file}: ${e.error}`).join('\n');
    const more = errors.length > 3 ? `\n…and ${errors.length - 3} more` : '';
    toast.error(`${errors.length} not imported`, { description: shown + more });
  }

  async function handleImport(event) {
    const files = Array.from(event.currentTarget?.files ?? []);
    if (!files.length) return;
    try {
      // .dat and .xml files, or zips of them (No-Intro's daily pack), in one request.
      const result = await datMatching.importDATs(files);
      if (result?.imported?.length) toast.success(result.message);
      reportRefused(result?.errors);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't import the game lists");
    } finally {
      // Reset so the same file can be picked again.
      if (fileInputEl) fileInputEl.value = '';
    }
  }

  async function handleRedumpSync() {
    try {
      const result = await datMatching.syncRedump();
      if (!result) return;
      const failed = (result.systems ?? []).filter((s) => s.status === 'failed');
      if (failed.length) reportRefused(failed.map((s) => ({ file: s.system, error: s.error })));
      else toast.success(result.message ?? 'Redump lists up to date');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't get the Redump lists");
    }
  }

  async function handleSync() {
    try {
      await datMatching.syncMAMERedump();
      toast.info('Updating MAME Redump');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't start the update");
    }
  }

  async function handleCancelSync() {
    try {
      await datMatching.cancelSync();
      toast.info('Stopping the update');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't stop the update");
    }
  }

  async function handleDeleteDat(dat) {
    if (!dat?.id) return;
    const name = dat.name ?? dat.id;
    if (!window.confirm(`Remove the game list "${name}"? Games only it knew lose their name.`)) return;
    try {
      await datMatching.deleteDAT(dat.id);
      toast.success(`Removed ${name}`);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't remove the game list");
    }
  }

  function importedOn(iso) {
    const when = iso ? new Date(iso) : null;
    return when && !Number.isNaN(when.getTime()) ? when.toLocaleDateString() : '';
  }

  // Sync status fields come from `_progress` on the backend (see
  // app/services/dat_sync.py): `{ status, files_total, files_imported,
  // current_file, file_index, error }`. Percent is files_imported /
  // files_total, there is no top-level numeric progress field.
  const syncProgress = $derived(syncStatus?.progress ?? null);
  const syncPercent = $derived.by(() => {
    const total = syncProgress?.files_total ?? 0;
    const done = syncProgress?.files_imported ?? 0;
    if (total <= 0) return null;
    return Math.round((done / total) * 100);
  });
  const syncPhase = $derived(syncProgress?.status ?? 'starting');
  const syncCurrentFile = $derived(syncProgress?.current_file ?? '');
</script>

<p class="hint">Game lists (DATs) name your games and tell good dumps from bad, offline.</p>

<div class="actions">
  <!-- `hidden`, not visually hidden: the button below is the one control. -->
  <input
    type="file"
    accept=".dat,.xml,.zip"
    multiple
    hidden
    bind:this={fileInputEl}
    onchange={handleImport}
  />
  <Button
    variant="secondary"
    disabled={importing}
    loading={importing}
    onclick={() => fileInputEl?.click()}
  >
    {#snippet icon()}<Upload size={14} />{/snippet}
    Import game lists…
  </Button>
  <Button
    variant="secondary"
    disabled={redumpSyncing || redumpSystems.length === 0}
    loading={redumpSyncing}
    title={redumpSystems.length
      ? `Downloads Redump's lists for ${redumpSystems.map((s) => s.name).join(', ')}`
      : 'None of your Libraries has a disc console Redump covers'}
    onclick={handleRedumpSync}
  >
    {#snippet icon()}<Download size={14} />{/snippet}
    Get Redump lists for my consoles
  </Button>
  {#if syncing}
    <Button variant="destructive" onclick={handleCancelSync}>
      {#snippet icon()}<XIcon size={14} />{/snippet}
      Stop updating
    </Button>
  {:else}
    <Button variant="secondary" onclick={handleSync}>
      {#snippet icon()}<Download size={14} />{/snippet}
      Update MAME Redump
    </Button>
  {/if}
</div>

{#if syncing}
  <div class="sync-status" role="status" aria-live="polite">
    <div class="sync-header">
      <strong>Updating MAME Redump…</strong>
      <Badge tone="info">{syncPhase}</Badge>
      {#if syncProgress?.files_total}
        <span class="sync-counter">
          {syncProgress.files_imported ?? 0} / {syncProgress.files_total}
        </span>
      {/if}
    </div>
    <ProgressBar value={syncPercent} size="sm" />
    {#if syncCurrentFile}
      <div class="sync-msg">Importing {syncCurrentFile}</div>
    {:else if syncProgress?.error}
      <div class="sync-msg">{syncProgress.error}</div>
    {/if}
  </div>
{/if}

{#if loading}
  <div class="state"><Spinner size="sm" /> Loading…</div>
{:else if error}
  <div class="error" role="alert">
    Couldn't load this. {error}
    <Button size="sm" variant="secondary" onclick={() => datMatching.loadDATs()}>Try again</Button>
  </div>
{:else if dats.length === 0}
  <EmptyState
    title="No game lists yet"
    description="Get the Redump lists for your disc consoles, or import game lists: several .dat files at once, or a whole zip such as No-Intro's daily pack."
    glyph="≣"
  />
{:else}
  <ul class="dat-list">
    {#each dats as dat (dat.id ?? dat.name)}
      <li class="dat-row">
        <div class="dat-meta">
          <div class="dat-name">
            {dat.name ?? '(unnamed)'}
            {#if dat.family}
              <Badge tone={dat.family === 'other' ? 'neutral' : 'dat-match'}>{dat.family}</Badge>
            {/if}
          </div>
          <div class="dat-sub">
            {#if dat.file_count != null}<span>{dat.file_count} games</span>{/if}
            {#if importedOn(dat.imported_at)}<span>added {importedOn(dat.imported_at)}</span>{/if}
            {#if dat.version}<span>v{dat.version}</span>{/if}
          </div>
        </div>
        <IconButton label="Remove {dat.name ?? 'game list'}" size="sm" onclick={() => handleDeleteDat(dat)}>
          <Trash2 size={14} />
        </IconButton>
      </li>
    {/each}
  </ul>

  {#if dats.length > 1}
    <details class="dat-ranking">
      <summary>Name order</summary>
      <p class="ranking-hint">
        When more than one list knows a game, the higher one names it.
        {#if !rankingCustom}
          Until you reorder them, Redump comes first for discs and No-Intro first
          for cartridges.
        {/if}
      </p>
      <ol class="rank-list">
        {#each dats as dat, index (dat.id ?? dat.name)}
          <li class="rank-row">
            <span class="dat-rank">{index + 1}</span>
            <span class="rank-name">{dat.name ?? '(unnamed)'}</span>
            <IconButton
              label="Move {dat.name ?? 'list'} up"
              size="sm"
              disabled={savingOrder || index === 0}
              onclick={() => moveDat(index, -1)}
            >
              <ArrowUp size={14} />
            </IconButton>
            <IconButton
              label="Move {dat.name ?? 'list'} down"
              size="sm"
              disabled={savingOrder || index === dats.length - 1}
              onclick={() => moveDat(index, 1)}
            >
              <ArrowDown size={14} />
            </IconButton>
          </li>
        {/each}
      </ol>
      {#if rankingCustom}
        <div class="reset">
          <Button variant="secondary" size="sm" disabled={savingOrder} onclick={() => saveOrder(null)}>
            {#snippet icon()}<RotateCcw size={14} />{/snippet}
            Use the default order
          </Button>
        </div>
      {/if}
    </details>
  {/if}
{/if}

<style>
  .hint { margin: 0 0 var(--space-3); color: var(--text-2); }
  .actions { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-bottom: var(--space-3); }

  .sync-status { display: flex; flex-direction: column; gap: var(--space-2); margin-bottom: var(--space-3); }
  .sync-header { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .sync-counter { color: var(--text-3); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
  .sync-msg { color: var(--text-3); font-size: var(--text-xs); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .dat-list, .rank-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .dat-row, .rank-row { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2); min-width: 0; }
  .dat-meta { flex: 1; min-width: 0; }
  .dat-name { color: var(--text-1); font-size: var(--text-sm); font-weight: var(--weight-medium); display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; overflow-wrap: anywhere; }
  .dat-sub { color: var(--text-2); font-size: var(--text-xs); display: flex; gap: var(--space-2); flex-wrap: wrap; }

  .dat-ranking { margin-top: var(--space-3); }
  .dat-ranking summary { cursor: pointer; color: var(--text-2); font-size: var(--text-sm); }
  .dat-ranking[open] summary { margin-bottom: var(--space-2); }
  .reset { margin-top: var(--space-2); }
  .ranking-hint { margin: 0 0 var(--space-2); color: var(--text-2); font-size: var(--text-sm); line-height: 1.5; }
  .dat-rank { flex: none; min-width: 1.75em; text-align: right; color: var(--text-3); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
  .rank-name { flex: 1; min-width: 0; color: var(--text-1); font-size: var(--text-sm); overflow-wrap: anywhere; }

  .state { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); font-size: var(--text-sm); }
  .error { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); color: var(--error); background: var(--error-muted); padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); font-size: var(--text-sm); }
</style>
