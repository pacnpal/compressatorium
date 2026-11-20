<script>
  /**
   * Activity: what needs you, what's running, and everything that ran. The
   * tab is in the URL (`#/activity/<tab>`); the bare route opens on what
   * matters now: Needs attention when anything does, else Running when
   * anything runs, else History.
   */
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ledger } from '$lib/stores/ledger.svelte.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import FindingsView from './FindingsView.svelte';
  import LedgerView from './LedgerView.svelte';
  import JobsPanel from '$lib/components/panels/JobsPanel.svelte';
  import SavingsCard from '$lib/components/dashboard/SavingsCard.svelte';
  import QueueSummaryCard from '$lib/components/dashboard/QueueSummaryCard.svelte';

  const attention = $derived(findings.openCount);
  const running = $derived(jobs.activeCount);
  const tab = $derived(
    ui.activityTab ?? (attention > 0 ? 'attention' : running > 0 ? 'running' : 'history'),
  );
  const TABS = $derived([
    { id: 'attention', label: `Needs attention (${attention})` },
    { id: 'running', label: `Running (${running})` },
    { id: 'history', label: 'History' },
  ]);

  // Refresh the savings card whenever the set of finished jobs changes.
  // Keyed on identity, not count: once history hits its cap a new finished
  // job replaces an evicted one and the count stays flat.
  const finished = $derived(
    jobs.jobs
      .filter((j) => j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled')
      .map((j) => j.id)
      .join(','),
  );
  $effect(() => {
    void finished;
    ledger.refresh();
  });
</script>

<section class="view" aria-labelledby="activity-title">
  <header class="header">
    <h1 id="activity-title">Activity</h1>
    <p class="hint">What needs you, what's running, and everything that ran.</p>
  </header>

  <div class="summary">
    <QueueSummaryCard />
    <SavingsCard />
  </div>

  <div class="tabs" role="tablist" aria-label="Activity sections">
    {#each TABS as t (t.id)}
      <button
        type="button"
        class="tab"
        class:active={tab === t.id}
        role="tab"
        id={`activity-tab-${t.id}`}
        aria-selected={tab === t.id}
        aria-controls="activity-panel"
        onclick={() => ui.navigate('activity', { tab: t.id })}
      >{t.label}</button>
    {/each}
  </div>

  <div role="tabpanel" id="activity-panel" aria-labelledby={`activity-tab-${tab}`} class="panel">
    {#if tab === 'attention'}
      <FindingsView />
    {:else if tab === 'running'}
      <!-- The whole jobs panel for now: a Check Rule's runs write no History
           row, so its Completed and Failed tabs are still the only place
           those show. -->

      <div class="card"><JobsPanel /></div>
    {:else}
      <LedgerView />
    {/if}
  </div>
</section>

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-5);
    max-width: var(--container-max);
    margin: 0 auto;
    width: 100%;
    min-width: 0;
  }
  .header h1 {
    margin: 0;
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .hint { color: var(--text-2); margin-top: var(--space-1); }
  .summary {
    display: grid;
    gap: var(--space-4);
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  }
  .tabs {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    border-bottom: 1px solid var(--border-subtle);
  }
  .tab {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    padding: var(--space-2) var(--space-3);
    color: var(--text-2);
    font-size: var(--text-sm);
    cursor: pointer;
  }
  .tab:hover { color: var(--text-1); }
  .tab.active {
    color: var(--text-1);
    border-bottom-color: var(--accent);
    font-weight: var(--weight-medium);
  }
  .tab:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .panel { min-width: 0; }
  /* The hosted views bring their own page padding; take it back here. */
  .panel > :global(.view) { padding: 0; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
  }
</style>
