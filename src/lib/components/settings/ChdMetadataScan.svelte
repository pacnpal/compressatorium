<script>
  /**
   * Settings → Converters: the CHD badge scan. The file list only reads the
   * cache that tells a CHD's CD from its DVD (POST /api/chd-metadata never runs
   * chdman), so a CHD added outside the app has no badge until this scan reads
   * it. Force reads every CHD again. Progress shows while it runs.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { chdMetadata } from '$lib/stores/chdMetadata.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
  import DatabaseZap from '@lucide/svelte/icons/database-zap';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';

  let pollTimer = null;

  onMount(() => {
    chdMetadata.pollStatus();
    return () => {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
    };
  });

  // Poll while a scan runs; stop when it ends.
  $effect(() => {
    if (chdMetadata.scanRunning && !pollTimer) {
      pollTimer = setInterval(() => chdMetadata.pollStatus(), 2000);
    } else if (!chdMetadata.scanRunning && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  });

  const status = $derived(chdMetadata.scanStatus);
  const running = $derived(chdMetadata.scanRunning);
  const total = $derived(status?.total ?? 0);
  const processed = $derived(status?.processed ?? 0);
  const percent = $derived(total > 0 ? Math.round((processed / total) * 100) : null);

  async function scan(force) {
    try {
      await chdMetadata.startScan({ force });
      toast.info(force ? 'Reading every CHD again' : 'Reading new CHDs');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't start the scan");
    }
  }
</script>

<div class="chd-scan">
  <h3>CD and DVD badges</h3>
  {#if running}
    <div class="row">
      <span class="what">Reading CHDs…</span>
      {#if total > 0}<span class="count">{processed} / {total}</span>{/if}
    </div>
    <ProgressBar value={percent} size="sm" label="CHD scan progress" />
    {#if status?.current_file}
      <div class="current" title={status.current_file}>{status.current_file}</div>
    {/if}
  {:else}
    <p class="hint">
      Shows which CHDs hold a CD and which a DVD. Run it after adding CHDs from
      outside Compressatorium; Force rescan reads every one again.
    </p>
  {/if}
  <div class="actions">
    <Button variant="secondary" size="sm" disabled={running} onclick={() => scan(false)}>
      {#snippet icon()}<DatabaseZap size={14} />{/snippet}
      Scan CHDs
    </Button>
    <Button variant="ghost" size="sm" disabled={running} onclick={() => scan(true)}>
      {#snippet icon()}<RefreshCw size={14} />{/snippet}
      Force rescan
    </Button>
  </div>
</div>

<style>
  .chd-scan {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-top: var(--space-4);
    padding-top: var(--space-4);
    border-top: 1px solid var(--border-subtle);
  }
  h3 { margin: 0; font-size: var(--text-base); font-weight: var(--weight-semibold); color: var(--text-1); }
  .hint { margin: 0; color: var(--text-2); font-size: var(--text-sm); }
  .row { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-2); }
  .what { color: var(--text-1); font-size: var(--text-sm); font-weight: var(--weight-medium); }
  .count { color: var(--text-2); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
  .current {
    color: var(--text-2); font-size: var(--text-xs); font-family: var(--font-mono);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
</style>
