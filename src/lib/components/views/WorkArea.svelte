<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';

  const tool = $derived(registry.forTool(ui.workspaceTool));
</script>

<section class="view" aria-labelledby="workspace-title">
  <header class="header">
    <h1 id="workspace-title">{tool?.label ?? 'Workspace'}</h1>
    <p class="hint">{tool?.hint ?? ''}</p>
  </header>

  <div class="panels">
    <article class="panel" aria-label="Volumes and files">
      <h2 class="panel-title">Volumes & Files</h2>
      <EmptyState
        title="File browser is on the way"
        description="Volumes, breadcrumb, and the convertible-file list arrive in the next phase."
        glyph="◇"
      />
    </article>
    <article class="panel" aria-label="Conversion configuration">
      <h2 class="panel-title">Convert</h2>
      <EmptyState
        title="Conversion controls are on the way"
        description="Pick a mode, configure compression, and submit jobs from this panel once it's wired up."
        glyph="◈"
      />
    </article>
    <article class="panel" aria-label="Job queue">
      <h2 class="panel-title">Jobs</h2>
      <EmptyState
        title="Job queue is on the way"
        description="Live progress and history for every conversion will appear here."
        glyph="◉"
      />
    </article>
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
  .panels {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
  }
  @media (min-width: 1100px) {
    .panels {
      grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
    }
  }
  .panel {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
  }
  .panel-title {
    margin: 0 0 var(--space-2);
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
</style>
