<script>
  /**
   * Libraries: the collections Compressatorium looks after. `#/libraries`
   * lists them; `#/libraries/romm[/<tab>]` is the RomM page and
   * `#/libraries/<folder id>[/<tab>]` a folder Library's settings.
   */
  import { onMount } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { libraries, ROMM_LIBRARY_ID } from '$lib/stores/libraries.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import RommView from './RommView.svelte';
  import RulesView from './RulesView.svelte';
  import AddLibraryModal from '$lib/components/modals/AddLibraryModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Plus from '@lucide/svelte/icons/plus';

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

  onMount(() => {
    libraries.load();
    if (!romm.statusLoading) romm.loadStatus();
    if (!romm.settings) romm.loadSettings();
  });
</script>

{#if id === ROMM_LIBRARY_ID}
  <RommView />
{:else if id}
  {#key id}<RulesView />{/key}
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
      <div class="state"><Spinner size="sm" label="Loading" /> Loading your Libraries…</div>
    {:else if libraries.error}
      <div class="state">
        <span>Couldn't load your Libraries. {libraries.error}</span>
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
        <li>
          <a
            class="card"
            href={hashFor('libraries', { id: ROMM_LIBRARY_ID, tab: romm.configured ? null : 'connection' })}
          >
            <span class="name">RomM</span>
            {#if rommStatus == null}
              <span class="where">…</span>
            {:else if romm.configured}
              <span class="where">RomM · {host(romm.settings?.url) || 'connected'}</span>
              <span class="facts">
                {#if romm.connected}<Badge tone="success">Connected</Badge>
                {:else}<Badge tone="warning">Not answering</Badge>{/if}
              </span>
            {:else}
              <span class="where">Not connected</span>
              <span class="facts"><span class="link">Connect RomM →</span></span>
            {/if}
          </a>
        </li>
        {#each folders as lib (lib.id)}
          <li>
            <a class="card" href={hashFor('libraries', { id: lib.id })}>
              <span class="name">{lib.name}</span>
              <span class="where mono">{lib.root}</span>
              <span class="facts">
                <Badge tone={lib.schedule_enabled ? 'success' : 'neutral'}>
                  {lib.schedule_enabled ? 'Automation on' : 'Automation off'}
                </Badge>
              </span>
            </a>
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
  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-4);
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    height: 100%;
    padding: var(--space-4);
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-1);
    color: var(--text-1);
    text-decoration: none;
    min-width: 0;
    transition: border-color var(--dur-fast) var(--ease-out);
  }
  .card:hover { border-color: var(--border-strong); }
  .card:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .name { font-weight: var(--weight-semibold); font-size: var(--text-base); }
  .where { color: var(--text-2); font-size: var(--text-sm); overflow-wrap: anywhere; }
  .mono { font-family: var(--font-mono); font-size: var(--text-xs); }
  .facts { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
  .link { color: var(--accent); font-size: var(--text-sm); }
</style>
