<script>
  /**
   * Activity › Running: the queue, and nothing else. What is running comes
   * first, then what is waiting; a job leaves on its own when it ends, and
   * History keeps every finished one (check runs included).
   */
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import JobRow from '$lib/components/panels/JobRow.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Pager from '$lib/components/ui/Pager.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import CircleX from '@lucide/svelte/icons/circle-x';

  const PAGE_SIZE = 50;
  let page = $state(1);

  const list = $derived(jobs.runningList);
  const running = $derived(list.filter((j) => j.status === 'processing').length);
  const pageCount = $derived(Math.max(1, Math.ceil(list.length / PAGE_SIZE)));
  // A page that emptied as jobs ended steps back to the last one left.
  const shown = $derived(Math.min(page, pageCount));
  const pageJobs = $derived(list.slice((shown - 1) * PAGE_SIZE, shown * PAGE_SIZE));
</script>

<section class="running" aria-label="Running">
  {#if list.length === 0}
    <EmptyState glyph="◌" title="Nothing running" description="Pick games in Files and press Convert.">
      {#snippet actions()}
        <Button variant="secondary" onclick={() => ui.navigate('files')}>Open Files</Button>
      {/snippet}
    </EmptyState>
  {:else}
    <div class="controls">
      <span class="count">
        {running} running · {list.length - running} waiting
      </span>
      <Button
        variant="secondary" size="sm" icon={cancelIcon}
        loading={jobs.cancellingAll}
        onclick={() => (ui.showCancelAll = true)}
      >
        Cancel all
      </Button>
    </div>
    <ul class="rows">
      {#each pageJobs as job (job.id)}
        <JobRow {job} />
      {/each}
    </ul>
    {#if pageCount > 1}
      <div class="footer">
        <Pager page={shown} {pageCount} onpage={(p) => (page = p)} />
      </div>
    {/if}
  {/if}
</section>

{#snippet cancelIcon()}<CircleX size={14} />{/snippet}

<style>
  .running {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .count { color: var(--text-2); font-size: var(--text-sm); }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .footer { display: flex; justify-content: flex-end; }
</style>
