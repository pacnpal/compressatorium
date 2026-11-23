<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { defaults } from '$lib/stores/defaults.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { formatSize } from '$lib/api/format.js';
  import { uncheckedCopies, deleteGate, splitPath, freedBytes } from '$lib/util/deleteSafety.js';
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

  // Sources whose new copy exists but hasn't passed a check: the delete
  // can't be sure about these. It refuses them unless the Settings opt-out
  // is on, and then asks as it used to (#593).
  const unverifiedReplacements = $derived(
    uncheckedCopies(entries, (p) => verification.statuses.has(p)),
  );
  const gate = $derived(deleteGate({
    unchecked: unverifiedReplacements.length,
    allowUnchecked: defaults.values.allow_unchecked_delete,
    acknowledged,
  }));
  const freed = $derived(freedBytes(entries));

  // Refused: check the copies instead, so the delete can go ahead after.
  function checkCopies() {
    const copies = unverifiedReplacements.flatMap((r) => r.outputs);
    ui.bulkDeleteEntries = null;
    ui.bulkVerifyItems = copies;
  }

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
      for (const p of removed) verification.forget(p);
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
  description={`Permanent: the files are removed from disk and can't be recovered.${freed ? ` Frees ${formatSize(freed)}.` : ''}`}
  size="md"
>
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    {#if gate.state !== 'clear'}
      {@const n = unverifiedReplacements.length}
      <div class="bd-warn" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          <strong>
            {n === 1 ? "1 original's new copy hasn't" : `${n} originals' new copies haven't`} passed a check.
          </strong>
          {#if gate.state === 'refuse'}
            Nothing is deleted until {n === 1 ? 'it does' : 'they do'}, so a bad conversion can't cost you the game.
          {:else}
            Deleting {n === 1 ? 'it' : 'them'} now loses the game if a conversion went wrong.
          {/if}
          <ul class="bd-warn-list">
            {#each unverifiedReplacements.slice(0, 6) as r (r.source)}
              <li title={r.source}>{splitPath(r.source).name}</li>
            {/each}
            {#if n > 6}
              <li>…and {n - 6} more</li>
            {/if}
          </ul>
          {#if gate.state === 'ask'}
            <label class="bd-ack">
              <input type="checkbox" bind:checked={acknowledged} />
              <span>I understand and want to delete anyway.</span>
            </label>
          {/if}
        </div>
      </div>
    {/if}
    <ul class="bd-list">
      {#each paths.slice(0, 20) as p (p)}
        {@const { name, folder } = splitPath(p)}
        <li class="bd-row" title={p}>
          <span class="bd-name">{name}</span>
          {#if folder}<span class="bd-folder"><bdi>{folder}</bdi></span>{/if}
        </li>
      {/each}
      {#if paths.length > 20}
        <li class="bd-more">…and {paths.length - 20} more</li>
      {/if}
    </ul>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={close} disabled={busy}>Keep</Button>
    {#if gate.state === 'refuse'}
      <Button variant="secondary" onclick={checkCopies} disabled={busy}>Check the copies</Button>
    {/if}
    <Button variant="destructive" onclick={handleConfirm} disabled={busy || paths.length === 0 || !gate.canDelete} loading={busy}>
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
    display: flex;
    flex-direction: column;
    padding: var(--space-1) var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    min-width: 0;
  }
  .bd-name {
    font-size: var(--text-sm);
    color: var(--text-1);
    overflow-wrap: anywhere;
  }
  /* The folder, muted, cut from the start so the part nearest the file
     stays. */
  .bd-folder {
    font-size: var(--text-xs);
    color: var(--text-3);
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    direction: rtl;
    text-align: left;
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
    color: var(--warning-text);
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
