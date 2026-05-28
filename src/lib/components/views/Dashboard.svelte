<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
</script>

<section class="view" aria-labelledby="dashboard-title">
  <header class="header">
    <h1 id="dashboard-title">Dashboard</h1>
    <p class="hint">At-a-glance view of jobs, volumes, and recent activity.</p>
  </header>

  <article class="panel">
    <EmptyState
      title="Stat cards coming soon"
      description="Live queue status, recent conversions, verification rates, and DAT match coverage will surface here. For now, head to the workspace to start converting."
      glyph="◐"
    >
      {#snippet actions()}
        <Button variant="primary" onclick={() => ui.navigate('workspace', ui.workspaceTool)}>
          Open workspace
        </Button>
      {/snippet}
    </EmptyState>
  </article>
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
  .panel {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
  }
</style>
