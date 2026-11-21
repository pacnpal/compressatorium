<script>
  import { ui, VIEW_TITLES } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { libraries, ROMM_LIBRARY_ID } from '$lib/stores/libraries.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import ThemeToggle from './ThemeToggle.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import Menu from '@lucide/svelte/icons/menu';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';

  const version = $derived(ui.appVersion);
  const view = $derived(ui.activeView);
  // On a Library's own page the title is that Library, under "Libraries ›".
  const libraryId = $derived(view === 'libraries' ? ui.libraryTarget : null);
  const libraryName = $derived(
    libraryId === ROMM_LIBRARY_ID
      ? 'RomM'
      : (libraries.list.find((lib) => lib.id === libraryId)?.name ?? ''),
  );
  const title = $derived(libraryId ? libraryName : (VIEW_TITLES[view] ?? ''));

  // Shown only while something is converting or waiting.
  const converting = $derived(jobs.processingCount);
  const waiting = $derived(jobs.queuedCount);
  const chip = $derived(
    [converting && `${converting} converting`, waiting && `${waiting} waiting`]
      .filter(Boolean)
      .join(' · '),
  );
</script>

<header class="topbar">
  <div class="left">
    <IconButton label="Open menu" size="md" onclick={() => ui.openDrawer()}>
      <Menu size={18} />
    </IconButton>
    {#if libraryId}
      <a class="crumb" href={hashFor('libraries')}>Libraries</a>
      <span class="sep" aria-hidden="true">›</span>
    {/if}
    <span class="view-title">{title}</span>
  </div>
  <div class="right">
    {#if chip}
      <a class="activity-chip" href={hashFor('activity', { tab: 'running' })} title="Open Running in Activity">
        <RefreshCw size={14} aria-hidden="true" class="spin" />
        <span>{chip}</span>
      </a>
    {/if}
    {#if version}<span class="version" title="Application version">v{version}</span>{/if}
    <ThemeToggle />
  </div>
</header>

<style>
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    height: var(--topbar-height);
    padding: 0 var(--space-4);
    background: var(--surface-1);
    border-bottom: 1px solid var(--border-subtle);
    position: sticky;
    top: 0;
    z-index: var(--z-topbar);
  }
  .left, .right { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
  .view-title {
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .crumb {
    color: var(--text-2);
    font-size: var(--text-lg);
    text-decoration: none;
    white-space: nowrap;
  }
  .crumb:hover { color: var(--text-1); text-decoration: underline; }
  .sep { color: var(--text-3); }
  .activity-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 2px 10px;
    border-radius: var(--radius-full);
    background: var(--accent-muted);
    color: var(--text-1);
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    text-decoration: none;
    white-space: nowrap;
  }
  .activity-chip:hover { background: var(--surface-3); }
  .activity-chip:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .activity-chip :global(.spin) { animation: spin 1.4s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .version {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-3);
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    background: var(--surface-2);
  }
  @media (max-width: 520px) {
    .version { display: none; }
  }
  @media (min-width: 900px) {
    .topbar :global(.icon-btn[aria-label='Open menu']) { display: none; }
  }
</style>
