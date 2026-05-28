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
    try {
      await api.deleteFile(target.path);
      toast.success(`Deleted: ${target.name ?? target.path}`);
      // Reflect the deletion in the listing without waiting for a manual
      // refresh. forced refresh bypasses the auto-refresh-while-jobs-active
      // guard since this is an explicit user action.
      await fileBrowser.refresh({ force: true });
      ui.deleteTarget = null;
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
