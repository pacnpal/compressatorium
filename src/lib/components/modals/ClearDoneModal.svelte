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
    // Snapshot the pre-clear count for the toast fallback. jobs.clearCompleted
    // optimistically drops terminal jobs from the local store before the
    // request resolves, so `total` would re-derive to 0 by the time the
    // toast evaluates and we'd report "Removed 0 job(s)" on a real success.
    const pending = total;
    try {
      const r = await jobs.clearCompleted();
      // Backend /api/jobs/completed returns { deleted, count } — not
      // `removed_count`. Fall back to the pre-clear snapshot when the
      // count is missing for any reason.
      const removed = typeof r?.count === 'number' ? r.count : pending;
      toast.success(`Removed ${removed} job(s) from history`);
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
