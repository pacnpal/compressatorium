<script>
  import { ledger } from '$lib/stores/ledger.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import StatCard from './StatCard.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Clock from '@lucide/svelte/icons/clock';

  // Read from the Ledger, so the list survives a restart. The Dashboard
  // refreshes the store whenever another job finishes.
  const recent = $derived(ledger.recent);

  function shortFile(row) {
    const p = row?.source_path ?? '';
    return p.split(/[/\\]/).pop() ?? p;
  }

  function tone(s) {
    if (s === 'completed') return 'success';
    if (s === 'failed') return 'error';
    return 'neutral';
  }
</script>

<StatCard title="Recent conversions" subtitle="Last 5" accent="var(--success)">
  {#snippet icon()}<Clock size={16} />{/snippet}
  {#snippet body()}
    {#if recent.length === 0}
      <EmptyState title="No recent activity" description="Completed jobs will appear here." glyph="∅" />
    {:else}
      <ul class="recent-list">
        {#each recent as row (row.id)}
          {@const spec = registry.specFor(row.mode)}
          <li class="recent-row" title={row.source_path}>
            <span class="recent-name">{shortFile(row)}</span>
            <span class="recent-mode">{spec?.label ?? row.mode}</span>
            <Badge tone={tone(row.status)} size="sm">{row.status}</Badge>
          </li>
        {/each}
      </ul>
    {/if}
  {/snippet}
</StatCard>

<style>
  .recent-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .recent-row { display: grid; grid-template-columns: 1fr auto auto; gap: var(--space-2); align-items: center; padding: 4px 0; border-bottom: 1px solid var(--border-subtle); font-size: var(--text-sm); }
  .recent-row:last-child { border-bottom: none; }
  .recent-name { color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .recent-mode { color: var(--text-3); font-size: var(--text-xs); }
</style>
