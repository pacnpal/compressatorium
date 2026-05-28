<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Settings2 from '@lucide/svelte/icons/settings-2';
</script>

<section class="panel" aria-label="Conversion configuration">
  <h2 class="panel-title">
    <Settings2 size={14} aria-hidden="true" /> Convert
  </h2>
  <EmptyState
    title="Conversion controls land next"
    description={`Pick a mode for ${ui.workspaceTool}, configure compression, and submit jobs from this panel — wiring up next.`}
    glyph="◈"
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
