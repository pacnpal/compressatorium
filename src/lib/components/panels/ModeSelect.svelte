<script>
  // Every format any ready tool makes, grouped by what it does
  // ($lib/tools/convertTargets.js). Picking one from another tool switches the
  // panel to that tool, so no tool needs a place of its own.
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { convertTargets } from '$lib/tools/convertTargets.js';

  const mode = $derived(conversion.mode);
  const groups = $derived(
    convertTargets({
      perMode: true,
      ready: ui.toolsKnown ? ui.visibleTools.map((t) => t.id) : null,
    }).filter((g) => g.group !== 'Recommended'),
  );

  function choose(next) {
    const tool = registry.toolForMode(next);
    // setPrimaryTool resets the mode to the tool's default, so it goes first.
    if (tool && tool.id !== conversion.primaryTool) conversion.setPrimaryTool(tool.id);
    conversion.chooseTarget(next);
  }
</script>

<label class="mode-select">
  <span class="label">Convert to</span>
  <select value={mode} onchange={(e) => choose(e.currentTarget.value)} aria-label="Convert to">
    {#each groups as g (g.group)}
      <optgroup label={g.group}>
        {#each g.options as o (o.value)}
          <option value={o.value}>{o.label} · {o.hint}</option>
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
    min-width: 0;
    max-width: 100%;
  }
  select:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
</style>
