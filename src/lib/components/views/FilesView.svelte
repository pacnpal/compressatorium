<script>
  import { onMount, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { layout } from '$lib/stores/layout.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { readBool, writeBool } from '$lib/util/localStorage.js';
  import VolumeList from '$lib/components/panels/VolumeList.svelte';
  import FileList from '$lib/components/panels/FileList.svelte';
  import ConvertPanel from '$lib/components/panels/ConvertPanel.svelte';
  import Splitter from '$lib/components/ui/Splitter.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import AddLibraryModal from '$lib/components/modals/AddLibraryModal.svelte';
  import X from '@lucide/svelte/icons/x';

  // `#/files/<path>` opens that folder. Opening it puts the URL back to
  // `#/files`, so this runs once per link.
  $effect(() => {
    const path = ui.filesPath;
    if (path) untrack(() => fileBrowser.openFolder(path));
  });

  onMount(() => {
    libraries.load();
    if (!romm.status && !romm.statusLoading) romm.loadStatus();
  });

  // The first-run banner: no folder Library and no RomM yet, until dismissed.
  const FIRST_RUN_KEY = 'files.firstRunDismissed';
  let firstRunDismissed = $state(readBool(FIRST_RUN_KEY, false));
  const firstRun = $derived(
    !firstRunDismissed
      && !libraries.loading && !libraries.error && libraries.folders.length === 0
      && romm.status != null && !romm.configured,
  );
  let adding = $state(false);

  function dismissFirstRun() {
    firstRunDismissed = true;
    writeBool(FIRST_RUN_KEY, true);
  }

  // The Library the open folder belongs to: the nearest root above it.
  const path = $derived(fileBrowser.currentPath);
  const inLibrary = $derived.by(() => {
    if (!path || fileBrowser.rommPlatformId !== null) return null;
    const holders = libraries.folders.filter((lib) => {
      const root = String(lib.root ?? '').replace(/\/+$/, '');
      return !!root && (path === root || path.startsWith(`${root}/`));
    });
    return holders.sort((a, b) => b.root.length - a.root.length)[0] ?? null;
  });
  let making = $state(false);

  async function makeLibrary() {
    making = true;
    try {
      const library = await libraries.register(path);
      if (!library?.id) return;
      toast.success(`${library.name} is now a Library`, {
        action: {
          label: 'Set it up',
          onClick: () => ui.navigate('libraries', { id: library.id, tab: 'automation' }),
        },
      });
    } catch (e) {
      toast.error(e?.message ?? "Couldn't make it a Library");
    } finally {
      making = false;
    }
  }

  // Snapshot the panel width at drag start; each move applies the
  // cumulative delta. The left divider grows .side as it moves right;
  // the right divider shrinks .right as it moves right (so the middle
  // file list always absorbs the slack).
  let dragStartLeft = 0;
  let dragStartRight = 0;
</script>

<section class="view" aria-labelledby="files-title">
  <header class="header">
    <div>
      <h1 id="files-title">Files</h1>
      <p class="hint">Pick games and press Convert. Each one goes to its best format.</p>
    </div>
    {#if path && fileBrowser.rommPlatformId === null}
      <div class="library-chip">
        {#if inLibrary}
          <a href={`#/libraries/${encodeURIComponent(inLibrary.id)}`}>In Library: {inLibrary.name} →</a>
        {:else}
          <Button variant="ghost" size="sm" loading={making} onclick={makeLibrary}>
            Make this folder a Library
          </Button>
        {/if}
      </div>
    {/if}
  </header>

  {#if firstRun}
    <div class="first-run" role="region" aria-label="Get started">
      <div class="first-run-text">
        <h2>Make Compressatorium look after a games folder</h2>
        <p>A Library converts new games on its own and tells you when something needs you.</p>
        <div class="first-run-actions">
          <Button variant="primary" size="sm" onclick={() => (adding = true)}>Add a Library</Button>
          <Button
            variant="secondary"
            size="sm"
            onclick={() => ui.navigate('libraries', { id: 'romm', tab: 'connection' })}
          >Connect RomM</Button>
        </div>
      </div>
      <IconButton label="Dismiss" onclick={dismissFirstRun}><X size={16} /></IconButton>
    </div>
  {/if}

  <div class="grid" style="--ws-left: {layout.panels.left}px; --ws-right: {layout.panels.right}px;">
    <aside class="side">
      <VolumeList />
    </aside>

    <Splitter
      variant="panel"
      label="Resize drives panel"
      value={layout.panels.left}
      min={160}
      max={360}
      onstart={() => (dragStartLeft = layout.panels.left)}
      onmove={(d) => layout.setPanelWidth('left', dragStartLeft + d)}
      onstep={(d) => layout.setPanelWidth('left', layout.panels.left + d)}
      onreset={() => layout.resetPanel('left')}
    />

    <article class="main">
      <FileList />
    </article>

    <Splitter
      variant="panel"
      label="Resize convert panel"
      value={layout.panels.right}
      min={280}
      max={640}
      onstart={() => (dragStartRight = layout.panels.right)}
      onmove={(d) => layout.setPanelWidth('right', dragStartRight - d)}
      onstep={(d) => layout.setPanelWidth('right', layout.panels.right - d)}
      onreset={() => layout.resetPanel('right')}
    />

    <article class="right">
      <ConvertPanel />
    </article>
  </div>
</section>

<AddLibraryModal open={adding} start={path} onClose={() => (adding = false)} />

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-5);
    /* Wider than the default --container-max: the file table needs the
       room so all its columns show on a big monitor instead of scrolling
       horizontally. Other views keep the narrower reading-width cap. */
    max-width: 1760px;
    margin: 0 auto;
    width: 100%;
    min-width: 0;
  }
  .header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-3);
  }
  .library-chip a {
    display: inline-flex;
    padding: 4px 10px;
    border-radius: var(--radius-full);
    background: var(--accent-muted);
    color: var(--accent);
    font-size: var(--text-sm);
    text-decoration: none;
  }
  .library-chip a:hover { text-decoration: underline; }
  .first-run {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-4);
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-1);
  }
  .first-run h2 {
    margin: 0;
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .first-run p { margin: var(--space-1) 0 var(--space-3); color: var(--text-2); }
  .first-run-actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .header h1 {
    margin: 0;
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .hint {
    color: var(--text-2);
    margin-top: var(--space-1);
  }

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
    min-width: 0;
  }
  /* The panel splitters only make sense in the 3-column desktop layout.
     Hidden (and so removed from grid flow) below 1280px, where the right
     panel stacks and the left rail is a fixed width. Direct-child only:
     the file table's column handles are also .splitter elements but live
     deep inside .main, and must stay usable at every width. */
  .grid > :global(.splitter) { display: none; }
  @media (min-width: 900px) {
    .grid { grid-template-columns: 220px minmax(0, 1fr); }
    /* Two-column mode: volumes rail + file list share the first row, and
       the convert/jobs panel stacks below. Without the explicit span,
       auto-placement drops .right into the 220px rail column, leaving it
       a narrow strip beside a huge empty cell. */
    .right { grid-column: 1 / -1; }
  }
  @media (min-width: 1280px) {
    .grid {
      grid-template-columns:
        var(--ws-left, 220px) auto minmax(0, 1fr) auto var(--ws-right, 360px);
      gap: var(--space-2);
    }
    .grid > :global(.splitter) { display: block; }
    .right { grid-column: auto; }
  }

  .side, .main, .right {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
    min-width: 0;
  }
  .right { display: flex; flex-direction: column; gap: var(--space-4); }
</style>
