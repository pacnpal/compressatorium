<script>
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { registry } from '$lib/tools/registry.js';

  const tool = $derived(conversion.currentTool);
  const mode = $derived(conversion.mode);
  const groups = $derived(tool ? Array.from(registry.modesByGroup(tool.id).entries()) : []);
</script>

<label class="mode-select">
  <span class="label">Mode</span>
  <select
    value={mode}
    onchange={(e) => conversion.setMode(e.currentTarget.value)}
    aria-label="Conversion mode"
  >
    {#each groups as [group, list] (group)}
      <optgroup label={registry.groupLabel(group, tool?.id)}>
        {#each list as m (m.mode)}
          <option value={m.mode}>{m.label}</option>
        {/each}
      </optgroup>
    {/each}
  </select>
</label>

<style>
  .mode-select {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }
  .label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  select {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
    cursor: pointer;
  }
  select:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
</style>
