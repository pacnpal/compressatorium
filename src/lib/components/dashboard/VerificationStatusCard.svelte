<script>
  import { verification } from '$lib/stores/verification.svelte.js';
  import StatCard from './StatCard.svelte';
  import ShieldCheck from '@lucide/svelte/icons/shield-check';

  const verifiedCount = $derived(verification.statuses.size);
  const batch = $derived(verification.batchRun);
</script>

<StatCard title="Verification" subtitle="Cached OK results" accent="var(--badge-verified)">
  {#snippet icon()}<ShieldCheck size={16} />{/snippet}
  {#snippet body()}
    <div class="big">
      <span class="num">{verifiedCount}</span>
      <span class="lbl">verified</span>
    </div>
    {#if batch}
      <div class="batch">
        Batch in progress: {batch.done}/{batch.total}
      </div>
    {/if}
  {/snippet}
</StatCard>

<style>
  .big { display: flex; align-items: baseline; gap: var(--space-2); }
  .num { font-size: var(--text-2xl); font-weight: var(--weight-bold); color: var(--text-1); font-variant-numeric: tabular-nums; }
  .lbl { color: var(--text-3); font-size: var(--text-sm); }
  .batch { color: var(--text-2); font-size: var(--text-xs); margin-top: var(--space-2); }
</style>
