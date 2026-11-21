<script>
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import JobRow from './JobRow.svelte';
  import Pager from '$lib/components/ui/Pager.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import ListTodo from '@lucide/svelte/icons/list-todo';
  import CircleX from '@lucide/svelte/icons/circle-x';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  // Each $derived pulls from the rune-tracked store fields, so they
  // re-evaluate whenever jobs.jobs / .tab / .page mutate.
  //
  // Tab counts + Cancel/Clear gating use the *visible* counts (which
  // respect `Show metadata jobs` and locally-hidden ids) so the badge
  // never reads non-zero while the list is empty, and destructive
  // actions never operate on jobs the user chose to hide.
  const tab = $derived(jobs.tab);
  const pageJobs = $derived(jobs.pageJobs);
  const page = $derived(jobs.page);
  const pageCount = $derived(jobs.pageCount);
  const queuedCount = $derived(jobs.visibleQueuedCount);
  const completedCount = $derived(jobs.visibleCompletedCount);
  const failedCount = $derived(jobs.visibleFailedCount);

  function handleCancelAll() {
    // Delegate to the modal, it owns the confirmation dance + the
    // toast on success. JobsPanel just opens the dialog.
    ui.showCancelAll = true;
  }

  function handleClearCompleted() {
    ui.showClearDone = true;
  }
</script>

<section class="panel" aria-label="Job queue">
  <div class="title-row">
    <h2 class="panel-title">
      <ListTodo size={14} aria-hidden="true" /> Jobs
    </h2>
    <Checkbox
      bind:checked={() => jobs.showExternalScanJobs, (v) => jobs.setShowExternalScanJobs(v)}
      label="Show metadata jobs"
    />
  </div>

  <div class="tabs" role="tablist">
    <button
      type="button"
      class="tab"
      role="tab"
      aria-selected={tab === 'queue'}
      class:active={tab === 'queue'}
      onclick={() => jobs.setTab('queue')}
    >
      Queue {#if queuedCount > 0}<span class="count">{queuedCount}</span>{/if}
    </button>
    <button
      type="button"
      class="tab"
      role="tab"
      aria-selected={tab === 'completed'}
      class:active={tab === 'completed'}
      onclick={() => jobs.setTab('completed')}
    >
      Completed {#if completedCount > 0}<span class="count">{completedCount}</span>{/if}
    </button>
    <button
      type="button"
      class="tab"
      role="tab"
      aria-selected={tab === 'failed'}
      class:active={tab === 'failed'}
      onclick={() => jobs.setTab('failed')}
    >
      Failed {#if failedCount > 0}<span class="count">{failedCount}</span>{/if}
    </button>

    <span class="spacer"></span>

    {#if tab === 'queue' && queuedCount > 0}
      <button type="button" class="bulk" onclick={handleCancelAll} disabled={jobs.cancellingAll}>
        <CircleX size={12} aria-hidden="true" />
        {jobs.cancellingAll ? 'Cancelling…' : 'Cancel all'}
      </button>
    {/if}
    {#if (tab === 'completed' && completedCount > 0) || (tab === 'failed' && failedCount > 0)}
      <button type="button" class="bulk" onclick={handleClearCompleted} disabled={jobs.clearingCompleted}>
        <Trash2 size={12} aria-hidden="true" />
        {jobs.clearingCompleted ? 'Clearing…' : 'Clear'}
      </button>
    {/if}
  </div>

  {#if tab !== 'queue'}
    <!-- The list keeps the newest MAX_JOB_HISTORY finished jobs; the Ledger
         keeps every one (#533). -->
    <p class="trimmed">
      Older finished jobs leave this list.
      <button type="button" class="ledger-link" onclick={() => ui.navigate('activity', { tab: 'history' })}>
        Every job is in the Ledger
      </button>
    </p>
  {/if}

  {#if pageJobs.length === 0}
    {#if tab === 'queue'}
      <EmptyState
        title="Queue is idle"
        description="Submit a conversion from the Convert panel to see jobs here."
        glyph="∅"
      />
    {:else if tab === 'completed'}
      <EmptyState
        title="No completed jobs yet"
        description="Successful conversions show up here once they finish."
        glyph="∅"
      />
    {:else}
      <EmptyState
        title="No failed jobs"
        description="Failed or cancelled jobs land here. The queue looks healthy."
        glyph="∅"
      />
    {/if}
  {:else}
    <ul class="rows">
      {#each pageJobs as job (job.id)}
        <JobRow {job} />
      {/each}
    </ul>
  {/if}

  <div class="footer">
    <Pager {page} {pageCount} onpage={(p) => { jobs.page = p; }} />
  </div>
</section>

<style>
  .panel { display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
  .title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }
  .panel-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .tabs {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--space-2);
    flex-wrap: wrap;
  }
  .tab {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    border: 1px solid transparent;
    background: transparent;
    color: var(--text-2);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: var(--text-sm);
    cursor: pointer;
  }
  .tab:hover { background: var(--surface-2); color: var(--text-1); }
  .tab.active {
    background: var(--accent-muted);
    color: var(--accent);
    border-color: var(--accent);
    font-weight: var(--weight-medium);
  }
  .count {
    background: var(--surface-3);
    color: var(--text-1);
    border-radius: var(--radius-full);
    padding: 0 6px;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }
  .tab.active .count { background: var(--accent); color: var(--accent-contrast); }
  .spacer { flex: 1; }

  .bulk {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    background: transparent;
    border: 1px solid var(--border-subtle);
    color: var(--text-2);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: var(--text-xs);
    cursor: pointer;
  }
  .bulk:hover:not(:disabled) {
    background: var(--surface-2);
    color: var(--text-1);
    border-color: var(--border-strong);
  }
  .bulk:disabled { opacity: 0.6; cursor: not-allowed; }

  .trimmed {
    margin: 0;
    color: var(--text-3);
    font-size: var(--text-xs);
  }
  .ledger-link {
    background: none;
    border: 0;
    padding: 0;
    color: var(--accent);
    font: inherit;
    cursor: pointer;
    text-decoration: underline;
  }

  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    max-height: 480px;
    overflow-y: auto;
  }
  .footer {
    display: flex;
    align-items: center;
    justify-content: flex-end;
  }
</style>
