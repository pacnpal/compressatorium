<script>
  // Re-introduces the legacy "Scan Metadata" / "Force Rescan" controls.
  // The scan walks every CHD on disk, populates media_type / sha1 fields
  // in the backend cache, and FileList row badges (CD / DVD / disc icon)
  // depend on it. The chdMetadata store already exposed startScan and
  // pollStatus; nothing surfaced them after the rebuild.

  import { onMount } from 'svelte';
  import { chdMetadata } from '$lib/stores/chdMetadata.svelte.js';
  import { toast } from 'svelte-sonner';
  import StatCard from './StatCard.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
  import DatabaseZap from '@lucide/svelte/icons/database-zap';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';

  let pollTimer = null;

  onMount(() => {
    chdMetadata.pollStatus();
    return () => {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    };
  });

  // Poll while a scan is in flight; clear when it finishes. Same shape
  // as the DAT sync polling effect in DATView.
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

  async function handleScan(force) {
    try {
      await chdMetadata.startScan({ force });
      toast.info(force ? 'Force rescan started' : 'Metadata scan started');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to start scan');
    }
  }
</script>

<StatCard title="CHD metadata" subtitle="Media-type cache" accent="var(--badge-dvd)">
  {#snippet icon()}<DatabaseZap size={16} />{/snippet}
  {#snippet body()}
    {#if running}
      <div class="ms-row">
        <span class="ms-label">Scanning…</span>
        {#if total > 0}
          <span class="ms-counter">{processed} / {total}</span>
        {/if}
      </div>
      <ProgressBar value={percent} size="sm" />
      {#if status?.current_file}
        <div class="ms-current" title={status.current_file}>{status.current_file}</div>
      {/if}
    {:else}
      <p class="ms-blurb">
        Builds the cache that powers CD / DVD badges on file rows. Run after
        adding CHDs from outside the UI; Force re-reads every entry.
      </p>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="ms-actions">
      <Button variant="secondary" size="sm" disabled={running} onclick={() => handleScan(false)}>
        {#snippet icon()}<DatabaseZap size={14} />{/snippet}
        Scan
      </Button>
      <Button variant="ghost" size="sm" disabled={running} onclick={() => handleScan(true)}>
        {#snippet icon()}<RefreshCw size={14} />{/snippet}
        Force rescan
      </Button>
    </div>
  {/snippet}
</StatCard>

<style>
  .ms-row { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-2); }
  .ms-label { color: var(--text-1); font-size: var(--text-sm); font-weight: var(--weight-medium); }
  .ms-counter { color: var(--text-3); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
  .ms-current {
    color: var(--text-3); font-size: var(--text-xs); font-family: var(--font-mono);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: var(--space-1);
  }
  .ms-blurb { margin: 0; color: var(--text-2); font-size: var(--text-sm); }
  .ms-actions { display: inline-flex; gap: var(--space-2); flex-wrap: wrap; }
</style>
