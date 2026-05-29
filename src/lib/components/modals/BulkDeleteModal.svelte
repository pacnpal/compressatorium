<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { toast } from 'svelte-sonner';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  const open = $derived(!!ui.bulkDeleteEntries);
  const entries = $derived(ui.bulkDeleteEntries ?? []);
  const paths = $derived(entries.map((e) => (typeof e === 'string' ? e : e?.path)).filter(Boolean));
  let busy = $state(false);
  // Two-step confirmation: when the selection includes sources whose
  // declared outputs exist but haven't been verified yet, the legacy
  // bulk-delete flow paused to surface that risk before destruction.
  // Reset whenever the modal opens with a new selection.
  let acknowledged = $state(false);
  $effect(() => {
    if (open) acknowledged = false;
  });

  // Sources with unverified replacement outputs. Each entry in the
  // selection may carry an `outputs` array from /api/files — items
  // with `exists: true` and a path NOT in verification.statuses are
  // the risk surface for permanently removing the original before the
  // new output has been confirmed correct.
  const unverifiedReplacements = $derived.by(() => {
    const out = [];
    for (const e of entries) {
      const path = typeof e === 'string' ? null : e?.path;
      const outputs = typeof e === 'object' && Array.isArray(e?.outputs) ? e.outputs : [];
      const risky = outputs.filter(
        (o) => o?.exists && o.path && !verification.statuses.has(o.path),
      );
      if (path && risky.length > 0) {
        out.push({ source: path, outputs: risky.map((o) => o.path) });
      }
    }
    return out;
  });
  const needsAck = $derived(unverifiedReplacements.length > 0 && !acknowledged);

  function close() {
    if (busy) return;
    ui.bulkDeleteEntries = null;
  }

  async function handleConfirm() {
    if (paths.length === 0) return;
    busy = true;
    try {
      const result = await api.deleteBatch(paths);
      // Backend (app/routes/files.py:delete_files_batch) returns
      // { total, success, failed, results }. `deleted` doesn't exist,
      // so falling back to paths.length on a partial-success would
      // wrongly toast the request count as successes.
      const success = typeof result?.success === 'number'
        ? result.success
        : paths.length - (result?.failed ?? 0);
      const failed = result?.failed ?? 0;
      // Invalidate verification records for paths the backend reported
      // as successfully removed. If `results` is missing (older shape),
      // fall back to invalidating every requested path. The backend
      // also prunes server-side; this mirror prevents a new file later
      // created at the same path during this session from inheriting a
      // stale OK badge.
      const removed = Array.isArray(result?.results)
        ? result.results.filter((r) => r?.success).map((r) => r.path)
        : paths;
      for (const p of removed) verification.statuses.delete(p);
      // Close + drop selection + report immediately so a downstream
      // refresh failure doesn't surface as "Failed to delete" after
      // the backend already removed the files.
      ui.bulkDeleteEntries = null;
      fileBrowser.clearSelection();
      if (failed > 0) {
        toast.warning(`Deleted ${success}; ${failed} failed`);
      } else {
        toast.success(`Deleted ${success} file${success === 1 ? '' : 's'}`);
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
    {#if unverifiedReplacements.length > 0}
      <div class="bd-warn" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          <strong>{unverifiedReplacements.length} source file{unverifiedReplacements.length === 1 ? ' has' : 's have'} an unverified output.</strong>
          Their replacement files have not been verified yet, so deleting the originals now risks losing data if a conversion was corrupted. Verify the outputs first, or acknowledge the risk to proceed.
          <ul class="bd-warn-list">
            {#each unverifiedReplacements.slice(0, 6) as r (r.source)}
              <li title={r.source}>{r.source}</li>
            {/each}
            {#if unverifiedReplacements.length > 6}
              <li>…and {unverifiedReplacements.length - 6} more</li>
            {/if}
          </ul>
          <label class="bd-ack">
            <input type="checkbox" bind:checked={acknowledged} />
            <span>I understand and want to delete anyway.</span>
          </label>
        </div>
      </div>
    {/if}
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
    <Button variant="destructive" onclick={handleConfirm} disabled={busy || paths.length === 0 || needsAck} loading={busy}>
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
  .bd-warn {
    display: flex;
    gap: var(--space-2);
    background: var(--warning-muted);
    color: var(--warning);
    border-radius: var(--radius-md);
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
    align-items: flex-start;
    margin-bottom: var(--space-2);
  }
  .bd-warn strong { font-weight: var(--weight-semibold); }
  .bd-warn-list { margin: var(--space-1) 0; padding-left: var(--space-4); font-size: var(--text-xs); font-family: var(--font-mono); }
  .bd-ack {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-1);
    cursor: pointer;
  }
</style>
