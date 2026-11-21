<script>
  /**
   * A folder Library's Browse tab: its consoles, how many games each has, and
   * the folder they're in (where its games are, which the listing says,
   * not where new ones would go), each a link into Files. The view
   * (LibraryView) loads the consoles.
   *
   * @typedef {Object} Props
   * @property {object} source the folder Library's rules source
   * @property {object} library the Library row
   */
  import { hashFor } from '$lib/router/routes.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';

  /** @type {Props} */
  let { source, library } = $props();

  // A folder is read one listing at a time: while one runs, look again in 3 s.
  const RETRY_MS = 3000;

  $effect(() => {
    if (!source.platformsBusy) return;
    const timer = setTimeout(() => source.loadPlatforms(), RETRY_MS);
    return () => clearTimeout(timer);
  });

  const needConsole = $derived((source.counts?.unsorted ?? 0) + (source.counts?.conflicted ?? 0));
  const unreadable = $derived(source.counts?.unreadable ?? 0);
  const folderOf = (platform) => platform.folder ?? '';
  const openHref = (platform) => hashFor('files', {
    path: folderOf(platform) ? `${library.root.replace(/\/+$/, '')}/${folderOf(platform)}` : library.root,
  });
</script>

{#if (source.platformsLoading && source.platforms.length === 0) || source.platformsBusy}
  <p class="muted"><Spinner size="sm" label="Reading" /> Reading the folder…</p>
{:else if source.platformsError}
  <div class="state" role="alert">
    <strong>Couldn't read this folder</strong>
    <span class="muted">{source.platformsError}</span>
    <Button size="sm" variant="secondary" onclick={() => source.loadPlatforms()}>Try again</Button>
  </div>
{:else if source.platforms.length === 0}
  <EmptyState
    title="No games found here yet"
    description="Put games in folders named after their console, like psx or ps2. New ones show up on the next look."
  >
    {#snippet actions()}
      <Button variant="secondary" onclick={() => ui.navigate('files', { path: library.root })}>
        Open in Files
      </Button>
    {/snippet}
  </EmptyState>
{:else}
  {#if unreadable > 0}
    <p class="warn" role="status">
      {unreadable} {unreadable === 1 ? "file couldn't" : "files couldn't"} be read, so they're left alone.
    </p>
  {/if}
  {#if needConsole > 0}
    <p class="warn" role="status">
      <strong>{needConsole} {needConsole === 1 ? 'game needs' : 'games need'} a console.</strong>
      Compressatorium leaves them alone until you pick one.
      <a href={hashFor('activity', { tab: 'attention', id: library.id })}>Fix in Activity</a>
    </p>
  {/if}
  <table class="consoles">
    <thead>
      <tr><th>Console</th><th>Games</th><th>Folder</th><th><span class="sr-only">Open</span></th></tr>
    </thead>
    <tbody>
      {#each source.platforms as platform (platform.id)}
        <tr>
          <td>{platform.name}</td>
          <td class="num">{platform.images ?? 0}</td>
          <td><code>{folderOf(platform) ? `${folderOf(platform)}/` : '(the top folder)'}</code></td>
          <td class="open"><a href={openHref(platform)}>Open</a></td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<style>
  .consoles { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  th {
    text-align: left;
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0 var(--space-3) var(--space-2) 0;
  }
  td { padding: var(--space-2) var(--space-3) var(--space-2) 0; border-top: 1px solid var(--border-subtle); }
  td code { font-size: var(--text-xs); overflow-wrap: anywhere; }
  .num { font-variant-numeric: tabular-nums; }
  .open { text-align: right; }
  a { color: var(--accent); }
  .warn {
    margin: 0 0 var(--space-3);
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--warning);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
  }
  .state { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
  .muted { color: var(--text-2); font-size: var(--text-sm); }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
