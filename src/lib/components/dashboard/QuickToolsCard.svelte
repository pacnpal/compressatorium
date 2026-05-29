<script>
  // Deep-link tile per tool from the registry. Adding a 4th tool
  // automatically gets a tile here — no edits required.

  import { registry } from '$lib/tools/registry.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import StatCard from './StatCard.svelte';
  import Zap from '@lucide/svelte/icons/zap';

  const tools = $derived(registry.all());
</script>

<StatCard title="Quick tools" subtitle="Jump into a workspace" accent="var(--badge-dat-match)">
  {#snippet icon()}<Zap size={16} />{/snippet}
  {#snippet body()}
    <ul class="quick-list">
      {#each tools as tool (tool.id)}
        <li>
          <button
            type="button"
            class="quick-btn"
            style:--tool-accent={tool.accent ?? 'var(--accent)'}
            onclick={() => ui.navigate('workspace', tool.id)}
          >
            <span class="glyph" aria-hidden="true">{tool.glyph ?? '◇'}</span>
            <span class="tool-meta">
              <span class="tool-label">{tool.label}</span>
              <span class="tool-hint">{tool.hint}</span>
            </span>
          </button>
        </li>
      {/each}
    </ul>
  {/snippet}
</StatCard>

<style>
  .quick-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .quick-btn {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    padding: var(--space-2);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-1);
    cursor: pointer;
    text-align: left;
    transition: background var(--dur-fast) var(--ease-out), border-color var(--dur-fast) var(--ease-out);
  }
  .quick-btn:hover { background: var(--surface-3); border-color: var(--tool-accent); }
  .quick-btn:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .glyph {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    background: color-mix(in srgb, var(--tool-accent) 14%, transparent);
    color: var(--tool-accent);
    border-radius: var(--radius-sm);
    font-size: var(--text-xs);
    font-weight: var(--weight-bold);
    flex-shrink: 0;
  }
  .tool-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .tool-label { font-size: var(--text-sm); font-weight: var(--weight-medium); color: var(--text-1); }
  .tool-hint { font-size: var(--text-xs); color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
