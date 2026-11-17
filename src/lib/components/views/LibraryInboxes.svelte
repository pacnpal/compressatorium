<script>
  /**
   * A Library's Inboxes (#431).
   *
   * An Inbox is a folder whose new files are taken into this Library: once a
   * file stops growing, its Platform is worked out, it is converted under that
   * Platform's Rule (or copied as it is when there's none) into the folder
   * "Where new Images go" names, and the original moves to `processed/`
   * inside the Inbox. Nothing is deleted. Each Inbox shows its last poll, the
   * files waiting to settle, those in progress or held with a Finding, and how
   * many it has placed. The backend checks the folder; this only asks.
   */
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import { LatestOnly } from '$lib/util/latestOnly.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Inbox from '@lucide/svelte/icons/inbox';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';

  let { library } = $props();

  // The scheduler polls once a minute; the status is read again at that pace.
  const REFRESH_MS = 30_000;

  let inboxes = $state([]);
  let loading = $state(false);
  let error = $state(null);
  let folder = $state('');
  let settle = $state(60);
  let saving = $state(false);
  let removing = $state(null);

  const latest = new LatestOnly();
  const libraryId = $derived(library?.id);

  async function load(id) {
    if (!id) return;
    loading = true;
    const result = await latest.run(() => api.listInboxes(id));
    if (!result.current) return;
    loading = false;
    if (result.error) {
      error = result.error?.message ?? 'Could not list the Inboxes';
      return;
    }
    error = null;
    inboxes = Array.isArray(result.value?.inboxes) ? result.value.inboxes : [];
  }

  $effect(() => {
    const id = libraryId;
    if (!id) return;
    inboxes = [];
    load(id);
    const timer = setInterval(() => load(id), REFRESH_MS);
    return () => clearInterval(timer);
  });

  async function add(event) {
    event.preventDefault();
    const submitted = folder;
    const path = submitted.trim();
    const id = libraryId;
    if (!path || !id) return;
    saving = true;
    try {
      await api.createInbox(id, path, Number(settle) || 0);
      toast.success('Inbox saved', {
        description: 'New files there are taken in on the next poll after they settle.',
      });
      // Only the draft that was saved: one typed while the save was out, or
      // another Library picked meanwhile, keeps what's in the field.
      if (libraryId === id && folder === submitted) folder = '';
      if (libraryId === id) await load(id);
    } catch (e) {
      toast.error(e?.message ?? 'Could not save the Inbox');
    } finally {
      saving = false;
    }
  }

  async function remove(row) {
    removing = row.id;
    try {
      await api.removeInbox(libraryId, row.id);
      toast.success('Inbox removed. No file was touched.');
      await load(libraryId);
    } catch (e) {
      toast.error(e?.message ?? 'Could not remove the Inbox');
    } finally {
      removing = null;
    }
  }

  function when(iso) {
    if (!iso) return 'not yet';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
  }
</script>

<section class="inboxes" aria-labelledby="inboxes-title">
  <div class="text">
    <h2 id="inboxes-title">Inboxes</h2>
    <p class="muted">
      Files dropped in an Inbox are taken into {library?.name ?? 'this Library'} once
      they stop growing: converted under their Platform's Rule (or copied as they are
      when there's none) into the folder above, and the originals moved to
      <code>processed/</code> inside the Inbox. Nothing is deleted. A file that can't
      be placed stays where it is, with a Finding saying why.
    </p>
  </div>

  {#if loading && inboxes.length === 0}
    <p class="muted"><Spinner /> Reading the Inboxes…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if inboxes.length === 0}
    <p class="muted">No Inbox yet.</p>
  {:else}
    <ul class="rows">
      {#each inboxes as row (row.id)}
        <li>
          <div class="where">
            <code>{row.folder}</code>
            <span class="muted small">settles after {row.settle_seconds}s</span>
          </div>
          <dl class="status">
            <div><dt>Last poll</dt><dd>{when(row.last_poll)}</dd></div>
            <div><dt>Waiting</dt><dd>{row.waiting}</dd></div>
            <div><dt>In progress</dt><dd>{row.in_progress}</dd></div>
            <div><dt>Held</dt><dd>{row.held}</dd></div>
            <div><dt>Processed</dt><dd>{row.processed}</dd></div>
          </dl>
          {#if row.error}<p class="error small">{row.error}</p>{/if}
          <Button
            variant="ghost" size="sm" icon={trashIcon}
            loading={removing === row.id}
            ariaLabel={`Stop using ${row.folder} as an Inbox`}
            onclick={() => remove(row)}
          >
            Remove
          </Button>
        </li>
      {/each}
    </ul>
  {/if}

  <form class="add" onsubmit={add}>
    <label class="field grow">
      <span class="label">Inbox folder</span>
      <input
        type="text" bind:value={folder} placeholder="/volumes/downloads"
        autocomplete="off" spellcheck="false"
      />
    </label>
    <label class="field settle">
      <span class="label">Settle (seconds)</span>
      <input type="number" min="0" step="1" bind:value={settle} />
    </label>
    <div class="buttons">
      <Button type="submit" variant="secondary" icon={inboxIcon} loading={saving}
        disabled={!folder.trim()}>
        Add Inbox
      </Button>
      <Button variant="ghost" icon={refreshIcon} ariaLabel="Read the Inboxes again"
        onclick={() => load(libraryId)}>
        Refresh
      </Button>
    </div>
  </form>
</section>

{#snippet inboxIcon()}<Inbox size={14} />{/snippet}
{#snippet trashIcon()}<Trash2 size={14} />{/snippet}
{#snippet refreshIcon()}<RefreshCw size={14} />{/snippet}

<style>
  .inboxes {
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    padding: 12px 16px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md, 8px);
  }
  .text h2 {
    margin: 0 0 4px;
    font-size: var(--text-md, 1rem);
  }
  .text p { margin: 0; }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
  }
  .rows li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2, 8px) var(--space-3, 12px);
    padding: var(--space-2, 8px) 0;
    border-top: 1px solid var(--border-subtle, var(--border));
    min-width: 0;
  }
  .where {
    display: flex;
    flex-direction: column;
    min-width: 0;
    flex: 1 1 220px;
  }
  .where code { overflow-wrap: anywhere; }
  .status {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1, 4px) var(--space-3, 12px);
    margin: 0;
    font-size: var(--text-sm, 0.85rem);
  }
  .status div { display: flex; gap: 4px; }
  .status dt { color: var(--text-3); }
  .status dd { margin: 0; font-weight: 600; }
  .add {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-2, 8px);
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1, 4px);
    min-width: 0;
  }
  .grow { flex: 1 1 240px; }
  .settle { width: 140px; }
  .label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  .field input {
    background: var(--surface-1, var(--surface-2));
    border: 1px solid var(--border-subtle, var(--border));
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
  }
  .field input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .buttons { display: flex; gap: var(--space-2, 8px); }
  .muted { color: var(--text-3); }
  .small { font-size: var(--text-xs, 0.75rem); }
  .error { color: var(--danger, #d33); margin: 0; }
</style>
