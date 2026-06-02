<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { toast } from 'svelte-sonner';
  import ConfirmModal from './ConfirmModal.svelte';
  import CircleX from '@lucide/svelte/icons/circle-x';

  const open = $derived(ui.showCancelAll);
  // /api/jobs/cancel-all is a global op, it cancels every queued
  // and processing job server-side, including hidden metadata-scan
  // and dat-match background work. Use the unfiltered totals here
  // (and call out hidden ones in the description) so the user isn't
  // surprised when a background scan also gets cancelled.
  const visibleQueued = $derived(jobs.visibleQueuedCount);
  const totalQueued = $derived(jobs.queuedCount + jobs.processingCount);
  const hiddenCount = $derived(Math.max(0, totalQueued - visibleQueued));

  function close() { ui.showCancelAll = false; }

  async function handleConfirm() {
    // Snapshot the pre-cancel count for the toast fallback, the
    // SSE-driven jobs.queuedCount/processingCount can drop to 0
    // before the toast evaluates if the cancellation races through.
    const pending = totalQueued;
    try {
      const r = await jobs.cancelAll();
      // Backend /api/jobs/cancel-all returns
      // { requested, queued, processing, ... }. Not cancelled_count.
      const cancelled = typeof r?.requested === 'number' ? r.requested : pending;
      toast.success(`Cancelled ${cancelled} job(s)`);
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
  description={totalQueued === 0
    ? 'No active jobs to cancel.'
    : hiddenCount > 0
      ? `This will cancel all ${totalQueued} queued and processing job(s), including ${hiddenCount} hidden metadata/DAT background job(s). Already-completed jobs are unaffected.`
      : `This will cancel ${totalQueued} queued and processing job(s). Already-completed jobs are unaffected.`}
  confirmLabel="Cancel all"
  cancelLabel="Keep running"
  confirmVariant="destructive"
  busy={jobs.cancellingAll}
>
  {#snippet titleIcon()}<CircleX size={18} aria-hidden="true" />{/snippet}
</ConfirmModal>
