<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { toast } from 'svelte-sonner';
  import ConfirmModal from './ConfirmModal.svelte';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  const open = $derived(!!ui.deleteTarget);
  const target = $derived(ui.deleteTarget);
  let busy = $state(false);
  // Two-step confirmation when the source has unverified replacement
  // outputs. Same shape as BulkDeleteModal — required to mirror the
  // legacy single-delete verification gate. Reset on open.
  let acknowledged = $state(false);
  $effect(() => {
    if (open) acknowledged = false;
  });

  // entry.outputs entries with `exists: true` and a path that isn't
  // in verification.statuses — i.e. a generated product whose
  // correctness hasn't been confirmed. Deleting the source before the
  // output is verified is the risk we want to surface.
  const unverifiedOutputs = $derived.by(() => {
    const outs = target && Array.isArray(target.outputs) ? target.outputs : [];
    return outs.filter(
      (o) => o?.exists && o.path && !verification.statuses.has(o.path),
    );
  });
  const needsAck = $derived(unverifiedOutputs.length > 0 && !acknowledged);

  function close() {
    if (busy) return;
    ui.deleteTarget = null;
  }

  async function handleConfirm() {
    if (!target?.path) return;
    busy = true;
    const deletedPath = target.path;
    // Snapshot the display name before clearing ui.deleteTarget below
    // — that assignment invalidates the $derived `target`, so reading
    // `target.name` afterwards would throw and get caught as a delete
    // failure even though the backend has already removed the file.
    const deletedName = target.name ?? deletedPath;
    try {
      await api.deleteFile(deletedPath);
      // Drop the now-deleted path from the selection set so the
      // selection bar / conversion panel doesn't keep an invisible
      // entry around that the next batch submit would forward to the
      // backend. Same idea as clearing after a bulk delete.
      fileBrowser.selectedFiles.delete(deletedPath);
      // Invalidate the verification record too. The backend prunes
      // it server-side; mirror that so a new file later created at
      // the same path (e.g. a re-converted output) doesn't inherit a
      // stale OK badge from the deleted predecessor during this
      // session.
      verification.statuses.delete(deletedPath);
      // Close + report success immediately so a downstream refresh
      // failure doesn't surface as "Failed to delete" after the delete
      // already happened server-side.
      ui.deleteTarget = null;
      toast.success(`Deleted: ${deletedName}`);
      try {
        await fileBrowser.refresh({ force: true });
      } catch (_e) {
        toast.warning('Deleted; refreshing the listing failed');
      }
    } catch (e) {
      toast.error(e?.message ?? 'Failed to delete');
    } finally {
      busy = false;
    }
  }
</script>

<ConfirmModal
  {open}
  onClose={close}
  onConfirm={handleConfirm}
  title="Delete file?"
  description={target ? `Permanently delete ${target.name ?? target.path}? This cannot be undone.` : ''}
  confirmLabel="Delete"
  cancelLabel="Keep"
  confirmVariant="destructive"
  {busy}
  confirmDisabled={needsAck}
>
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    {#if unverifiedOutputs.length > 0}
      <div class="dm-warn" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          <strong>Unverified output{unverifiedOutputs.length === 1 ? '' : 's'} present.</strong>
          Deleting this source removes the original before the
          replacement has been confirmed correct. Verify the output
          first, or acknowledge the risk to proceed.
          <ul class="dm-warn-list">
            {#each unverifiedOutputs as o (o.path)}
              <li title={o.path}>{o.tool_id ?? '?'}: {o.path}</li>
            {/each}
          </ul>
          <label class="dm-ack">
            <input type="checkbox" bind:checked={acknowledged} />
            <span>I understand and want to delete anyway.</span>
          </label>
        </div>
      </div>
    {/if}
  {/snippet}
</ConfirmModal>

<style>
  :global(.dm-warn) {
    display: flex;
    gap: var(--space-2);
    background: var(--warning-muted);
    color: var(--warning);
    border-radius: var(--radius-md);
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
    align-items: flex-start;
  }
  :global(.dm-warn strong) { font-weight: var(--weight-semibold); }
  :global(.dm-warn-list) {
    margin: var(--space-1) 0;
    padding-left: var(--space-4);
    font-size: var(--text-xs);
    font-family: var(--font-mono);
  }
  :global(.dm-ack) {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-1);
    cursor: pointer;
  }
</style>
