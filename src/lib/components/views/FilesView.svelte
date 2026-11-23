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
  const inLibrary = $derived(
    fileBrowser.rommPlatformId === null ? libraries.folderLibraryFor(path) : null,
  );
  let making = $state(false);

  async function makeLibrary() {
    if (making) return;
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

  // While games are selected in the stacked layout, Convert sticks to the
  // bottom of the screen (#589). Its height is kept as --sheet-h so a row
  // scrolled into view stops above it, and the row just ticked is brought
  // clear of it when the sheet appears or grows.
  let sheetEl = $state(null);
  let sheetHeight = $state(0);
  const sheetOpen = $derived(fileBrowser.selectedFiles.size > 0);
  $effect(() => {
    fileBrowser.selectedFiles.size;
    sheetHeight;
    if (!sheetEl || getComputedStyle(sheetEl).position !== 'sticky') return;
    const row = document.activeElement?.closest?.('.filelist tbody tr');
    if (!row) return;
    // Only a row the sheet now covers: one scrolled out of view stays there.
    const { top, bottom } = row.getBoundingClientRect();
    if (bottom > sheetEl.getBoundingClientRect().top && top < window.innerHeight) {
      row.scrollIntoView({ block: 'nearest' });
    }
  });
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

  <div
    class="grid"
    style="--ws-left: {layout.panels.left}px; --ws-right: {layout.panels.right}px; --sheet-h: {sheetOpen ? sheetHeight : 0}px;"
  >
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

    <!-- Stacked, the Convert panel is a sheet stuck to the bottom of the
         screen while games are selected. -->
    <article
      class="right"
      class:sheet={sheetOpen}
      bind:this={sheetEl}
      bind:offsetHeight={sheetHeight}
    >
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
    /* The panel layout follows the width this view has, not the window's:
       the sidebar takes its share first (#562). */
    container-type: inline-size;
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

  /* Stacked: Drives, the list, then Convert. A flex column rather than a
     grid so the Convert sheet can stick across all of it: a grid item only
     sticks inside its own grid area. --list-room is the file table at its
     default widths (676px) plus the list's padding and the two frames. */
  .grid {
    --list-room: 712px;
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    min-width: 0;
  }
  /* The panel splitters only make sense with panels side by side. Direct
     children only: the file table's column handles are also .splitter
     elements, deep inside .main, and stay usable at every width. */
  .grid > :global(.splitter) { display: none; }

  /* Convert beside the list once the list still holds the whole table next
     to Convert at its narrowest (280px), with a 9px splitter and two 8px
     gaps; Drives run as a strip across the top (#589). A 1440px laptop with
     the sidebar open gets this. Convert gives way before the table does. */
  @container (min-width: 1024px) {
    .grid {
      display: grid;
      grid-template-columns:
        minmax(0, 1fr) auto
        clamp(280px, calc(100cqw - var(--list-room) - 25px), var(--ws-right, 360px));
      gap: var(--space-2);
    }
    .side { grid-column: 1 / -1; }
    .grid > :global(.splitter) { display: block; }
    .side + :global(.splitter) { display: none; }
  }
  /* Three panels once Drives fit beside them too: the Drives rail (220),
     Convert at its narrowest, the table, two 9px splitters and four 8px
     gaps. */
  @container (min-width: 1264px) {
    .grid {
      grid-template-columns:
        var(--ws-left, 220px) auto minmax(0, 1fr) auto
        clamp(
          280px,
          calc(100cqw - var(--ws-left, 220px) - var(--list-room) - 50px),
          var(--ws-right, 360px)
        );
    }
    .side { grid-column: auto; }
    .side + :global(.splitter) { display: block; }
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
  /* Stacked, the sheet sticks to the foot of the screen and takes its own
     place below the list, so scrolling down always uncovers every row. */
  @container (max-width: 1023px) {
    .right.sheet {
      position: sticky;
      bottom: 0;
      z-index: var(--z-sticky);
      max-height: 50dvh;
      overflow-y: auto;
      box-shadow: var(--elev-2);
      border-bottom-left-radius: 0;
      border-bottom-right-radius: 0;
    }
    /* A row scrolled into view, by focus or by the script above, stops
       above the sheet rather than under it. */
    .grid :global(.filelist tbody tr) {
      scroll-margin-bottom: calc(var(--sheet-h, 0px) + var(--space-2));
    }
  }
</style>
