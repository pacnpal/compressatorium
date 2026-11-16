<script>
  /**
   * Delete the other copies of a duplicate group (#389): the confirmation list.
   *
   * Opened on a group with its suggested keeper picked. The backend's plan
   * lists every file each other copy's delete would remove (a sheet's tracks,
   * a split set's parts) and the playlists that would be pointed at the copy
   * kept. Picking another keeper plans again. Confirm deletes exactly what the
   * list shows; a copy that changed since, or a plan that no longer matches,
   * is refused by the backend and nothing of it is deleted.
   */
  import { findings, findingName } from '$lib/stores/findings.svelte.js';
  import { summarizeMessages } from '$lib/util/messages.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  /** @type {{ onDone: (result: object | null) => void }} */
  let { onDone } = $props();

  const open = $derived(findings.cleanup);
  const plan = $derived(open?.plan ?? null);
  const items = $derived(Array.isArray(plan?.items) ? plan.items : []);
  const copies = $derived(open?.row?.cause?.copies ?? []);
  const total = $derived(plan?.total_delete_count ?? 0);
  const blocked = $derived(!!plan?.blocked);

  const blocking = $derived.by(() => {
    const out = [];
    for (const item of items) {
      for (const m of item.errors ?? []) out.push(m);
      for (const m of item.unsafe_paths ?? []) out.push(`Unsafe: ${m}`);
      for (const m of item.missing_paths ?? []) out.push(`Missing: ${m}`);
    }
    return summarizeMessages(out);
  });

  function shortName(p) {
    return p?.split(/[/\\]/).pop() ?? p ?? '';
  }

  function pickKeeper(event) {
    if (open?.row) findings.openCleanup(open.row, event.currentTarget.value);
  }

  async function confirm() {
    try {
      onDone(await findings.confirmCleanup());
    } catch {
      // The dialog shows the error and stays open; the list was read again.
    }
  }
</script>

<BaseModal
  open={!!open}
  onClose={() => findings.closeCleanup()}
  title="Delete the other copies?"
  size="md"
>
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    {#if open}
      <label class="dd-keep">
        <span>Keep</span>
        <select value={open.keep} onchange={pickKeeper} aria-label="The copy to keep">
          {#each copies as copy (copy.path)}
            <option value={copy.path}>{copy.path}</option>
          {/each}
        </select>
      </label>
      {#if open.keep === open.row.keeper && open.row.keeper_reason}
        <p class="dd-note">Suggested: {open.row.keeper_reason}.</p>
      {/if}

      {#if open.deleting}
        <!-- The copy to keep is read in full under its lock first (#389). -->
        <p class="dd-note"><Spinner /> Checking the copy to keep (reading it in full), then deleting…</p>
      {/if}
      {#if open.error}
        <div class="dd-block" role="alert">
          <TriangleAlert size={14} aria-hidden="true" />
          <div>{open.error}</div>
        </div>
      {:else if !plan}
        <p class="dd-note"><Spinner /> Checking every copy…</p>
      {:else}
        <p class="dd-lead">
          <Badge tone={blocked ? 'error' : 'warning'}>{total}</Badge>
          file{total === 1 ? '' : 's'} of {findingName(open.row)} will be deleted.
          Every copy holds the same disc (equal content hashes). This cannot be undone.
        </p>
        {#if blocked}
          <div class="dd-block" role="alert">
            <TriangleAlert size={14} aria-hidden="true" />
            <div>
              <strong>Blocked.</strong>
              <ul>
                {#each blocking.slice(0, 8) as m (m.key)}<li>{m.text}</li>{/each}
              </ul>
            </div>
          </div>
        {/if}
        <ul class="dd-list" aria-label="Files to delete">
          {#each items as item (item.source_path)}
            <li>
              <code title={item.source_path}>{item.source_path}</code>
              {#each item.delete_paths ?? [] as p (p)}
                {#if p !== item.source_path}
                  <span class="dd-extra" title={p}>+ {shortName(p)}</span>
                {/if}
              {/each}
              {#each item.referrer_changes ?? [] as change (change.target)}
                <span class="dd-extra" title={change.target}>
                  updates {shortName(change.target)} to name the copy kept
                </span>
              {/each}
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={() => findings.closeCleanup()}>Cancel</Button>
    <Button
      variant="destructive"
      disabled={!plan || blocked || !!open?.error}
      loading={!!open?.deleting}
      onclick={confirm}
    >
      Delete {total} file{total === 1 ? '' : 's'}
    </Button>
  {/snippet}
</BaseModal>

<style>
  .dd-keep { display: flex; flex-direction: column; gap: 4px; font-size: var(--text-sm); color: var(--text-2); }
  .dd-keep select { max-width: 100%; font-family: var(--font-mono); font-size: var(--text-xs); }
  .dd-lead { margin: 0; font-size: var(--text-sm); color: var(--text-1); display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .dd-note { margin: 0; font-size: var(--text-xs); color: var(--text-2); }
  .dd-block { display: flex; gap: var(--space-2); background: var(--error-muted); color: var(--error); border-radius: var(--radius-md); padding: var(--space-2) var(--space-3); font-size: var(--text-sm); align-items: flex-start; }
  .dd-block ul { margin: var(--space-1) 0 0; padding-left: var(--space-4); font-size: var(--text-xs); }
  .dd-list { list-style: none; margin: 0; padding: 0; max-height: 280px; overflow-y: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2); }
  .dd-list li { padding: 6px var(--space-2); border-bottom: 1px solid var(--border-subtle); display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .dd-list li:last-child { border-bottom: none; }
  .dd-list code { font-size: var(--text-xs); overflow-wrap: anywhere; color: var(--text-1); }
  .dd-extra { font-size: var(--text-xs); color: var(--text-3); font-family: var(--font-mono); overflow-wrap: anywhere; }
</style>
