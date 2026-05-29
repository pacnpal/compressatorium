<script>
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import StatCard from './StatCard.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Clock from '@lucide/svelte/icons/clock';

  // Most-recent five terminal-state jobs (any kind). Sorted by
  // `completed_at` when present, otherwise by id; the backend assigns
  // ids monotonically so they're a stable order proxy.
  const recent = $derived.by(() => {
    const terminal = jobs.jobs.filter(
      (j) => j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled',
    );
    terminal.sort((a, b) => {
      const ta = Date.parse(a.completed_at ?? '') || 0;
      const tb = Date.parse(b.completed_at ?? '') || 0;
      if (ta !== tb) return tb - ta;
      // Numeric fallback when both ids parse as numbers so "10" sorts
      // after "2"; lexicographic for non-numeric ids (UUIDs, etc.).
      const na = Number(a.id);
      const nb = Number(b.id);
      if (Number.isFinite(na) && Number.isFinite(nb)) return nb - na;
      return String(b.id).localeCompare(String(a.id));
    });
    return terminal.slice(0, 5);
  });

  function shortFile(j) {
    const p = j?.file_path ?? '';
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
        {#each recent as j (j.id)}
          {@const spec = registry.specFor(j.mode)}
          <li class="recent-row" title={j.file_path}>
            <span class="recent-name">{shortFile(j)}</span>
            <span class="recent-mode">{spec?.label ?? j.mode}</span>
            <Badge tone={tone(j.status)} size="sm">{j.status}</Badge>
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
