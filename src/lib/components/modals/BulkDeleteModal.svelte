<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { toast } from 'svelte-sonner';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  const open = $derived(!!ui.bulkDeleteEntries);
  const entries = $derived(ui.bulkDeleteEntries ?? []);
  const paths = $derived(entries.map((e) => (typeof e === 'string' ? e : e?.path)).filter(Boolean));
  let busy = $state(false);

  function close() {
    if (busy) return;
    ui.bulkDeleteEntries = null;
  }

  async function handleConfirm() {
    if (paths.length === 0) return;
    busy = true;
    try {
      const result = await api.deleteBatch(paths);
      const deleted = result?.deleted ?? paths.length;
      const failed = result?.failed ?? 0;
      // Close + report success immediately so a downstream refresh
      // failure doesn't surface as "Failed to delete" after the
      // backend already removed the files.
      ui.bulkDeleteEntries = null;
      fileBrowser.clearSelection();
      if (failed > 0) {
        toast.warning(`Deleted ${deleted}; ${failed} failed`);
      } else {
        toast.success(`Deleted ${deleted} file${deleted === 1 ? '' : 's'}`);
      }
      try {
        await fileBrowser.refresh({ force: true });
      } catch (_e) {
        toast.warning('Deleted; refreshing the listing failed');
      }
    } catch (e) {
      toast.error(e?.message ?? 'Failed to delete files');
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  {open}
  onClose={close}
  title="Delete {paths.length} file{paths.length === 1 ? '' : 's'}?"
  description="Permanent — files are removed from disk and not recoverable. Output files queued for conversion are not affected."
  size="md"
>
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <ul class="bd-list">
      {#each paths.slice(0, 20) as p (p)}
        <li class="bd-row" title={p}>{p}</li>
      {/each}
      {#if paths.length > 20}
        <li class="bd-more">…and {paths.length - 20} more</li>
      {/if}
    </ul>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={close} disabled={busy}>Keep</Button>
    <Button variant="destructive" onclick={handleConfirm} disabled={busy || paths.length === 0} loading={busy}>
      Delete {paths.length}
    </Button>
  {/snippet}
</BaseModal>

<style>
  .bd-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 280px;
    overflow-y: auto;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--surface-2);
  }
  .bd-row {
    padding: 4px var(--space-2);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-2);
    border-bottom: 1px solid var(--border-subtle);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .bd-row:last-child { border-bottom: none; }
  .bd-more {
    padding: 4px var(--space-2);
    color: var(--text-3);
    font-size: var(--text-xs);
    font-style: italic;
  }
</style>
