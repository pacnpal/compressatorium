<script>
  /**
   * Where new Images go in one Library (#430).
   *
   * A folder Library picks its layout here (RomM style, ES-DE/Batocera style
   * or flat). Every Library lists where new Images of each of its Platforms
   * would land ("PlayStation 2 → roms/ps2"), whether that folder is there yet,
   * or why it can't be told. The backend works it out (`Library.placement`),
   * so the folder names here are the ones detection reads back.
   *
   * RomM often keeps several records for one Platform (#512). Each row shows
   * its own folder; the one new Images go to says so and why ("RomM's NES
   * folder, 4933 ROMs"), and offers the others in one select. A pick is
   * saved and wins over the automatic one.
   */
  import { untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import { PlacementsLoad, placementsOf } from '$lib/util/libraryPlacements.js';
  import Select from '$lib/components/ui/Select.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';

  // `listing`: the view's own Platform listing is running. A folder Library
  // is walked one listing at a time and a second one fails at once
  // (`listing_in_progress`), so the placements (which list the Platforms too)
  // wait for it, then load again whenever `platforms` changes.
  let { library, listing = false, platforms = null } = $props();

  const LAYOUTS = [
    { value: 'esde', label: 'ES-DE / Batocera: <root>/<platform>' },
    { value: 'romm', label: 'RomM: <root>/roms/<platform>' },
    { value: 'flat', label: 'Flat: every Image in <root>' },
  ];

  const isFolder = $derived(library?.kind === 'folder');
  let saving = $state(false);

  // The latest answer, with the Library it is for. Only the selected
  // Library's is shown: until it arrives the panel shows its loading line,
  // never the previous Library's rows (#464). Of overlapping loads only the
  // latest counts (Codex PRRT_kwDOQ9nULM64es7U).
  const placementsLoad = new PlacementsLoad((id) => api.getLibraryPlacements(id));
  let loaded = $state(null);

  async function load(id) {
    const answer = await placementsLoad.load(id);
    if (answer) loaded = answer;
  }

  // Again whenever the Library, its layout or its listed Platforms change,
  // once the view's own listing is done. Primitives for the row, so a fresh
  // copy of the same row (adopted after another setting's save) reloads nothing.
  // `listing` is read after the current flush (a zero timeout), not here: the
  // view's effect that starts its listing may run after this one in the same
  // flush, and the placements must not set off first.
  const libraryId = $derived(library?.id);
  const shown = $derived(placementsOf(loaded, libraryId));
  const layout = $derived(library?.layout);
  $effect(() => {
    const id = libraryId;
    void layout;
    void listing;
    void platforms;
    if (!id) return;
    const timer = setTimeout(() => {
      if (!untrack(() => listing)) load(id);
    }, 0);
    return () => clearTimeout(timer);
  });

  const roms = (n) => (n === 1 ? '1 ROM' : `${n} ROMs`);
  const choiceOptions = (row) =>
    row.choices.map((c) => ({ value: c.name, label: `${c.name} (${roms(c.rom_count)})` }));

  let picking = $state(false);
  async function pick(row, folder) {
    if (!library || folder === row.picked) return;
    picking = true;
    try {
      await api.pickLibraryPlacement(library.id, row.platform, folder);
      toast.success(`New ${row.name} Images go to ${folder}`);
      await load(library.id);
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save where new Images go');
    } finally {
      picking = false;
    }
  }

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
  {#if !shown}
    <p class="muted"><Spinner /> Working out the folders…</p>
  {:else if shown.error}
    <p class="error">{shown.error}</p>
  {:else if shown.placements.length === 0}
    <p class="muted">No Platforms yet.</p>
  {:else}
    <ul class="rows">
      <!-- Keyed by the Library's own Platform id: two RomM platforms can
           share a slug, and both rows show why (Codex PRRT_kwDOQ9nULM64es7J). -->
      {#each shown.placements as row (row.id)}
        <li>
          <span class="name">{row.name}</span>
          {#if row.error}
            <span class="error">can't tell: {row.error}</span>
          {:else}
            <span aria-hidden="true">→</span>
            <code>{row.folder || '(the root)'}</code>
            {#if row.rom_count != null}<span class="muted">{roms(row.rom_count)}</span>{/if}
            {#if !row.exists}<span class="muted new">will be created</span>{/if}
            {#if row.new_images_here}
              <span class="here">new Images go here</span>
            {:else if row.new_images_here === false}
              <span class="muted">not used for new Images</span>
            {/if}
            {#if row.guessed}<span class="guess">best guess</span>{/if}
            {#if row.why}<span class="why muted">{row.why}</span>{/if}
            {#if row.choices.length > 1}
              <div class="pick">
                <Select
                  ariaLabel={`Where new ${row.name} Images go`}
                  value={row.picked}
                  options={choiceOptions(row)}
                  disabled={picking}
                  onchange={(folder) => pick(row, folder)}
                />
              </div>
            {/if}
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
  .here { font-weight: 600; color: var(--success, var(--text-1)); }
  .guess {
    padding: 0 6px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 4px);
    font-size: var(--text-xs, 0.75rem);
  }
  .why { flex-basis: 100%; }
  .pick { flex-basis: 100%; max-width: 320px; }
  .muted { color: var(--text-3); }
  .error { color: var(--danger, #d33); }
</style>
