<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import SidebarItem from './SidebarItem.svelte';
  import FolderOpen from '@lucide/svelte/icons/folder-open';
  import Library from '@lucide/svelte/icons/library';
  import Activity from '@lucide/svelte/icons/activity';
  import Settings from '@lucide/svelte/icons/settings';
  import HelpCircle from '@lucide/svelte/icons/circle-help';
  import ChevronLeft from '@lucide/svelte/icons/chevron-left';
  import ChevronRight from '@lucide/svelte/icons/chevron-right';

  const collapsed = $derived(ui.sidebarCollapsed);
  const view = $derived(ui.activeView);
  // Activity's badge: what needs you first, else what is running or waiting.
  const attention = $derived(findings.openCount);
  const running = $derived(jobs.activeCount);
  const activityBadge = $derived(attention > 0 ? attention : running > 0 ? running : null);
  const activityTone = $derived(attention > 0 ? 'warning' : 'info');

  const PLACES = [
    { view: 'files', label: 'Files', icon: FolderOpen },
    { view: 'libraries', label: 'Libraries', icon: Library },
    { view: 'activity', label: 'Activity', icon: Activity },
    { view: 'settings', label: 'Settings', icon: Settings },
  ];

  function go(target) {
    ui.closeDrawer();
    ui.navigate(target);
  }
</script>

<aside class="sidebar" class:collapsed aria-label="Primary navigation">
  <div class="brand">
    <img class="brand-logo" src="/static/images/logo.png" alt="" aria-hidden="true" width="32" height="32" />
    {#if !collapsed}<span class="brand-text">Compressatorium</span>{/if}
  </div>

  <nav class="section" aria-label="Places">
    {#each PLACES as place (place.view)}
      <SidebarItem
        label={place.label}
        {collapsed}
        active={view === place.view}
        badge={place.view === 'activity' ? activityBadge : null}
        badgeTone={activityTone}
        onclick={() => go(place.view)}
      >
        {#snippet icon()}<place.icon size={16} />{/snippet}
      </SidebarItem>
    {/each}
  </nav>

  <div class="section footer">
    <SidebarItem label="Help" {collapsed} active={view === 'help'} onclick={() => go('help')}>
      {#snippet icon()}<HelpCircle size={16} />{/snippet}
    </SidebarItem>
    <SidebarItem
      label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      {collapsed}
      onclick={() => ui.toggleSidebar()}
    >
      {#snippet icon()}
        {#if collapsed}<ChevronRight size={16} />{:else}<ChevronLeft size={16} />{/if}
      {/snippet}
    </SidebarItem>
  </div>
</aside>

<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    width: var(--sidebar-width);
    /* Full height on its own, so its background never stops short of a long page. */
    position: sticky;
    top: 0;
    height: 100dvh;
    padding: var(--space-3);
    background: var(--surface-1);
    border-right: 1px solid var(--border-subtle);
    overflow-y: auto;
    transition: width var(--dur-base) var(--ease-out);
  }
  .collapsed {
    width: var(--sidebar-collapsed-width);
    padding: var(--space-3) var(--space-2);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2);
    color: var(--text-1);
  }
  .brand-logo {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-sm);
    flex-shrink: 0;
    object-fit: contain;
  }
  .brand-text {
    font-weight: var(--weight-semibold);
    font-size: var(--text-lg);
    flex-shrink: 0;
    white-space: nowrap;
  }
  .collapsed .brand { justify-content: center; padding: var(--space-1); }
  .section { display: flex; flex-direction: column; gap: 2px; }
  .footer {
    margin-top: auto;
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-2);
  }
</style>
