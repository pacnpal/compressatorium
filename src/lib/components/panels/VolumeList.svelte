<script>
  import { onMount } from 'svelte';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import HardDrive from '@lucide/svelte/icons/hard-drive';
  import Loader from '@lucide/svelte/icons/loader-circle';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  const volumes = $derived(fileBrowser.volumes);
  const selected = $derived(fileBrowser.selectedVolume);
  const loading = $derived(fileBrowser.volumesLoading);
  const error = $derived(fileBrowser.volumesError);

  onMount(() => {
    if (volumes.length === 0 && !loading) fileBrowser.loadVolumes();
  });
</script>

<section class="volumes" aria-label="Volumes">
  <h3 class="header">Volumes</h3>
  {#if loading && volumes.length === 0}
    <div class="state"><Loader class="spin" size={14} /> Loading…</div>
  {:else if error}
    <div class="state error"><TriangleAlert size={14} /> {error}</div>
  {:else if volumes.length === 0}
    <div class="state muted">No volumes configured</div>
  {:else}
    <ul class="list">
      {#each volumes as v (v.path)}
        <li>
          <button
            type="button"
            class="item"
            class:active={selected?.path === v.path}
            onclick={() => fileBrowser.selectVolume(v)}
            aria-current={selected?.path === v.path ? 'true' : undefined}
            title={v.path}
          >
            <HardDrive size={14} class="icon" />
            <span class="name">{v.name}</span>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .volumes {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  .header {
    margin: 0 var(--space-2);
    font-size: var(--text-xs);
    font-weight: var(--weight-semibold);
    color: var(--text-3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 2px; }
  .item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    padding: var(--space-2);
    border-radius: var(--radius-md);
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-2);
    text-align: left;
    cursor: pointer;
    font-size: var(--text-sm);
    transition: background var(--dur-fast) var(--ease-out), color var(--dur-fast) var(--ease-out);
  }
  .item:hover { background: var(--surface-2); color: var(--text-1); }
  .active {
    background: var(--accent-muted);
    color: var(--accent);
    font-weight: var(--weight-medium);
  }
  .item :global(.icon) { flex-shrink: 0; }
  .name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
    color: var(--text-2);
  }
  .muted { color: var(--text-3); font-style: italic; }
  .error { color: var(--error); }
  .state :global(.spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
