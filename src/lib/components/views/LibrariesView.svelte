<script>
  /**
   * Libraries: the collections Compressatorium looks after. `#/libraries`
   * lists them as cards, RomM first; `#/libraries/<id>[/<tab>]` is one
   * Library's page (LibraryView). Each card reads its own facts lazily.
   */
  import { onMount } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { libraries, ROMM_LIBRARY_ID } from '$lib/stores/libraries.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import { drivePath } from '$lib/util/drivePath.js';
  import LibraryView from './LibraryView.svelte';
  import AddLibraryModal from '$lib/components/modals/AddLibraryModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Plus from '@lucide/svelte/icons/plus';
  import { plural } from '$lib/api/format.js';

  const id = $derived(ui.libraryTarget);
  const folders = $derived(libraries.folders);
  const rommStatus = $derived(romm.status);
  const empty = $derived(
    !libraries.loading && !libraries.error && folders.length === 0
      && rommStatus != null && !romm.configured,
  );
  let adding = $state(false);

  function host(url) {
    try {
      return new URL(url).host;
    } catch {
      return '';
    }
  }

  const needYou = (libraryId) => findings.list.filter(
    (row) => row?.library_id === libraryId && !row.dismissed,
  ).length;


  // Each card's facts, read once it shows.
  $effect(() => {
    if (id) return;
    for (const lib of folders) libraries.loadFacts(lib.id);
    if (romm.usable) libraries.loadFacts(ROMM_LIBRARY_ID);
  });

  onMount(() => {
    libraries.load();
    if (!romm.statusLoading) romm.loadStatus();
    if (!romm.settings) romm.loadSettings();
    if (!fileBrowser.volumes.length) fileBrowser.fetchVolumes();
  });
</script>

{#if id}
  <LibraryView />
{:else}
  <section class="view" aria-labelledby="libraries-title">
    <header class="header">
      <div>
        <h1 id="libraries-title">Libraries</h1>
        <p class="hint">
          A Library is a games folder (or RomM) that Compressatorium keeps converted and checked.
        </p>
      </div>
      <Button variant="primary" onclick={() => (adding = true)}>
        {#snippet icon()}<Plus size={14} />{/snippet}
        Add a Library
      </Button>
    </header>

    {#if libraries.loading && libraries.list.length === 0}
      <ul class="cards" aria-busy="true" aria-label="Loading your Libraries">
        {#each [0, 1, 2] as n (n)}<li class="card skeleton" aria-hidden="true"></li>{/each}
      </ul>
    {:else if libraries.error}
      <div class="state" role="alert">
        <strong>Couldn't load your Libraries.</strong>
        <span>{libraries.error}</span>
        <Button size="sm" variant="secondary" onclick={() => libraries.load()}>Try again</Button>
      </div>
    {:else if empty}
      <EmptyState
        glyph="▤"
        title="No Libraries yet"
        description="Add your games folder, or connect RomM. Compressatorium then converts new games on its own."
      >
        {#snippet actions()}
          <Button variant="primary" onclick={() => (adding = true)}>Add a Library</Button>
          <Button variant="secondary" onclick={() => ui.navigate('libraries', { id: 'romm', tab: 'connection' })}>
            Connect RomM
          </Button>
        {/snippet}
      </EmptyState>
    {:else}
      <ul class="cards">
        <li class="card">
          <a
            class="name"
            href={hashFor('libraries', { id: ROMM_LIBRARY_ID, tab: romm.configured ? null : 'connection' })}
          >RomM</a>
          {#if rommStatus == null}
            <span class="where">…</span>
          {:else if romm.configured}
            {@const facts = libraries.facts[ROMM_LIBRARY_ID]}
            {@const problems = needYou(ROMM_LIBRARY_ID)}
            <span class="where">RomM · {host(romm.settings?.url) || 'connected'}</span>
            <span class="facts">
              {#if !romm.connected}
                <Badge tone="warning">Not answering</Badge>
              {:else if facts}
                {#if facts.consoles != null}
                  <span>{plural(facts.consoles, 'console', 'consoles')} · {plural(facts.games, 'game', 'games')}</span>
                {/if}
              {:else}
                <span class="muted">…</span>
              {/if}
              <Badge tone={romm.scheduleEnabled ? 'success' : 'neutral'}>
                {romm.scheduleEnabled ? 'Automation on' : 'Automation off'}
              </Badge>
              {#if facts?.watchFolders}<span>{plural(facts.watchFolders, 'watch folder', 'watch folders')}</span>{/if}
              {#if problems}
                <a class="above" href={hashFor('activity', { tab: 'attention', id: ROMM_LIBRARY_ID })}>
                  <Badge tone="warning">{problems} need you</Badge>
                </a>
              {/if}
            </span>
          {:else}
            <span class="where">Not connected</span>
            <span class="facts">
              <Button size="sm" variant="secondary" onclick={() => ui.navigate('libraries', { id: 'romm', tab: 'connection' })}>
                Connect RomM
              </Button>
            </span>
          {/if}
        </li>
        {#each folders as lib (lib.id)}
          {@const facts = libraries.facts[lib.id]}
          {@const problems = needYou(lib.id)}
          <li class="card">
            <a class="name" href={hashFor('libraries', { id: lib.id })}>{lib.name}</a>
            <span class="where mono">{drivePath(lib.root, fileBrowser.volumes)}</span>
            <span class="facts">
              {#if facts}
                {#if facts.consoles != null}
                  <span>{plural(facts.consoles, 'console', 'consoles')} · {plural(facts.games, 'game', 'games')}</span>
                {/if}
              {:else}
                <span class="muted">…</span>
              {/if}
              <Badge tone={lib.schedule_enabled ? 'success' : 'neutral'}>
                {lib.schedule_enabled ? 'Automation on' : 'Automation off'}
              </Badge>
              {#if facts?.watchFolders}<span>{plural(facts.watchFolders, 'watch folder', 'watch folders')}</span>{/if}
              {#if problems}
                <a class="above" href={hashFor('activity', { tab: 'attention', id: lib.id })}>
                  <Badge tone="warning">{problems} need you</Badge>
                </a>
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<AddLibraryModal open={adding} onClose={() => (adding = false)} />

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
    padding: var(--space-5);
    max-width: var(--container-max);
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
  .header h1 {
    margin: 0;
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .hint { color: var(--text-2); margin-top: var(--space-1); }
  .state {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    color: var(--text-2);
  }
  .state strong { color: var(--text-1); }
  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-4);
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
  }
  .card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-height: 120px;
    padding: var(--space-4);
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-1);
    color: var(--text-1);
    min-width: 0;
    transition: border-color var(--dur-fast) var(--ease-out);
  }
  .card:hover { border-color: var(--border-strong); }
  .card:focus-within { box-shadow: var(--focus-ring); }
  .skeleton { background: var(--surface-2); box-shadow: none; }
  /* The name links the whole card; the "need you" link sits above it. */
  .name {
    font-weight: var(--weight-semibold);
    font-size: var(--text-base);
    color: var(--text-1);
    text-decoration: none;
    outline: none;
  }
  .name::after { content: ''; position: absolute; inset: 0; border-radius: inherit; }
  .above, .facts :global(button) { position: relative; z-index: 1; }
  .above { text-decoration: none; }
  .where { color: var(--text-2); font-size: var(--text-sm); overflow-wrap: anywhere; }
  .mono { font-family: var(--font-mono); font-size: var(--text-xs); }
  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
    margin-top: auto;
    font-size: var(--text-sm);
    color: var(--text-2);
  }
  .muted { color: var(--text-2); }
</style>
