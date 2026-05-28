<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { toast } from 'svelte-sonner';
  import ConfirmModal from './ConfirmModal.svelte';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  const open = $derived(ui.showClearDone);
  const total = $derived(jobs.completedCount + jobs.failedCount + jobs.cancelledCount);

  function close() { ui.showClearDone = false; }

  async function handleConfirm() {
    try {
      const r = await jobs.clearCompleted();
      toast.success(`Removed ${r?.removed_count ?? total} job(s) from history`);
      close();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to clear completed jobs');
    }
  }
</script>

<ConfirmModal
  {open}
  onClose={close}
  onConfirm={handleConfirm}
  title="Clear completed jobs?"
  description={total === 0 ? 'No completed, failed, or cancelled jobs to clear.' : `Remove ${total} completed, failed, and cancelled job(s) from the history. Output files are not affected.`}
  confirmLabel="Clear"
  cancelLabel="Keep"
  confirmVariant="primary"
  busy={jobs.clearingCompleted}
>
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
</ConfirmModal>
