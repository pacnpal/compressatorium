<script>
  import { onMount, tick } from 'svelte';
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
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { jobs } from '$lib/stores/jobs.svelte.js';

  let mediaQuery = null;
  /** @type {HTMLElement | undefined} */
  let mainEl;

  function onSystemChange(ev) {
    ui.systemIsDark = ev.matches;
    if (ui.theme === 'system') ui.applyTheme();
  }

  // Move keyboard focus to the main landmark whenever the route changes,
  // so screen readers / keyboard users don't lose context. Triggered by
  // ui.focusBump (incremented in ui.applyHash) rather than reading the
  // URL directly so it cleanly captures both initial mount and back/forward.
  $effect(() => {
    ui.focusBump;
    if (!mainEl) return;
    tick().then(() => {
      try {
        mainEl.focus({ preventScroll: false });
      } catch (_e) {
        // ignore — focus may fail on disconnected nodes mid-route swap
      }
    });
  });

  function handleSkip(e) {
    e.preventDefault();
    mainEl?.focus({ preventScroll: false });
  }

  function onBoundaryError(err, reset) {
    console.error('View boundary caught error:', err);
    ui.notify('Something went wrong. Try again.', 'error', 6000);
    reset();
  }

  onMount(() => {
    ui.applyTheme();
    ui.loadVersion();
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

<a class="skip-link" href="#main-content" onclick={handleSkip}>Skip to main content</a>

<div class="shell">
  <div class="sidebar-host">
    <Sidebar />
  </div>
  <MobileDrawer />

  <div class="main-col">
    <TopBar />
    <main
      id="main-content"
      bind:this={mainEl}
      class="main"
      tabindex="-1"
      aria-label="Main content"
    >
      <svelte:boundary onerror={onBoundaryError}>
        {#if ui.activeView === 'dashboard'}<Dashboard />
        {:else if ui.activeView === 'workspace'}<WorkArea />
        {:else if ui.activeView === 'dat'}<DATView />
        {:else if ui.activeView === 'help'}<HelpView />
        {/if}

        {#snippet failed(error, reset)}
          <div class="error-frame">
            <EmptyState
              title="This view crashed"
              description={error?.message ?? 'An unexpected error occurred while rendering.'}
              glyph="!"
            >
              {#snippet actions()}
                <Button variant="primary" onclick={reset}>Try again</Button>
              {/snippet}
            </EmptyState>
          </div>
        {/snippet}
      </svelte:boundary>
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
    outline: none;
  }
  .main:focus-visible {
    box-shadow: inset 0 0 0 3px var(--accent);
  }

  /* Skip link: visually hidden until focused. Activated by Tab from the
     document top, this lets keyboard users jump straight to <main>
     without traversing the sidebar nav on every page load. */
  .skip-link {
    position: fixed;
    top: var(--space-2);
    left: var(--space-2);
    z-index: calc(var(--z-notification) + 1);
    padding: var(--space-2) var(--space-4);
    background: var(--accent);
    color: var(--accent-contrast);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
    font-weight: var(--weight-semibold);
    text-decoration: none;
    transform: translateY(-150%);
    transition: transform var(--dur-base) var(--ease-out);
  }
  .skip-link:focus-visible {
    transform: translateY(0);
    box-shadow: var(--elev-3);
  }

  .error-frame {
    padding: var(--space-6);
  }
</style>
