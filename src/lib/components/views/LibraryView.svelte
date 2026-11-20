<script>
  /**
   * One Library (`#/libraries/<id>[/<tab>]`): Browse, Automation and, for
   * RomM, Connection. The tab is in the URL. This view loads the Library's
   * consoles and Rules once for every tab; for RomM it also puts the file
   * browser back to the Drives when it is left (RomM's Browse lists RomM).
   */
  import { onMount, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { DropdownMenu } from 'bits-ui';
  import { api } from '$lib/api/endpoints.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { libraries, ROMM_LIBRARY_ID } from '$lib/stores/libraries.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import { drivePath } from '$lib/util/drivePath.js';
  import LibraryBrowse from '$lib/components/library/LibraryBrowse.svelte';
  import RommBrowse from '$lib/components/library/RommBrowse.svelte';
  import LibraryAutomation from '$lib/components/library/LibraryAutomation.svelte';
  import RommSettings from './RommSettings.svelte';
  import BaseModal from '$lib/components/modals/BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Panel from '$lib/components/ui/Panel.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Ellipsis from '@lucide/svelte/icons/ellipsis';

  const id = $derived(ui.libraryTarget);
  const isRomm = $derived(id === ROMM_LIBRARY_ID);
  const library = $derived(libraries.list.find((lib) => lib.id === id) ?? null);
  const source = $derived(libraries.sourceFor(id));
  const tabs = $derived(
    isRomm
      ? [['browse', 'Browse'], ['automation', 'Automation'], ['connection', 'Connection']]
      : [['browse', 'Browse'], ['automation', 'Automation']],
  );
  const tab = $derived(tabs.some(([t]) => t === ui.libraryTab) ? ui.libraryTab : 'browse');
  const name = $derived(isRomm ? 'RomM' : (library?.name ?? ''));

  function host(url) {
    try {
      return new URL(url).host;
    } catch {
      return '';
    }
  }

  const where = $derived.by(() => {
    if (isRomm) {
      if (!romm.configured) return 'Not connected';
      return `Connected to ${host(romm.settings?.url) || 'RomM'}`;
    }
    return library?.root ? drivePath(library.root, fileBrowser.volumes) : '';
  });

  // #518: a Library inside another, or holding others.
  const within = $derived(library?.within ?? null);
  const contains = $derived(Array.isArray(library?.contains) ? library.contains : []);

  function openTab(next) {
    ui.navigate('libraries', { id, tab: next });
  }

  // False once the view is gone: a RomM load that outlives it must not put
  // the file browser into RomM mode after the person has left.
  let alive = true;
  let enteredRomm = false;

  async function loadRomm() {
    await Promise.all([romm.loadStatus(), romm.loadSettings()]);
    if (!alive || !romm.usable) return;
    enteredRomm = true;
    await Promise.allSettled([romm.loadPlatforms(), romm.loadRules()]);
    if (!alive) return;
    if (romm.pendingRepins > 0 && romm.settings?.repin_on_load !== false) {
      // Surfaced by the pending badge on Browse when it fails.
      await romm.runRepin().catch(() => {});
    }
  }

  onMount(() => {
    libraries.load();
    if (!fileBrowser.volumes.length) fileBrowser.fetchVolumes();
    return () => {
      alive = false;
      if (!enteredRomm) return;
      // Before exitRomm: a platform load still in flight ends by selecting a
      // console, which would put RomM straight back into the browser.
      romm.cancelPlatformLoad();
      fileBrowser.exitRomm();
    };
  });

  // Load whichever Library is open. Untracked: the loads write the source's
  // own state, which must not re-run this.
  $effect(() => {
    const src = source;
    const romMode = isRomm;
    if (!src) return;
    untrack(() => {
      if (romMode) {
        loadRomm();
        return;
      }
      if (enteredRomm) {
        enteredRomm = false;
        romm.cancelPlatformLoad();
        fileBrowser.exitRomm();
      }
      src.loadPlatforms();
      src.loadRules().catch(() => {});
    });
  });

  // After a Connection save: the store reloads the consoles itself.
  function afterSave() {
    if (romm.usable) romm.loadRules().catch(() => {});
    else romm.loadStatus();
  }

  // --- the ⋯ menu (folder Libraries) ---------------------------------------

  async function findDuplicates() {
    try {
      await api.findDuplicates(id);
      toast.info('Looking for duplicates…', {
        description: 'Each group shows in Activity when it finishes. Nothing is deleted.',
        action: { label: 'Review', onClick: () => ui.navigate('activity', { tab: 'attention', id }) },
      });
    } catch (e) {
      toast.error(e?.message ?? "Couldn't start looking for duplicates");
    }
  }

  let renaming = $state(false);
  let draftName = $state('');
  let savingName = $state(false);

  async function rename(event) {
    event?.preventDefault();
    const next = draftName.trim();
    if (!next) return;
    savingName = true;
    try {
      await libraries.saveSettings(id, { name: next });
      renaming = false;
    } catch (e) {
      toast.error(e?.message ?? "Couldn't rename it");
    } finally {
      savingName = false;
    }
  }

  let confirmRemove = $state(false);
  let removing = $state(false);

  async function remove() {
    const gone = name;
    removing = true;
    try {
      await libraries.remove(id);
      confirmRemove = false;
      toast.success(`${gone} is no longer a Library. Nothing on disk was touched.`);
      ui.navigate('libraries');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't remove it");
    } finally {
      removing = false;
    }
  }
</script>

<section class="view" aria-labelledby="library-title">
  {#if !isRomm && !library}
    {#if libraries.loading || !libraries.list.length}
      <p class="muted"><Spinner size="sm" label="Loading" /> Loading…</p>
    {:else}
      <EmptyState title="This Library isn't here any more" description="It may have been removed.">
        {#snippet actions()}
          <Button variant="secondary" onclick={() => ui.navigate('libraries')}>See your Libraries</Button>
        {/snippet}
      </EmptyState>
    {/if}
  {:else}
    <header class="header">
      <div class="title">
        <h1 id="library-title">{name}</h1>
        {#if where}<p class="where" class:mono={!isRomm}>{where}</p>{/if}
        {#if within}
          <p class="note">Inside {within.name ?? within} — this Library's settings apply to its own folder.</p>
        {/if}
        {#if contains.length}
          <p class="note">
            Holds {contains.map((c) => c.name ?? c).join(', ')} — those folders follow their own settings.
          </p>
        {/if}
      </div>
      {#if !isRomm}
        <DropdownMenu.Root>
          <DropdownMenu.Trigger class="library-menu-trigger" aria-label={`More for ${name}`} title="More">
            <Ellipsis size={18} />
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content class="library-menu" align="end" sideOffset={4}>
              <DropdownMenu.Item class="library-menu-item" onSelect={findDuplicates}>
                Find duplicates
              </DropdownMenu.Item>
              <DropdownMenu.Item
                class="library-menu-item"
                onSelect={() => ui.navigate('files', { path: library.root })}
              >
                Open folder in Files
              </DropdownMenu.Item>
              <DropdownMenu.Item
                class="library-menu-item"
                onSelect={() => { draftName = name; renaming = true; }}
              >
                Rename…
              </DropdownMenu.Item>
              <DropdownMenu.Item class="library-menu-item danger" onSelect={() => (confirmRemove = true)}>
                Remove Library
              </DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      {/if}
    </header>

    <div class="tabs" role="tablist" aria-label={`${name} sections`}>
      {#each tabs as [t, label] (t)}
        <a
          class="tab" class:active={tab === t}
          role="tab"
          id={`library-tab-${t}`}
          aria-selected={tab === t}
          aria-controls="library-panel"
          href={hashFor('libraries', { id, tab: t })}
          onclick={(e) => { e.preventDefault(); openTab(t); }}
        >{label}</a>
      {/each}
    </div>

    <div role="tabpanel" id="library-panel" aria-labelledby={`library-tab-${tab}`} class="panel">
      {#if tab === 'connection' && isRomm}
        <RommSettings onsaved={afterSave} />
      {:else if tab === 'automation'}
        {#if isRomm && !romm.configured && romm.status}
          <EmptyState title="RomM isn't connected yet" description="Connect it once and its consoles show up here.">
            {#snippet actions()}
              <Button onclick={() => openTab('connection')}>Connect RomM</Button>
            {/snippet}
          </EmptyState>
        {:else if source}
          {#key id}<LibraryAutomation {source} {library} />{/key}
        {/if}
      {:else if isRomm}
        <RommBrowse />
      {:else if source}
        <Panel>
          <LibraryBrowse {source} {library} />
        </Panel>
      {/if}
    </div>
  {/if}
</section>

<BaseModal open={renaming} onClose={() => (renaming = false)} title="Rename this Library" size="sm">
  {#snippet body()}
    <form class="rename" onsubmit={rename}>
      <label>
        <span class="label">Name</span>
        <!-- svelte-ignore a11y_autofocus -->
        <input type="text" bind:value={draftName} autofocus />
      </label>
    </form>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={() => (renaming = false)}>Cancel</Button>
    <Button onclick={rename} loading={savingName} disabled={!draftName.trim()}>Rename</Button>
  {/snippet}
</BaseModal>

<BaseModal
  open={confirmRemove}
  onClose={() => (confirmRemove = false)}
  title={`Remove ${name}?`}
  description="Nothing on disk is touched. Its automation settings and history notes are removed."
  size="sm"
>
  {#snippet footer()}
    <Button variant="secondary" onclick={() => (confirmRemove = false)}>Cancel</Button>
    <Button variant="destructive" onclick={remove} loading={removing}>Remove</Button>
  {/snippet}
</BaseModal>

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-5);
    max-width: 1760px;
    margin: 0 auto;
    width: 100%;
    min-width: 0;
  }
  .header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .title { min-width: 0; }
  .header h1 {
    margin: 0;
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .where { margin: var(--space-1) 0 0; color: var(--text-2); overflow-wrap: anywhere; }
  .mono { font-family: var(--font-mono); font-size: var(--text-sm); }
  .note { margin: var(--space-1) 0 0; color: var(--text-2); font-size: var(--text-sm); }
  .muted { color: var(--text-2); }
  .tabs {
    display: flex;
    gap: var(--space-1);
    border-bottom: 1px solid var(--border-subtle);
    overflow-x: auto;
  }
  .tab {
    padding: var(--space-2) var(--space-3);
    border-bottom: 2px solid transparent;
    color: var(--text-2);
    font-weight: var(--weight-medium);
    text-decoration: none;
    white-space: nowrap;
  }
  .tab.active { color: var(--text-1); border-bottom-color: var(--accent); }
  .tab:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .panel { min-width: 0; }
  .rename label { display: flex; flex-direction: column; gap: var(--space-1); }
  .rename .label { font-size: var(--text-xs); color: var(--text-2); }
  .rename input {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font: inherit;
    padding: var(--space-2) var(--space-3);
  }
  :global(.library-menu-trigger) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-1);
    color: var(--text-2);
    cursor: pointer;
  }
  :global(.library-menu-trigger:focus-visible) { outline: none; box-shadow: var(--focus-ring); }
  :global(.library-menu) {
    min-width: 200px;
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    box-shadow: var(--elev-2);
    padding: var(--space-1);
    z-index: var(--z-modal);
  }
  :global(.library-menu-item) {
    padding: 6px var(--space-2);
    color: var(--text-1);
    font-size: var(--text-sm);
    border-radius: var(--radius-sm);
    cursor: pointer;
    outline: none;
  }
  :global(.library-menu-item[data-highlighted]) { background: var(--accent-muted); color: var(--accent); }
  :global(.library-menu-item.danger) { color: var(--error); }
</style>
