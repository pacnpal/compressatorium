<script>
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import StatCard from './StatCard.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { ledger } from '$lib/stores/ledger.svelte.js';
  import ListTodo from '@lucide/svelte/icons/list-todo';

  // Done and Failed are all-time totals from the Ledger (#533); the job list
  // keeps only the newest finished jobs. Until the Ledger answers, the list.
  const totals = $derived(ledger.totals ?? {});
  const failed = $derived(
    totals.failed != null
      ? totals.failed + (totals.cancelled ?? 0)
      : jobs.failedCount + jobs.cancelledCount,
  );
</script>

<StatCard title="Job queue" subtitle="Live state" accent="var(--badge-converting)">
  {#snippet icon()}<ListTodo size={16} />{/snippet}
  {#snippet body()}
    <div class="queue-grid">
      <div class="cell">
        <span class="num">{jobs.queuedCount}</span>
        <span class="lbl">Queued</span>
      </div>
      <div class="cell">
        <span class="num">{jobs.processingCount}</span>
        <span class="lbl">Active</span>
      </div>
      <div class="cell">
        <span class="num">{totals.completed ?? jobs.completedCount}</span>
        <span class="lbl">Done</span>
      </div>
      <div class="cell">
        <span class="num">{failed}</span>
        <span class="lbl">Failed</span>
      </div>
    </div>
  {/snippet}
  {#snippet footer()}
    <Button variant="ghost" size="sm" onclick={() => ui.navigate('files')}>
      Go to Files →
    </Button>
  {/snippet}
</StatCard>

<style>
  .queue-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--space-2); }
  .cell { display: flex; flex-direction: column; gap: 2px; }
  .num { font-size: var(--text-xl); font-weight: var(--weight-bold); color: var(--text-1); font-variant-numeric: tabular-nums; }
  .lbl { color: var(--text-3); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em; }
</style>
