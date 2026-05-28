<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { toast } from 'svelte-sonner';
  import ConfirmModal from './ConfirmModal.svelte';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  const open = $derived(!!ui.deleteTarget);
  const target = $derived(ui.deleteTarget);
  let busy = $state(false);

  function close() {
    if (busy) return;
    ui.deleteTarget = null;
  }

  async function handleConfirm() {
    if (!target?.path) return;
    busy = true;
    const deletedPath = target.path;
    try {
      await api.deleteFile(deletedPath);
      // Drop the now-deleted path from the selection set so the
      // selection bar / conversion panel doesn't keep an invisible
      // entry around that the next batch submit would forward to the
      // backend. Same idea as clearing after a bulk delete.
      fileBrowser.selectedFiles.delete(deletedPath);
      // Close + report success immediately so a downstream refresh
      // failure doesn't surface as "Failed to delete" after the delete
      // already happened server-side.
      ui.deleteTarget = null;
      toast.success(`Deleted: ${target.name ?? deletedPath}`);
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
>
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
</ConfirmModal>
