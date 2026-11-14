<script>
  import { ledger } from '$lib/stores/ledger.svelte.js';
  import { formatSize } from '$lib/api/format.js';
  import StatCard from './StatCard.svelte';
  import HardDrive from '@lucide/svelte/icons/hard-drive';

  // Space reclaimed: completed Ledger rows whose Source was deleted.
  const savings = $derived(ledger.savings);

  const split = (obj) =>
    Object.entries(obj ?? {})
      .filter(([, v]) => v !== 0)
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
      .slice(0, 4);
</script>

<StatCard title="Space saved" subtitle="From the Ledger" accent="var(--success)">
  {#snippet icon()}<HardDrive size={16} />{/snippet}
  {#snippet body()}
    <div class="big">
      <span class="num">{formatSize(savings?.saved_bytes ?? 0)}</span>
      <span class="lbl">{savings?.conversions ?? 0} sources replaced</span>
    </div>
    {#each [['By platform', savings?.by_platform], ['By format', savings?.by_format]] as [label, obj] (label)}
      {@const rows = split(obj)}
      {#if rows.length}
        <div class="split">
          <span class="split-label">{label}</span>
          {#each rows as [key, bytes] (key)}
            <span class="chip">{key} <b>{formatSize(bytes)}</b></span>
          {/each}
        </div>
      {/if}
    {/each}
  {/snippet}
</StatCard>

<style>
  .big { display: flex; align-items: baseline; gap: var(--space-2); }
  .num { font-size: var(--text-2xl); font-weight: var(--weight-bold); color: var(--text-1); font-variant-numeric: tabular-nums; }
  .lbl { color: var(--text-3); font-size: var(--text-sm); }
  .split { display: flex; flex-wrap: wrap; gap: var(--space-1); align-items: center; margin-top: var(--space-2); font-size: var(--text-xs); }
  .split-label { color: var(--text-3); margin-right: var(--space-1); }
  .chip { color: var(--text-2); }
  .chip b { color: var(--text-1); font-weight: var(--weight-medium, 500); }
</style>
