<script>
  /**
   * Where new Images go in one Library (#430).
   *
   * A folder Library picks its layout here (RomM style, ES-DE/Batocera style
   * or flat). Every Library lists where new Images of each of its Platforms
   * would land ("PlayStation 2 → roms/ps2"), whether that folder is there yet,
   * or why it can't be told. The backend works it out (`Library.placement`),
   * so the folder names here are the ones detection reads back.
   */
  import { untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import Select from '$lib/components/ui/Select.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';

  let { library } = $props();

  const LAYOUTS = [
    { value: 'esde', label: 'ES-DE / Batocera: <root>/<platform>' },
    { value: 'romm', label: 'RomM: <root>/roms/<platform>' },
    { value: 'flat', label: 'Flat: every Image in <root>' },
  ];

  const isFolder = $derived(library?.kind === 'folder');
  let placements = $state([]);
  let loading = $state(false);
  let error = $state(null);
  let saving = $state(false);

  async function load(id) {
    loading = true;
    error = null;
    try {
      const data = await api.getLibraryPlacements(id);
      if (library?.id !== id) return;
      placements = Array.isArray(data?.placements) ? data.placements : [];
    } catch (e) {
      if (library?.id !== id) return;
      error = e?.message ?? 'Could not read where Images go';
      placements = [];
    } finally {
      if (library?.id === id) loading = false;
    }
  }

  // Again whenever the Library or its layout changes. Primitives, so a fresh
  // copy of the same row (adopted after another setting's save) reloads nothing.
  const libraryId = $derived(library?.id);
  const layout = $derived(library?.layout);
  $effect(() => {
    const id = libraryId;
    void layout;
    if (id) untrack(() => load(id));
  });

  async function setLayout(layout) {
    if (!library || layout === library.layout) return;
    saving = true;
    try {
      await libraries.saveSettings(library.id, { layout });
      toast.success('Layout saved');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save the layout');
    } finally {
      saving = false;
    }
  }
</script>

<section class="placement">
  <div class="placement-text">
    <h2>Where new Images go</h2>
    <p class="muted">
      Where new Images of each Platform land, named the way Platform detection reads
      them. A folder that isn't there yet is created when the first Image arrives.
      {#if isFolder}
        Changing the layout moves nothing that's already here.
      {:else}
        RomM's own folder structure decides.
      {/if}
    </p>
    {#if isFolder}
      <div class="layout">
        <Select
          label="Layout"
          value={library.layout ?? 'esde'}
          options={LAYOUTS}
          disabled={saving}
          onchange={setLayout}
        />
      </div>
    {/if}
  </div>
  {#if loading && placements.length === 0}
    <p class="muted"><Spinner /> Working out the folders…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if placements.length === 0}
    <p class="muted">No Platforms yet.</p>
  {:else}
    <ul class="rows">
      {#each placements as row (row.platform)}
        <li>
          <span class="name">{row.name}</span>
          {#if row.error}
            <span class="error">can't tell: {row.error}</span>
          {:else}
            <span aria-hidden="true">→</span>
            <code>{row.folder || '(the root)'}</code>
            {#if !row.exists}<span class="muted new">will be created</span>{/if}
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .placement {
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    padding: 12px 16px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md, 8px);
  }
  .placement-text h2 {
    margin: 0 0 4px;
    font-size: var(--text-md, 1rem);
  }
  .placement-text p { margin: 0; }
  .layout { margin-top: var(--space-2, 8px); max-width: 360px; }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 4px var(--space-3, 12px);
  }
  .rows li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px;
    min-width: 0;
    font-size: var(--text-sm, 0.85rem);
  }
  .rows code { overflow-wrap: anywhere; }
  .name { font-weight: 600; }
  .new { font-style: italic; }
  .muted { color: var(--text-3); }
  .error { color: var(--danger, #d33); }
</style>
