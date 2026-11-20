<script>
  /**
   * Activity's summary strip: four numbers, each a link to the tab that
   * explains it. What needs you, what runs, the space saved so far and how
   * many games passed a check.
   */
  import { findings } from '$lib/stores/findings.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ledger } from '$lib/stores/ledger.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { formatSize } from '$lib/api/format.js';
  import { hashFor } from '$lib/router/routes.js';
  import Panel from '$lib/components/ui/Panel.svelte';

  const running = $derived(jobs.runningList);
  const busy = $derived(running.filter((j) => j.status === 'processing').length);
  const checked = $derived(verification.statuses.size);

  const stats = $derived([
    {
      label: 'Needs you',
      value: String(findings.openCount),
      href: hashFor('activity', { tab: 'attention' }),
    },
    {
      label: 'Running',
      value: running.length > busy ? `${busy} · ${running.length - busy} waiting` : String(busy),
      href: hashFor('activity', { tab: 'running' }),
    },
    {
      label: 'Space saved',
      value: formatSize(ledger.savings?.saved_bytes ?? 0),
      href: hashFor('activity', { tab: 'history' }),
    },
    {
      // "{n} games checked", the number large.
      label: 'Checked',
      value: String(checked),
      unit: checked === 1 ? 'game checked' : 'games checked',
      href: `${hashFor('activity', { tab: 'history' })}?status=checked`,
    },
  ]);
</script>

<Panel>
  <ul class="stats" aria-label="Activity at a glance">
    {#each stats as stat (stat.label)}
      <li>
        <a class="stat" href={stat.href}>
          <span class="label">{stat.label}</span>
          <span class="value">
            {stat.value}{#if stat.unit}<span class="unit">{stat.unit}</span>{/if}
          </span>
        </a>
      </li>
    {/each}
  </ul>
</Panel>

<style>
  .stats {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-3);
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 130px), 1fr));
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-md);
    color: inherit;
    text-decoration: none;
  }
  .stat:hover { background: var(--surface-2); }
  .stat:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .label { font-size: var(--text-xs); color: var(--text-2); }
  .value {
    font-size: var(--text-xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    font-variant-numeric: tabular-nums;
  }
  .unit {
    font-size: var(--text-sm);
    font-weight: var(--weight-regular);
    margin-left: 0.3em;
    color: var(--text-2);
  }
</style>
