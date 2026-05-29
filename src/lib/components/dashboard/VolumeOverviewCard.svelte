<script>
  import { onMount } from 'svelte';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import StatCard from './StatCard.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Boxes from '@lucide/svelte/icons/boxes';

  const volumes = $derived(fileBrowser.volumes);
  const loading = $derived(fileBrowser.volumesLoading);

  onMount(() => {
    if (volumes.length === 0) fileBrowser.loadVolumes();
  });
</script>

<StatCard title="Volumes" subtitle="Mounted storage" accent="var(--badge-cd)">
  {#snippet icon()}<Boxes size={16} />{/snippet}
  {#snippet body()}
    {#if loading && volumes.length === 0}
      <p class="muted">Loading…</p>
    {:else if volumes.length === 0}
      <EmptyState title="No volumes" description="Configure COMPRESSATORIUM_VOLUMES to expose paths." glyph="∅" />
    {:else}
      <ul class="vol-list">
        {#each volumes.slice(0, 4) as v (v.path)}
          <li class="vol-row" title={v.path}>
            <span class="vol-name">{v.name ?? v.path}</span>
            {#if typeof v.file_count === 'number'}
              <span class="vol-meta">{v.file_count} files</span>
            {/if}
          </li>
        {/each}
        {#if volumes.length > 4}
          <li class="vol-more">…and {volumes.length - 4} more</li>
        {/if}
      </ul>
    {/if}
  {/snippet}
  {#snippet footer()}
    <Button variant="ghost" size="sm" onclick={() => ui.navigate('workspace', ui.workspaceTool)}>
      Browse →
    </Button>
  {/snippet}
</StatCard>

<style>
  .muted { color: var(--text-3); font-size: var(--text-sm); margin: 0; }
  .vol-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .vol-row { display: flex; justify-content: space-between; gap: var(--space-2); padding: 4px 0; border-bottom: 1px solid var(--border-subtle); font-size: var(--text-sm); }
  .vol-row:last-child { border-bottom: none; }
  .vol-name { color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .vol-meta { color: var(--text-3); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
  .vol-more { color: var(--text-3); font-size: var(--text-xs); font-style: italic; padding: 4px 0; }
</style>
