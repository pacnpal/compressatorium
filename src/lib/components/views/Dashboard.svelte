<script>
  import QueueSummaryCard from '$lib/components/dashboard/QueueSummaryCard.svelte';
  import VolumeOverviewCard from '$lib/components/dashboard/VolumeOverviewCard.svelte';
  import RecentConversionsCard from '$lib/components/dashboard/RecentConversionsCard.svelte';
  import SavingsCard from '$lib/components/dashboard/SavingsCard.svelte';
  import VerificationStatusCard from '$lib/components/dashboard/VerificationStatusCard.svelte';
  import QuickToolsCard from '$lib/components/dashboard/QuickToolsCard.svelte';
  import MetadataScanCard from '$lib/components/dashboard/MetadataScanCard.svelte';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ledger } from '$lib/stores/ledger.svelte.js';

  // One refresh of the Ledger cards per terminal job (and on mount).
  const finished = $derived(
    jobs.jobs.filter(
      (j) => j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled',
    ).length,
  );

  $effect(() => {
    void finished;
    ledger.refresh();
  });
</script>

<section class="view" aria-labelledby="dashboard-title">
  <header class="header">
    <h1 id="dashboard-title">Dashboard</h1>
    <p class="hint">At-a-glance view of jobs, volumes, and recent activity.</p>
  </header>

  <div class="grid">
    <QueueSummaryCard />
    <VerificationStatusCard />
    <VolumeOverviewCard />
    <SavingsCard />
    <RecentConversionsCard />
    <MetadataScanCard />
    <QuickToolsCard />
  </div>
</section>

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
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
  .hint {
    color: var(--text-2);
    margin-top: var(--space-1);
  }
  .grid {
    display: grid;
    gap: var(--space-4);
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    min-width: 0;
  }
</style>
