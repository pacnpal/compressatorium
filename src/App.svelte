<script>
  import { onMount } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { startRouter } from '$lib/router/hashRouter.js';
  import Sidebar from '$lib/components/layout/Sidebar.svelte';
  import MobileDrawer from '$lib/components/layout/MobileDrawer.svelte';
  import TopBar from '$lib/components/layout/TopBar.svelte';
  import Dashboard from '$lib/components/views/Dashboard.svelte';
  import WorkArea from '$lib/components/views/WorkArea.svelte';
  import DATView from '$lib/components/views/DATView.svelte';
  import HelpView from '$lib/components/views/HelpView.svelte';
  import Notification from '$lib/components/ui/Notification.svelte';
  import { jobs } from '$lib/stores/jobs.svelte.js';

  let mediaQuery = null;

  function onSystemChange(ev) {
    ui.systemIsDark = ev.matches;
    if (ui.theme === 'system') ui.applyTheme();
  }

  onMount(() => {
    ui.applyTheme();
    ui.loadVersion();
    // SSE hydrates the full job list via a one-time `snapshot` event
    // emitted by the backend at connection time (and re-emitted after
    // each reconnect). No separate /api/jobs round-trip needed; the
    // refresh() method remains available for explicit manual recovery.
    jobs.connect();
    const stopRouter = startRouter();

    if (window.matchMedia) {
      mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
      mediaQuery.addEventListener('change', onSystemChange);
    }

    return () => {
      stopRouter();
      jobs.dispose();
      mediaQuery?.removeEventListener('change', onSystemChange);
    };
  });
</script>

<div class="shell">
  <div class="sidebar-host">
    <Sidebar />
  </div>
  <MobileDrawer />

  <div class="main-col">
    <TopBar />
    <main class="main">
      {#if ui.activeView === 'dashboard'}<Dashboard />
      {:else if ui.activeView === 'workspace'}<WorkArea />
      {:else if ui.activeView === 'dat'}<DATView />
      {:else if ui.activeView === 'help'}<HelpView />
      {/if}
    </main>
  </div>

  <Notification />
</div>

<style>
  .shell {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    height: 100dvh;
    background: var(--surface-2);
  }
  .sidebar-host {
    display: none;
  }
  @media (min-width: 900px) {
    .sidebar-host {
      display: block;
    }
  }
  .main-col {
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--surface-2);
  }
  .main {
    flex: 1;
    overflow-y: auto;
  }
</style>
