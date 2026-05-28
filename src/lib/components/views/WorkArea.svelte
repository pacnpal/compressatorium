<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import VolumeList from '$lib/components/panels/VolumeList.svelte';
  import FileList from '$lib/components/panels/FileList.svelte';
  import ConvertPanel from '$lib/components/panels/ConvertPanel.svelte';
  import JobsPanel from '$lib/components/panels/JobsPanel.svelte';

  const tool = $derived(registry.forTool(ui.workspaceTool));
</script>

<section class="view" aria-labelledby="workspace-title">
  <header class="header">
    <div>
      <h1 id="workspace-title">{tool?.label ?? 'Workspace'}</h1>
      <p class="hint">{tool?.hint ?? ''}</p>
    </div>
  </header>

  <div class="grid">
    <aside class="side">
      <VolumeList />
    </aside>

    <article class="main">
      <FileList />
    </article>

    <article class="right">
      <ConvertPanel />
      <div class="separator" aria-hidden="true"></div>
      <JobsPanel />
    </article>
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
  .hint {
    color: var(--text-2);
    margin-top: var(--space-1);
  }

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
    min-width: 0;
  }
  @media (min-width: 900px) {
    .grid { grid-template-columns: 220px minmax(0, 1fr); }
  }
  @media (min-width: 1280px) {
    .grid { grid-template-columns: 220px minmax(0, 1.5fr) minmax(320px, 1fr); }
  }

  .side, .main, .right {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
    min-width: 0;
  }
  .right { display: flex; flex-direction: column; gap: var(--space-4); }
  .separator {
    height: 1px;
    background: var(--border-subtle);
    margin: var(--space-2) 0;
  }
</style>
