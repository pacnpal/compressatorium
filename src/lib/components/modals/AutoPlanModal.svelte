<script>
  // Auto plan preview. ConvertPanel fetches the backend's plan (each Image's
  // Platform, how it was worked out, and its default target) and shows it here
  // before anything is queued. Conflicted and Unsorted Images are listed with
  // the reason they are left alone.

  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Wand from '@lucide/svelte/icons/wand-sparkles';

  /** @type {{ open: boolean, onResolve: (proceed: boolean) => void }} */
  let { open, onResolve } = $props();

  const plan = $derived(conversion.autoPlan ?? []);
  const queued = $derived(plan.filter((row) => row.mode));
  const skipped = $derived(plan.filter((row) => !row.mode));

  function baseName(path) {
    const member = (path ?? '').split('::').pop();
    return member.split(/[/\\]/).pop();
  }

  function targetLabel(row) {
    const spec = registry.specFor(row.mode);
    const tool = registry.forTool(row.tool_id);
    const label = spec?.label ?? row.mode;
    return tool?.label && !label.includes(tool.label) ? `${tool.label} ${label}` : label;
  }
</script>

<BaseModal {open} onClose={() => onResolve(false)} title="Auto plan" size="md">
  {#snippet titleIcon()}<Wand size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <p class="lead">
      <Badge tone="success">{queued.length}</Badge>
      Image{queued.length === 1 ? '' : 's'} will convert.
      {#if skipped.length}
        <Badge tone="warning">{skipped.length}</Badge>
        skipped.
      {/if}
      Existing outputs are skipped.
    </p>
    <ul class="rows">
      {#each plan as row (row.file_path)}
        <li class="row" class:skip={!row.mode}>
          <span class="name" title={row.file_path}>{baseName(row.file_path)}</span>
          <span class="platform" title={row.reason}>
            {#if row.platform}
              {row.platform}<span class="how">{row.how}</span>
            {:else}
              {row.how === 'conflict' ? 'Conflict' : 'Unsorted'}
            {/if}
          </span>
          {#if row.mode}
            <span class="target">{targetLabel(row)}</span>
          {:else}
            <span class="target reason">{row.skip_reason}</span>
          {/if}
        </li>
      {/each}
    </ul>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={() => onResolve(false)}>Cancel</Button>
    <Button variant="primary" disabled={queued.length === 0} onclick={() => onResolve(true)}>
      Convert {queued.length}
    </Button>
  {/snippet}
</BaseModal>

<style>
  .lead { margin: 0; font-size: var(--text-sm); color: var(--text-1); display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .rows { list-style: none; margin: 0; padding: 0; max-height: 320px; overflow-y: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2); }
  .row { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 0.8fr) minmax(0, 1.4fr); gap: var(--space-2); align-items: baseline; padding: 4px var(--space-2); border-bottom: 1px solid var(--border-subtle); font-size: var(--text-xs); }
  .row:last-child { border-bottom: none; }
  .name { font-family: var(--font-mono); color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .platform { color: var(--text-1); }
  .how { color: var(--text-3); margin-left: var(--space-1); }
  .target { color: var(--text-2); overflow-wrap: anywhere; }
  .skip .name, .skip .platform { color: var(--text-3); }
  .reason { color: var(--warning-text); }
  @media (max-width: 600px) {
    .row { grid-template-columns: 1fr; gap: 2px; }
  }
</style>
