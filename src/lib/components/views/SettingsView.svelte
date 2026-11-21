<script>
  /**
   * Settings: everything set once, on one page. `#/settings/<section>`
   * scrolls to that section. Each section loads on its own, with its own
   * spinner and error line, so one slow answer never blocks the page.
   */
  import { tick } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import Connections from '$lib/components/settings/Connections.svelte';
  import Identification from '$lib/components/settings/Identification.svelte';
  import NotificationSettings from './NotificationSettings.svelte';
  import Defaults from '$lib/components/settings/Defaults.svelte';
  import Converters from '$lib/components/settings/Converters.svelte';
  import ChdMetadataScan from '$lib/components/settings/ChdMetadataScan.svelte';

  const SECTIONS = [
    { id: 'connections', title: 'Connections' },
    { id: 'identification', title: 'Game identification' },
    { id: 'notifications', title: 'Notifications' },
    { id: 'defaults', title: 'Defaults' },
    { id: 'converters', title: 'Converters' },
  ];

  $effect(() => {
    const section = ui.settingsSection;
    if (!section) return;
    tick().then(() => {
      document.getElementById(`settings-${section}`)?.scrollIntoView({ block: 'start' });
    });
  });
</script>

<section class="view" aria-labelledby="settings-title">
  <header class="header">
    <h1 id="settings-title">Settings</h1>
    <p class="hint">Set once. Everything here has a sensible default.</p>
  </header>

  <div class="layout">
    <nav class="jump" aria-label="Settings sections">
      {#each SECTIONS as s (s.id)}
        <a href={hashFor('settings', { tab: s.id })} aria-current={ui.settingsSection === s.id ? 'location' : undefined}>
          {s.title}
        </a>
      {/each}
    </nav>

    <div class="sections">
      {#each SECTIONS as s (s.id)}
        {#if s.id === 'notifications'}
          <!-- NotificationSettings brings its own card and heading. -->
          <div class="slot" id="settings-{s.id}">
            <NotificationSettings />
          </div>
        {:else}
          <section class="card" id="settings-{s.id}" aria-labelledby="settings-{s.id}-title">
            <h2 id="settings-{s.id}-title">{s.title}</h2>
            {#if s.id === 'connections'}
              <Connections />
            {:else if s.id === 'identification'}
              <Identification />
            {:else if s.id === 'defaults'}
              <Defaults />
            {:else}
              <Converters />
              <ChdMetadataScan />
            {/if}
          </section>
        {/if}
      {/each}
    </div>
  </div>
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
  .layout { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
  .jump { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-3); font-size: var(--text-sm); }
  .jump a { color: var(--accent); text-decoration: none; }
  .jump a:hover { text-decoration: underline; }
  .jump a[aria-current] { color: var(--text-1); font-weight: var(--weight-medium); }
  .sections { display: flex; flex-direction: column; gap: var(--space-5); min-width: 0; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-1);
    padding: var(--space-4);
    min-width: 0;
  }
  .card, .slot { scroll-margin-top: var(--space-4); }
  /* NotificationSettings sizes itself for a page of its own; fill the column. */
  .slot > :global(.card) { max-width: none; box-shadow: var(--elev-1); }
  .card h2 {
    margin: 0 0 var(--space-3);
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  /* Wide: the section list sits on the left and stays put while you scroll. */
  @media (min-width: 1100px) {
    .layout { display: grid; grid-template-columns: 180px minmax(0, 1fr); align-items: start; }
    .jump {
      position: sticky;
      top: var(--space-4);
      flex-direction: column;
      gap: var(--space-2);
    }
  }
</style>
