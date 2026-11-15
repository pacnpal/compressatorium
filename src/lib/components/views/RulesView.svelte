<script>
  /**
   * The Rules view (#303): one Library's Rules, picked from every Library.
   *
   * RomM is always listed; each folder registered as a Library (from the file
   * browser's row menu) is listed after it. The editor below is the same for
   * every Library and reads what the Library allows from the backend, so a
   * folder Library's Rules offer the skip policy only.
   *
   * `embedded` is the RomM view's Automation tab: it opens on RomM, and the
   * RomM view loads RomM's platforms and Rules itself.
   */
  import { onMount, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { libraries, ROMM_LIBRARY_ID } from '$lib/stores/libraries.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import RuleEditor from '$lib/components/views/RuleEditor.svelte';
  import BaseModal from '$lib/components/modals/BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import FolderCog from '@lucide/svelte/icons/folder-cog';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  let { embedded = false } = $props();

  const selectedId = $derived(libraries.selectedId);
  const selected = $derived(libraries.selected);
  const source = $derived(libraries.sourceFor(selectedId));
  const isFolder = $derived(selected?.kind === 'folder');

  const options = $derived(
    (libraries.list.length ? libraries.list : [{ id: ROMM_LIBRARY_ID, kind: 'romm' }]).map(
      (lib) => ({
        value: lib.id,
        label: lib.kind === 'folder' ? `${lib.name} (folder)` : 'RomM',
      }),
    ),
  );

  // Set when this view (not the RomM view) loaded RomM's platforms, which puts
  // the file browser in RomM mode; leaving undoes it, as the RomM view does.
  let enteredRomm = false;

  async function loadRomm() {
    await Promise.all([romm.loadStatus(), romm.loadSettings()]);
    if (!romm.usable) return;
    enteredRomm = true;
    await Promise.allSettled([romm.loadPlatforms(), romm.loadRules()]);
  }

  onMount(() => {
    // `#/rules/<library id>` opens on that Library (kept if it still exists).
    if (embedded) libraries.select(ROMM_LIBRARY_ID);
    else if (ui.rulesTarget) libraries.select(ui.rulesTarget);
    libraries.load();
    return () => {
      if (!enteredRomm) return;
      romm.cancelPlatformLoad();
      fileBrowser.exitRomm();
    };
  });

  // Load whichever Library is picked. Untracked: the loads read and write the
  // source's own state, which must not re-run this.
  $effect(() => {
    const id = selectedId;
    const src = source;
    if (!src) return;
    untrack(() => {
      if (id === ROMM_LIBRARY_ID) {
        if (!embedded) loadRomm();
        return;
      }
      src.loadPlatforms();
      src.loadRules().catch(() => {});
    });
  });

  // Inside the RomM view, picking another Library opens the Rules view on it
  // rather than showing a folder's Rules under the RomM header.
  function pick(id) {
    if (embedded && id !== ROMM_LIBRARY_ID) libraries.open(id);
    else libraries.select(id);
  }

  let confirmRemove = $state(false);
  let removing = $state(false);

  async function remove() {
    const lib = selected;
    if (!lib) return;
    removing = true;
    try {
      await libraries.remove(lib.id);
      toast.success(`${lib.name} is no longer a Library. No file was touched.`);
      confirmRemove = false;
    } catch (e) {
      toast.error(e?.message ?? 'Could not remove the Library');
    } finally {
      removing = false;
    }
  }
</script>

<div class="rules-view" class:embedded>
  <header class="head">
    {#if !embedded}
      <div class="title">
        <h1>Rules</h1>
        <p class="muted">
          A Rule converts the Images of one Platform in one Library, on a schedule
          or when you press Run now.
        </p>
      </div>
    {/if}
    <div class="picker">
      <Select
        label="Library"
        value={selectedId}
        {options}
        onchange={pick}
      />
    </div>
  </header>

  {#if libraries.error}
    <div class="notice error" role="alert">Could not list the Libraries: {libraries.error}</div>
  {/if}

  {#if !embedded && libraries.folders.length === 0 && !libraries.loading}
    <p class="hint muted">
      <FolderCog size={14} aria-hidden="true" />
      To add a folder, open it in the Workspace and pick <strong>Register as a Library</strong>
      from its row menu.
    </p>
  {/if}

  {#if isFolder && source}
    <section class="library">
      <div class="library-text">
        <strong>{selected.name}</strong>
        <code class="root">{selected.root}</code>
        {#if source.platformsLoading}
          <span class="muted"><Spinner /> Looking for Platforms…</span>
        {:else if source.platformsBusy}
          <span class="muted busy">
            Listing still running, try again shortly.
            <Button variant="ghost" onclick={() => source.loadPlatforms()}>Retry</Button>
          </span>
        {:else if source.platformsError}
          <span class="error">{source.platformsError}</span>
        {:else}
          <span class="muted counts">
            {source.platforms.length} Platform{source.platforms.length === 1 ? '' : 's'}
            · {source.counts.unsorted} Unsorted
            · {source.counts.conflicted} Conflicted
            {#if source.counts.unreadable}· {source.counts.unreadable} unreadable{/if}
          </span>
          <span class="muted note">
            Unsorted and Conflicted Images are never converted by a Rule. Folder
            Rules skip an Image whose output already exists.
          </span>
        {/if}
      </div>
      <Button variant="ghost" onclick={() => (confirmRemove = true)} icon={trashIcon}>
        Remove Library
      </Button>
    </section>
  {/if}

  {#if source}
    {#key selectedId}
      <RuleEditor {source} />
    {/key}
  {/if}
</div>

<BaseModal
  open={confirmRemove}
  onClose={() => (confirmRemove = false)}
  title="Remove this Library?"
  description={selected
    ? `${selected.name} and its Rules are removed. The folder and every file in it stay as they are.`
    : ''}
  size="sm"
>
  {#snippet footer()}
    <Button variant="secondary" onclick={() => (confirmRemove = false)}>Cancel</Button>
    <Button variant="destructive" onclick={remove} loading={removing}>Remove</Button>
  {/snippet}
</BaseModal>

{#snippet trashIcon()}<Trash2 size={14} />{/snippet}

<style>
  .rules-view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4, 16px);
    padding: var(--space-6, 24px);
    max-width: 1100px;
    margin: 0 auto;
    width: 100%;
  }
  .rules-view.embedded {
    padding: 0;
    max-width: none;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--space-4, 16px);
  }
  .title h1 {
    margin: 0 0 4px;
    font-size: var(--text-xl, 1.25rem);
  }
  .title p { margin: 0; }
  .picker { min-width: 240px; }
  .muted { color: var(--text-3); }
  .error { color: var(--danger, #d33); }
  .notice.error {
    padding: 8px 12px;
    border: 1px solid var(--danger, #d33);
    border-radius: var(--radius-sm);
  }
  .hint {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0;
  }
  .library {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3, 12px);
    padding: 12px 16px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md, 8px);
  }
  .library-text {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .root {
    font-size: var(--text-sm, 0.85rem);
    overflow-wrap: anywhere;
  }
  .note { font-size: var(--text-sm, 0.85rem); }
</style>
