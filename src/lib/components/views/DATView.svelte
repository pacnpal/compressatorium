<script>
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
</script>

<section class="view" aria-labelledby="dat-title">
  <header class="header">
    <h1 id="dat-title">DAT Library</h1>
    <p class="hint">Match converted files against No-Intro / Redump / MAMERedump datasets.</p>
  </header>
  <article class="panel">
    <EmptyState
      title="DAT management is on the way"
      description="Importing custom DATs, syncing MAMERedump, and per-file match results land in a later phase."
      glyph="≣"
    />
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
