<script>
  /**
   * Activity: what needs you, what's running, and everything that ran. The
   * tab is in the URL (`#/activity/<tab>`); the bare route opens on what
   * matters now: Needs attention when anything does, else Running when
   * anything runs, else History.
   */
  import { onMount } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { terminalJobIds } from '$lib/stores/jobConstants.js';
  import { ledger } from '$lib/stores/ledger.svelte.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import ActivitySummary from '$lib/components/activity/ActivitySummary.svelte';
  import NeedsAttention from '$lib/components/activity/NeedsAttention.svelte';
  import RunningList from '$lib/components/activity/RunningList.svelte';
  import HistoryList from '$lib/components/activity/HistoryList.svelte';

  const attention = $derived(findings.openCount);
  // The queue as Running lists it (background scans left out).
  const running = $derived(jobs.runningList.length);
  // The bare route opens on what matters now: Needs attention when anything
  // does, else Running when anything runs, else History. Decided once per
  // visit, after the findings read that counts (one a newer read superseded
  // doesn't) and the first jobs snapshot, so a job ending doesn't swap the tab
  // (and its list) out from under you. A tab in the URL always wins.
  const live = $derived(attention > 0 ? 'attention' : running > 0 ? 'running' : 'history');
  const readsAtMount = findings.reads;
  const settled = $derived(findings.reads > readsAtMount && jobs.hydrated);
  let opening = $state(null);
  $effect(() => {
    if (opening === null && settled) opening = live;
  });
  const tab = $derived(ui.activityTab ?? opening ?? live);
  const TABS = $derived([
    { id: 'attention', label: `Needs attention (${attention})` },
    { id: 'running', label: `Running (${running})` },
    { id: 'history', label: 'History' },
  ]);

  // Refresh the space saved whenever the set of finished jobs changes.
  // Keyed on identity, not count: once history hits its cap a new finished
  // job replaces an evicted one and the count stays flat.
  const finished = $derived(terminalJobIds(jobs.jobs).join(','));
  $effect(() => {
    void finished;
    ledger.refresh();
  });

  // Entering Activity reads what needs you now, so the opening tab is right
  // even when nothing has finished since the last read.
  onMount(() => {
    findings.load();
  });
</script>

<section class="view" aria-labelledby="activity-title">
  <header class="header">
    <h1 id="activity-title">Activity</h1>
    <p class="hint">What needs you, what's running, and everything that ran.</p>
  </header>

  <ActivitySummary />

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
      <NeedsAttention />
    {:else if tab === 'running'}
      <RunningList />
    {:else}
      <HistoryList />
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
</style>
