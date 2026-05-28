<script>
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import House from '@lucide/svelte/icons/house';
  import ChevronRight from '@lucide/svelte/icons/chevron-right';
  import Archive from '@lucide/svelte/icons/archive';

  const volume = $derived(fileBrowser.selectedVolume);
  const segments = $derived(fileBrowser.breadcrumbSegments);
  const archivePath = $derived(fileBrowser.currentArchivePath);

  // Trim segments that duplicate the volume root, so we don't render
  // /games/games/My Folder when the volume mount is /games.
  const trimmed = $derived.by(() => {
    if (!volume) return segments;
    const root = volume.path.replace(/\/$/, '');
    return segments.filter((s) => s.path !== root);
  });

  function archiveName(p) {
    if (!p) return '';
    return p.split('/').pop() ?? p;
  }
</script>

<nav class="crumbs" aria-label="Breadcrumb">
  {#if volume}
    <button
      type="button"
      class="crumb root"
      onclick={() => fileBrowser.navigate(volume.path)}
      title={volume.path}
    >
      <House size={14} />
      <span>{volume.name}</span>
    </button>
    {#each trimmed as seg (seg.path)}
      <ChevronRight size={12} class="sep" aria-hidden="true" />
      <button
        type="button"
        class="crumb"
        onclick={() => fileBrowser.navigate(seg.path)}
        title={seg.path}
      >
        {seg.name}
      </button>
    {/each}
    {#if archivePath}
      <ChevronRight size={12} class="sep" aria-hidden="true" />
      <span class="crumb current">
        <Archive size={14} />
        {archiveName(archivePath)}
      </span>
    {/if}
  {/if}
</nav>

<style>
  .crumbs {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-1);
    min-height: 32px;
    padding: var(--space-1) 0;
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .crumbs :global(.sep) {
    color: var(--text-3);
    flex-shrink: 0;
  }
  .crumb {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 2px 8px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--text-2);
    cursor: pointer;
    font-size: inherit;
  }
  button.crumb { background: none; }
  button.crumb:hover {
    background: var(--surface-2);
    color: var(--text-1);
  }
  .root { font-weight: var(--weight-medium); }
  .current {
    color: var(--text-1);
    font-weight: var(--weight-medium);
    cursor: default;
  }
</style>
