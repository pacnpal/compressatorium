<script>
  // "Compatible with…" (#429), shared by the compression picker and the Rule
  // editor. Choosing a target hands its preferred codecs to `onpick`; a codec
  // the target isn't known to read shows a warning and is never blocked. The
  // table and its sources come from the server (compatTargets store).

  import { onMount } from 'svelte';
  import { compatTargets, compatWarning } from '$lib/stores/compatTargets.svelte.js';
  import Select from '$lib/components/ui/Select.svelte';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  /**
   * @type {{
   *   mode: string,
   *   codecs: string[] | null,
   *   target?: string,
   *   onpick: (codecs: string[]) => void,
   * }}
   * `codecs` is what the job runs with, or null for the tool's own default.
   */
  let { mode, codecs, target = $bindable(''), onpick } = $props();

  onMount(() => { compatTargets.load(); });

  const options = $derived([
    { value: '', label: 'Any (no target)' },
    ...compatTargets.targets.map((t) => ({ value: t.id, label: t.label })),
  ]);
  const chosen = $derived(compatTargets.targets.find((t) => t.id === target) ?? null);
  const entry = $derived(target ? compatTargets.entryFor(target, mode) : null);
  const warning = $derived(compatWarning(chosen, entry, codecs));

  function pick(id) {
    const preferred = compatTargets.entryFor(id, mode)?.preferred;
    if (preferred?.length) onpick(preferred);
  }
</script>

{#if compatTargets.targets.length}
  <div class="compat">
    <Select
      label="Compatible with…"
      bind:value={target}
      {options}
      onchange={pick}
    />
    {#if warning}
      <p class="warn" role="status"><TriangleAlert size={12} aria-hidden="true" /> {warning}</p>
    {/if}
    {#if entry}
      <p class="hint">
        {#if entry.note}{`${entry.note} `}{/if}Source{entry.sources.length > 1 ? 's' : ''}:
        {#each entry.sources as url, i (url)}{#if i}, {/if}<a href={url} target="_blank" rel="noopener noreferrer">{i + 1}</a>{/each}
      </p>
    {/if}
  </div>
{/if}

<style>
  .compat { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .warn {
    display: flex;
    align-items: baseline;
    gap: var(--space-1);
    margin: 0;
    color: var(--warning);
    font-size: var(--text-xs);
  }
  .hint { margin: 0; color: var(--text-3); font-size: var(--text-xs); }
  .hint a { color: var(--accent); }
</style>
