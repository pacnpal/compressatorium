<script>
  /**
   * A Library's watch folders (#431). New games dropped in one are sorted
   * into this Library once they stop growing: converted under their
   * console's settings into "Where new games go", the original moved to
   * `processed/` inside the watch folder. Nothing is deleted. The folder is
   * picked from the Drives, never typed; the status re-reads every 15 s.
   *
   * @typedef {Object} Props
   * @property {object} library the Library row (`{ id, name }`)
   * @property {import('svelte').Snippet} [after] under the rows (the *arr hook)
   */
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import { LatestOnly } from '$lib/util/latestOnly.js';
  import { drivePath } from '$lib/util/drivePath.js';
  import FolderPickerModal from '$lib/components/modals/FolderPickerModal.svelte';
  import BaseModal from '$lib/components/modals/BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import FolderPlus from '@lucide/svelte/icons/folder-plus';

  /** @type {Props} */
  let { library, after } = $props();

  const REFRESH_MS = 15_000;
  // The intake waits this long for a file to stop growing.
  const SETTLE_SECONDS = 60;

  let inboxes = $state([]);
  let loading = $state(false);
  let error = $state(null);
  let adding = $state(false);
  let addError = $state('');
  let confirming = $state(null);
  let removing = $state(false);

  const latest = new LatestOnly();
  const libraryId = $derived(library?.id);

  async function load(id) {
    if (!id) return;
    loading = true;
    const result = await latest.run(() => api.listInboxes(id));
    if (!result.current) return;
    loading = false;
    if (result.error) {
      error = result.error?.message ?? "Couldn't load watch folders.";
      return;
    }
    error = null;
    inboxes = Array.isArray(result.value?.inboxes) ? result.value.inboxes : [];
  }

  // Again every 15 s while the tab is showing; a hidden tab waits.
  $effect(() => {
    const id = libraryId;
    if (!id) return;
    inboxes = [];
    load(id);
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') load(id);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });

  $effect(() => {
    if (!fileBrowser.volumes.length && !fileBrowser.volumesLoading) fileBrowser.fetchVolumes();
  });

  async function add(path) {
    const id = libraryId;
    addError = '';
    try {
      await api.createInbox(id, path, SETTLE_SECONDS);
      adding = false;
      toast.success('Watching that folder', {
        description: 'New games there are sorted in once they finish downloading.',
      });
      if (libraryId === id) await load(id);
    } catch (e) {
      addError = e?.message ?? "Couldn't watch that folder";
    }
  }

  async function remove() {
    const row = confirming;
    if (!row) return;
    removing = true;
    try {
      await api.removeInbox(libraryId, row.id);
      toast.success('No longer watching that folder. Nothing on disk was touched.');
      confirming = null;
      await load(libraryId);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't stop watching it");
    } finally {
      removing = false;
    }
  }

  const where = (folder) => drivePath(folder, fileBrowser.volumes);
  const lastName = (folder) => String(folder ?? '').split('/').filter(Boolean).pop() ?? folder;

  function ago(iso) {
    if (!iso) return 'not yet';
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return 'not yet';
    const minutes = Math.round((Date.now() - then) / 60_000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    return hours < 24 ? `${hours} h ago` : new Date(iso).toLocaleDateString();
  }
</script>

<section class="watch" aria-labelledby="watch-title">
  <div class="text">
    <h2 id="watch-title">Watch folders</h2>
    <p class="muted">New games dropped here are sorted into this Library and converted.</p>
  </div>

  {#if loading && inboxes.length === 0}
    <p class="muted"><Spinner size="sm" label="Loading" /> Loading watch folders…</p>
  {:else if error}
    <p class="state">
      <span>Couldn't load watch folders. {error}</span>
      <Button size="sm" variant="secondary" onclick={() => load(libraryId)}>Try again</Button>
    </p>
  {:else if inboxes.length === 0}
    <p class="muted">
      No watch folders. Point one at your downloads folder and new games sort themselves.
    </p>
  {:else}
    <ul class="rows">
      {#each inboxes as row (row.id)}
        <li data-folder={row.folder}>
          <div class="where">
            <code>{where(row.folder)}</code>
            <span class="status muted">
              Checked {ago(row.last_poll)}
              {#if row.waiting} · {row.waiting} still downloading{/if}
              {#if row.in_progress} · {row.in_progress} being sorted in{/if}
              {#if row.held}
                · <a href={hashFor('activity', { tab: 'attention', id: libraryId })}>{row.held} need you →</a>
              {/if}
              · {row.processed} taken in
            </span>
            {#if row.error}<span class="error">{row.error}</span>{/if}
          </div>
          <Button
            variant="ghost" size="sm"
            ariaLabel={`Stop watching ${row.folder}`}
            onclick={() => (confirming = row)}
          >
            Remove
          </Button>
        </li>
      {/each}
    </ul>
  {/if}

  <div>
    <Button variant="secondary" icon={addIcon} onclick={() => { addError = ''; adding = true; }}>
      Add a watch folder
    </Button>
  </div>

  {#if after}{@render after()}{/if}
</section>

<FolderPickerModal
  open={adding}
  title="Pick a folder to watch"
  confirmLabel="Watch this folder"
  onClose={() => (adding = false)}
  onpick={add}
  error={addError}
/>

<BaseModal
  open={!!confirming}
  onClose={() => (confirming = null)}
  title={`Stop watching ${lastName(confirming?.folder)}?`}
  description="Nothing on disk is touched."
  size="sm"
>
  {#snippet footer()}
    <Button variant="secondary" onclick={() => (confirming = null)}>Cancel</Button>
    <Button variant="destructive" onclick={remove} loading={removing}>Remove</Button>
  {/snippet}
</BaseModal>

{#snippet addIcon()}<FolderPlus size={14} />{/snippet}

<style>
  .watch { display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
  .text h2 { margin: 0 0 var(--space-1); font-size: var(--text-lg); font-weight: var(--weight-semibold); }
  .text p { margin: 0; }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .rows li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-3);
    padding: var(--space-2) 0;
    border-top: 1px solid var(--border-subtle);
    min-width: 0;
  }
  .where { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; flex: 1 1 240px; }
  .where code { overflow-wrap: anywhere; font-size: var(--text-sm); }
  .status a { color: var(--accent); }
  .state { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin: 0; }
  .muted { color: var(--text-2); font-size: var(--text-sm); margin: 0; }
  .error { color: var(--error); font-size: var(--text-sm); }
</style>
