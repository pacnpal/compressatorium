<script>
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import ListTodo from '@lucide/svelte/icons/list-todo';
</script>

<section class="panel" aria-label="Job queue">
  <h2 class="panel-title">
    <ListTodo size={14} aria-hidden="true" /> Jobs
  </h2>
  <EmptyState
    title="Job queue is on the way"
    description="Live progress and history for every conversion will appear here."
    glyph="◉"
  />
</section>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
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
</style>
