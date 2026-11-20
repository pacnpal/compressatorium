<script>
  /**
   * Settings → Converters: each program Compressatorium uses, the formats it
   * makes, and whether it can run here (from GET /api/tools, through the ui
   * store). A tool that can't run says why when the server gives a reason.
   */
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { convertTargets } from '$lib/tools/convertTargets.js';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';

  const ready = $derived(new Set(ui.visibleTools.map((t) => t.id)));

  // What a tool makes, in format names: its Make-smaller formats, else what it unpacks to.
  function makes(toolId) {
    const groups = convertTargets({ toolIds: [toolId] });
    const pick = groups.find((g) => g.group === 'Make smaller')
      ?? groups.find((g) => g.group === 'Other')
      ?? groups.find((g) => g.group === 'Unpack');
    return pick ? pick.options.map((o) => o.label).join(', ') : '';
  }

  const rows = $derived(
    registry.all().map((tool) => ({
      id: tool.id,
      label: tool.label,
      makes: makes(tool.id),
      ready: ready.has(tool.id),
      reason: ui.toolReasons?.[tool.id] ?? '',
    })),
  );
</script>

<p class="hint">The programs Compressatorium uses. Docker images include all of them.</p>

{#if !ui.toolsKnown}
  <div class="state"><Spinner size="sm" label="Checking" /> Checking what can run here…</div>
{:else}
  <ul class="converters">
    {#each rows as row (row.id)}
      <li class="row">
        <span class="makes">{row.makes}</span>
        <span class="tool">{row.label}</span>
        <span class="status">
          {#if row.ready}
            <Badge tone="success">Ready ✓</Badge>
          {:else}
            <Badge tone="warning">Not ready</Badge>
            <span class="reason">{row.reason || "It can't run here."}</span>
          {/if}
        </span>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .hint { margin: 0 0 var(--space-3); color: var(--text-2); }
  .state { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); }
  .converters { list-style: none; margin: 0; padding: 0; }
  .row {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(0, 1fr) minmax(0, 2fr);
    gap: var(--space-3);
    align-items: center;
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  .row:last-child { border-bottom: none; }
  .makes { color: var(--text-1); font-weight: var(--weight-medium); overflow-wrap: anywhere; }
  .tool { color: var(--text-2); }
  .status { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
  .reason { color: var(--text-2); font-size: var(--text-xs); overflow-wrap: anywhere; }
  @media (max-width: 600px) {
    .row { grid-template-columns: minmax(0, 1fr) auto; }
    .status { grid-column: 1 / -1; }
  }
</style>
