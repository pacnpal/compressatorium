<script>
  // Duplicate-output preflight modal. Opens when ConvertPanel runs
  // checkDuplicates before submit and the backend reports at least one
  // output path already exists. The user picks one batch-wide action
  // (skip vs overwrite); we resolve a promise the caller is awaiting so
  // ConvertPanel can chain straight into submit() with the chosen
  // duplicateAction.

  import { conversion } from '$lib/stores/conversion.svelte.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  /** @type {{ open: boolean, onResolve: (action: 'skip'|'overwrite'|null) => void }} */
  let { open, onResolve } = $props();

  const check = $derived(conversion.duplicateCheck ?? []);
  const conflicts = $derived(check.filter((d) => d?.exists));
</script>

<BaseModal {open} onClose={() => onResolve(null)} title="Duplicate outputs" size="md">
  {#snippet titleIcon()}<TriangleAlert size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <p class="dup-lead">
      <Badge tone="warning">{conflicts.length}</Badge>
      output file{conflicts.length === 1 ? '' : 's'} already exist. Skip those inputs
      or overwrite the existing outputs?
    </p>
    <ul class="dup-list">
      {#each conflicts.slice(0, 12) as d (d.file_path)}
        <li class="dup-row">
          <span class="dup-from" title={d.file_path}>{d.file_path}</span>
          <span class="dup-arrow">→</span>
          <span class="dup-to" title={d.output_path}>{d.output_path}</span>
        </li>
      {/each}
      {#if conflicts.length > 12}
        <li class="dup-more">…and {conflicts.length - 12} more</li>
      {/if}
    </ul>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={() => onResolve(null)}>Cancel</Button>
    <Button variant="secondary" onclick={() => onResolve('skip')}>Skip duplicates</Button>
    <Button variant="destructive" onclick={() => onResolve('overwrite')}>Overwrite all</Button>
  {/snippet}
</BaseModal>

<style>
  .dup-lead { margin: 0; font-size: var(--text-sm); color: var(--text-1); display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .dup-list { list-style: none; margin: 0; padding: 0; max-height: 280px; overflow-y: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2); }
  .dup-row { display: grid; grid-template-columns: 1fr auto 1fr; gap: var(--space-2); align-items: center; padding: 4px var(--space-2); border-bottom: 1px solid var(--border-subtle); font-family: var(--font-mono); font-size: var(--text-xs); }
  .dup-row:last-child { border-bottom: none; }
  .dup-from, .dup-to { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dup-arrow { color: var(--text-3); }
  .dup-more { padding: 4px var(--space-2); color: var(--text-3); font-size: var(--text-xs); font-style: italic; }
</style>
