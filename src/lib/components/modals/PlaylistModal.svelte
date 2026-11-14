<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { canCreate } from '$lib/util/playlistDialog.js';
  import { toast } from 'svelte-sonner';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import ListMusic from '@lucide/svelte/icons/list-music';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  const paths = $derived(ui.playlistPaths ?? []);
  const open = $derived(paths.length > 0);

  let preview = $state(null);
  // The title `preview` was made for. Create is only allowed while the shown
  // title equals it (typing runs ahead of the debounced preview).
  let previewedTitle = $state('');
  // Selection refused before any preview exists: only Cancel makes sense.
  let selectionError = $state('');
  // The typed name was refused: shown under the input, which stays editable.
  let titleError = $state('');
  let title = $state('');
  let loading = $state(false);
  let busy = $state(false);
  // Drops the answer of a preview that a newer one has replaced.
  let token = 0;
  let timer;

  async function load(requestedTitle) {
    const mine = ++token;
    loading = true;
    try {
      const result = await api.playlistPreview(paths, requestedTitle);
      if (mine !== token) return;
      preview = result;
      selectionError = '';
      titleError = '';
      if (requestedTitle == null) title = result.title;
      previewedTitle = requestedTitle ?? result.title;
    } catch (e) {
      if (mine !== token) return;
      const message = e?.message ?? 'Failed to preview the playlist';
      // A refused name keeps the field and the last good preview; a refused
      // selection has nothing to edit.
      if (requestedTitle == null) {
        preview = null;
        selectionError = message;
      } else {
        titleError = message;
      }
    } finally {
      if (mine === token) loading = false;
    }
  }

  // Preview on open: the disc names choose the title.
  $effect(() => {
    if (open) {
      preview = null;
      previewedTitle = '';
      selectionError = '';
      titleError = '';
      title = '';
      load(null);
    } else {
      token++;
      clearTimeout(timer);
    }
  });

  const createEnabled = $derived(
    canCreate({ preview, title, previewedTitle, loading, busy, titleError }),
  );

  function onTitleInput() {
    clearTimeout(timer);
    titleError = '';
    timer = setTimeout(() => load(title), 300);
  }

  function close() {
    if (busy) return;
    ui.playlistPaths = null;
  }

  function onTitleKeydown(e) {
    if (e.key === 'Enter' && createEnabled) {
      e.preventDefault();
      handleCreate();
    }
  }

  async function handleCreate() {
    if (!createEnabled) return;
    clearTimeout(timer);
    busy = true;
    try {
      const result = await api.createPlaylist(paths, preview.file_name);
      ui.playlistPaths = null;
      toast.success(`Created ${result.file_name}`);
      try {
        await fileBrowser.refresh({ force: true });
      } catch (_e) {
        toast.warning('Playlist created; refreshing the listing failed');
      }
    } catch (e) {
      if (e?.status === 409 && e.detail?.suggested_name) {
        // Taken since the preview. Nothing was written; show the free name.
        toast.warning(`${preview.file_name} already exists. Review the new name and create again.`);
        await load(title);
      } else {
        toast.error(e?.message ?? 'Failed to create the playlist');
      }
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  {open}
  onClose={close}
  title="Create .m3u playlist"
  description="Lists the selected discs in disc order. Nothing is written until you create it, and an existing playlist is never overwritten."
  size="md"
>
  {#snippet titleIcon()}<ListMusic size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    {#if selectionError}
      <div class="pl-warn" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>{selectionError}</div>
      </div>
    {:else if preview}
      <label class="pl-label">
        <span class="pl-text">Playlist name</span>
        <span class="pl-name">
          <input
            type="text"
            class="pl-input"
            bind:value={title}
            oninput={onTitleInput}
            onkeydown={onTitleKeydown}
            aria-invalid={titleError ? 'true' : undefined}
            autocomplete="off"
            spellcheck="false"
            aria-label="Playlist name"
          />
          <span class="pl-ext">.m3u</span>
        </span>
      </label>
      {#if titleError}
        <p class="pl-error" role="alert">{titleError}</p>
      {/if}
      <p class="pl-file" title={preview.directory}>
        Will be saved as <strong>{preview.file_name}</strong> in {preview.directory}
      </p>
      {#if preview.name_taken}
        <div class="pl-warn" role="status">
          <TriangleAlert size={14} aria-hidden="true" />
          <div>{preview.requested_name} already exists and is left alone. This one gets a different name.</div>
        </div>
      {/if}
      {#each preview.warnings as w (w)}
        <div class="pl-warn" role="status">
          <TriangleAlert size={14} aria-hidden="true" />
          <div>{w}</div>
        </div>
      {/each}
      <span class="pl-text">Entries ({preview.entries.length})</span>
      <ol class="pl-list" aria-label="Playlist entries">
        {#each preview.entries as entry (entry)}
          <li class="pl-row" title={entry}>{entry}</li>
        {/each}
      </ol>
    {:else}
      <p class="pl-file">Checking the selection…</p>
    {/if}
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={close} disabled={busy}>Cancel</Button>
    <Button
      variant="primary"
      onclick={handleCreate}
      disabled={!createEnabled}
      loading={busy}
    >
      Create playlist
    </Button>
  {/snippet}
</BaseModal>

<style>
  .pl-label { display: flex; flex-direction: column; gap: var(--space-1); }
  .pl-text {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  .pl-name { display: flex; align-items: center; gap: var(--space-1); }
  .pl-input {
    flex: 1;
    min-width: 0;
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
    font-family: var(--font-mono);
  }
  .pl-input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .pl-ext { font-family: var(--font-mono); font-size: var(--text-sm); color: var(--text-2); }
  .pl-error { margin: var(--space-1) 0 0; font-size: var(--text-xs); color: var(--error); }
  .pl-file {
    margin: var(--space-2) 0;
    font-size: var(--text-xs);
    color: var(--text-2);
    overflow-wrap: anywhere;
  }
  .pl-file strong { color: var(--text-1); font-family: var(--font-mono); }
  .pl-list {
    list-style: none;
    counter-reset: entry;
    margin: var(--space-1) 0 0;
    padding: 0;
    max-height: 240px;
    overflow-y: auto;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--surface-2);
  }
  .pl-row {
    counter-increment: entry;
    padding: 4px var(--space-2);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-2);
    border-bottom: 1px solid var(--border-subtle);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pl-row::before {
    content: counter(entry) '. ';
    color: var(--text-3);
  }
  .pl-row:last-child { border-bottom: none; }
  .pl-warn {
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
</style>
