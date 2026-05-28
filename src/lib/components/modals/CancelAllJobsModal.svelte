<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { toast } from 'svelte-sonner';
  import ConfirmModal from './ConfirmModal.svelte';
  import CircleX from '@lucide/svelte/icons/circle-x';

  const open = $derived(ui.showCancelAll);
  const queuedCount = $derived(jobs.queuedCount + jobs.processingCount);

  function close() { ui.showCancelAll = false; }

  async function handleConfirm() {
    try {
      const r = await jobs.cancelAll();
      toast.success(`Cancelled ${r?.cancelled_count ?? queuedCount} job(s)`);
      close();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to cancel all jobs');
    }
  }
</script>

<ConfirmModal
  {open}
  onClose={close}
  onConfirm={handleConfirm}
  title="Cancel all jobs?"
  description={queuedCount === 0 ? 'No active jobs to cancel.' : `This will cancel ${queuedCount} queued and processing job(s). Already-completed jobs are unaffected.`}
  confirmLabel="Cancel all"
  cancelLabel="Keep running"
  confirmVariant="destructive"
  busy={jobs.cancellingAll}
>
  {#snippet titleIcon()}<CircleX size={18} aria-hidden="true" />{/snippet}
</ConfirmModal>
