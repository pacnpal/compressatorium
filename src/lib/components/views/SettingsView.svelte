<script>
  /**
   * Settings: everything set once, on one page. `#/settings/<section>`
   * scrolls to that section. Each section loads on its own.
   */
  import { onMount, tick } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import DATView from './DATView.svelte';
  import NotificationSettings from './NotificationSettings.svelte';
  import Converters from '$lib/components/settings/Converters.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';

  const SECTIONS = [
    { id: 'connections', title: 'Connections' },
    { id: 'identification', title: 'Game identification' },
    { id: 'notifications', title: 'Notifications' },
    { id: 'converters', title: 'Converters' },
  ];

  $effect(() => {
    const section = ui.settingsSection;
    if (!section) return;
    tick().then(() => {
      document.getElementById(`settings-${section}`)?.scrollIntoView({ block: 'start' });
    });
  });

  onMount(() => {
    if (!romm.status && !romm.statusLoading) romm.loadStatus();
  });
</script>

<section class="view" aria-labelledby="settings-title">
  <header class="header">
    <h1 id="settings-title">Settings</h1>
    <p class="hint">Set once. Everything here has a sensible default.</p>
  </header>

  <nav class="jump" aria-label="Settings sections">
    {#each SECTIONS as s (s.id)}
      <a href={hashFor('settings', { tab: s.id })}>{s.title}</a>
    {/each}
  </nav>

  <section class="card" id="settings-connections" aria-labelledby="settings-connections-title">
    <h2 id="settings-connections-title">Connections</h2>
    <div class="line">
      <span class="what">RomM</span>
      {#if romm.status == null}
        <span class="muted">…</span>
      {:else if romm.configured}
        <Badge tone={romm.connected ? 'success' : 'warning'}>
          {romm.connected ? 'Connected' : 'Not answering'}
        </Badge>
      {:else}
        <span class="muted">Not connected</span>
      {/if}
      <a class="manage" href={hashFor('libraries', { id: 'romm', tab: 'connection' })}>Manage</a>
    </div>
  </section>

  <section class="card hosted" id="settings-identification" aria-label="Game identification">
    <DATView />
  </section>

  <section class="card" id="settings-notifications" aria-labelledby="settings-notifications-title">
    <h2 id="settings-notifications-title">Notifications</h2>
    <NotificationSettings />
  </section>

  <section class="card" id="settings-converters" aria-labelledby="settings-converters-title">
    <h2 id="settings-converters-title">Converters</h2>
    <Converters />
  </section>
</section>

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
  .header h1 {
    margin: 0;
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .hint { color: var(--text-2); margin-top: var(--space-1); }
  .jump { display: flex; flex-wrap: wrap; gap: var(--space-3); font-size: var(--text-sm); }
  .jump a { color: var(--accent); text-decoration: none; }
  .jump a:hover { text-decoration: underline; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-1);
    padding: var(--space-4);
    min-width: 0;
    scroll-margin-top: var(--space-4);
  }
  /* The hosted view brings its own page padding and width; take them back. */
  .hosted > :global(.view) { padding: 0; max-width: none; }
  .card h2 {
    margin: 0 0 var(--space-3);
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .line { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); }
  .what { font-weight: var(--weight-medium); color: var(--text-1); }
  .muted { color: var(--text-2); }
  .manage { margin-left: auto; color: var(--accent); text-decoration: none; }
  .manage:hover { text-decoration: underline; }
</style>
