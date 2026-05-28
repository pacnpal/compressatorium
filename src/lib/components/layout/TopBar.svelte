<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import ThemeToggle from './ThemeToggle.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';

  function titleFor(view) {
    switch (view) {
      case 'dashboard':
        return 'Dashboard';
      case 'workspace':
        return 'Workspace';
      case 'dat':
        return 'DAT Library';
      case 'help':
        return 'Help';
      default:
        return '';
    }
  }

  const version = $derived(ui.appVersion);
  const view = $derived(ui.activeView);
  const title = $derived(titleFor(view));
</script>

<header class="topbar">
  <div class="left">
    <IconButton
      label="Open menu"
      size="md"
      onclick={() => ui.openDrawer()}
    >
      <span class="hamburger">≡</span>
    </IconButton>
    <span class="view-title">{title}</span>
  </div>

  <div class="right">
    {#if version}<span class="version" title="Application version">v{version}</span>{/if}
    <ThemeToggle />
  </div>
</header>

<style>
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: var(--topbar-height);
    padding: 0 var(--space-4);
    background: var(--surface-1);
    border-bottom: 1px solid var(--border-subtle);
    position: sticky;
    top: 0;
    z-index: var(--z-topbar);
  }
  .left,
  .right {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .view-title {
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .version {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-3);
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    background: var(--surface-2);
  }
  .hamburger {
    font-size: 22px;
    line-height: 1;
  }
  @media (min-width: 900px) {
    .topbar :global(.icon-btn[aria-label='Open menu']) {
      display: none;
    }
  }
</style>
