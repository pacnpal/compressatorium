<script>
  /**
   * Where a Library puts each Platform's new Images (#430).
   *
   * A folder Library picks its layout here (ES-DE/Batocera, RomM or flat); a
   * Library with a structure of its own (RomM) has no layout and shows where
   * its own structure puts them. The folders come from the backend
   * (`/api/libraries/{id}/placements`), so this view never carries its own
   * copy of a Platform's folder name. Listed only once the section is opened:
   * it stats a folder per Platform, and asks RomM for its platforms.
   */
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';

  let { library } = $props();

  const LAYOUTS = [
    { value: 'esde', label: 'ES-DE / Batocera (ps2, gc, …)' },
    { value: 'romm', label: 'RomM (roms/ps2, roms/ngc, …)' },
    { value: 'flat', label: 'Flat (everything in the Library folder)' },
  ];

  let open = $state(false);
  let rows = $state([]);
  let loading = $state(false);
  let error = $state(null);
  let saving = $state(false);
  let request = 0; // the latest listing; an older answer is dropped

  async function load(id) {
    const mine = ++request;
    loading = true;
    error = null;
    try {
      const data = await api.getLibraryPlacements(id);
      if (mine !== request) return;
      rows = Array.isArray(data?.placements) ? data.placements : [];
    } catch (e) {
      if (mine !== request) return;
      error = e?.message ?? 'Failed to list where Images go';
      rows = [];
    } finally {
      if (mine === request) loading = false;
    }
  }

  // Listed when opened, and again whenever the layout changes.
  $effect(() => {
    const id = library.id;
    void library.layout;
    if (open) load(id);
  });

  async function setLayout(layout) {
    saving = true;
    try {
      await libraries.saveSettings(library.id, { layout });
      toast.success('Layout saved');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save');
    } finally {
      saving = false;
    }
  }
</script>

<section class="placement">
  <div class="text">
    <h2>Where new Images go</h2>
    {#if library.layout != null}
      <p class="muted">
        How this Library's Platform folders are laid out. Detection reads the same folder
        names, so an Image placed here is found under its Platform again.
      </p>
    {:else}
      <p class="muted">RomM's own folder structure, with RomM's folder names.</p>
    {/if}
  </div>
  {#if library.layout != null}
    <div class="layout">
      <Select
        label="Layout"
        value={library.layout}
        options={LAYOUTS}
        disabled={saving}
        onchange={setLayout}
      />
    </div>
  {/if}
  <details class="list" bind:open>
    <summary>Folder for each Platform</summary>
    {#if loading && !rows.length}
      <p class="muted"><Spinner /> Working out the folders…</p>
    {:else if error}
      <p class="error">{error}</p>
    {:else if !rows.length}
      <p class="muted">This Library has no folder yet.</p>
    {:else}
      <ul>
        {#each rows as row (row.slug)}
          <li>
            <span class="name">{row.name}</span>
            <span aria-hidden="true">→</span>
            <code>{row.folder || '(Library folder)'}</code>
            {#if !row.exists}<Badge tone="info">will be created</Badge>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  </details>
</section>

<style>
  .placement {
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
  .text { flex: 1 1 40ch; min-width: 0; }
  h2 { margin: 0 0 4px; font-size: var(--text-md, 1rem); }
  p { margin: 0; }
  .layout { min-width: 280px; }
  .list { flex: 1 1 100%; }
  summary { cursor: pointer; }
  ul {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    columns: 2 320px;
    font-size: var(--text-sm, 0.85rem);
  }
  li {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 2px 0;
    break-inside: avoid;
  }
  .name { font-weight: var(--weight-semibold); }
  code { overflow-wrap: anywhere; }
  .muted { color: var(--text-3); }
  .error { color: var(--danger, #d33); }
</style>
