<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import Sidebar from './Sidebar.svelte';

  const open = $derived(ui.mobileDrawerOpen);

  function handleKey(e) {
    if (e.key === 'Escape' && open) ui.closeDrawer();
  }
</script>

<svelte:window onkeydown={handleKey} />

{#if open}
  <div
    class="backdrop"
    role="presentation"
    onclick={() => ui.closeDrawer()}
  ></div>
{/if}

<div class="drawer" class:open aria-hidden={!open}>
  <Sidebar />
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: var(--surface-overlay);
    z-index: var(--z-drawer);
  }
  .drawer {
    position: fixed;
    top: 0;
    left: 0;
    height: 100dvh;
    z-index: calc(var(--z-drawer) + 1);
    transform: translateX(-100%);
    transition: transform var(--dur-base) var(--ease-out);
    box-shadow: var(--elev-3);
  }
  .drawer.open {
    transform: translateX(0);
  }
  @media (min-width: 900px) {
    .backdrop,
    .drawer {
      display: none;
    }
  }
</style>
