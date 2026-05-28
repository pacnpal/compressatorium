<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';

  const tool = $derived(registry.forTool(ui.workspaceTool));
  const modes = $derived(tool ? Array.from(registry.modesByGroup(tool.id).entries()) : []);
</script>

<section class="view">
  <header class="header">
    <h1>{tool?.label ?? 'Workspace'}</h1>
    <p class="hint">{tool?.hint ?? ''}</p>
  </header>

  <div class="panels">
    <article class="panel">
      <h2 class="panel-title">Volumes & Files</h2>
      <p class="placeholder">FileList + Breadcrumb + VolumeList land in P4.</p>
    </article>
    <article class="panel">
      <h2 class="panel-title">Convert</h2>
      <p class="placeholder">ConversionConfig + ToolPicker + CompressionPicker land in P5.</p>
      {#if tool}
        <details>
          <summary>Available modes ({tool.modes.length})</summary>
          <ul class="modes">
            {#each modes as [group, list] (group)}
              <li>
                <strong>{registry.groupLabel(group)}</strong>
                <ul>
                  {#each list as m (m.mode)}
                    <li>
                      <code>{m.mode}</code> — {m.label}
                      {#if m.supportsCompression}<span class="tag">compression</span>{/if}
                      {#if m.supportsCompressionLevel}<span class="tag">level</span>{/if}
                      {#if m.supportsDeleteOnVerify}<span class="tag">del-on-verify</span>{/if}
                      {#if m.allowsArchiveInput}<span class="tag">archives</span>{/if}
                    </li>
                  {/each}
                </ul>
              </li>
            {/each}
          </ul>
        </details>
      {/if}
    </article>
    <article class="panel">
      <h2 class="panel-title">Jobs</h2>
      <p class="placeholder">JobList lands in P5.</p>
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
  .placeholder {
    color: var(--text-3);
    font-style: italic;
    margin: 0;
  }
  details {
    margin-top: var(--space-3);
    color: var(--text-2);
  }
  summary {
    cursor: pointer;
    user-select: none;
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .modes {
    list-style: none;
    padding: 0;
    margin: var(--space-2) 0 0;
    font-size: var(--text-sm);
  }
  .modes > li {
    margin-top: var(--space-2);
  }
  .modes ul {
    margin: var(--space-1) 0 0 var(--space-4);
    padding: 0;
    list-style: disc;
  }
  code {
    font-family: var(--font-mono);
    background: var(--surface-2);
    padding: 1px 5px;
    border-radius: var(--radius-xs);
    color: var(--text-1);
  }
  .tag {
    display: inline-block;
    margin-left: var(--space-1);
    padding: 1px 6px;
    background: var(--badge-bg);
    color: var(--badge-text);
    border-radius: var(--radius-full);
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
  }
</style>
