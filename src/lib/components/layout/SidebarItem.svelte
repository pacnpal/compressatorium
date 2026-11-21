<script>
  import Badge from '$lib/components/ui/Badge.svelte';

  /**
   * @typedef {Object} Props
   * @property {string} label
   * @property {boolean} [active]
   * @property {boolean} [collapsed]
   * @property {number|null} [badge] a count shown after the label (a dot when collapsed)
   * @property {'warning'|'info'} [badgeTone]
   * @property {string} [title]
   * @property {(e: MouseEvent) => void} [onclick]
   * @property {import('svelte').Snippet} [icon]
   */

  /** @type {Props} */
  let {
    label, active = false, collapsed = false, badge = null, badgeTone = 'info', title, onclick, icon,
  } = $props();
  const counted = $derived(typeof badge === 'number' && badge > 0);
  const name = $derived(counted ? `${label} (${badge})` : label);
</script>

<button
  type="button"
  class="sidebar-item"
  class:active
  class:collapsed
  title={title ?? name}
  aria-label={collapsed ? name : undefined}
  aria-current={active ? 'page' : undefined}
  {onclick}
>
  <span class="icon">
    {#if icon}{@render icon()}{:else}•{/if}
    {#if collapsed && counted}<span class="dot tone-{badgeTone}" aria-hidden="true"></span>{/if}
  </span>
  {#if !collapsed}
    <span class="label">{label}</span>
    {#if counted}<Badge tone={badgeTone}>{badge}</Badge>{/if}
  {/if}
</button>

<style>
  .sidebar-item {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    color: var(--text-2);
    background: transparent;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: var(--text-base);
    text-align: left;
    transition: background var(--dur-fast) var(--ease-out),
      color var(--dur-fast) var(--ease-out);
  }
  .sidebar-item:hover {
    background: var(--surface-2);
    color: var(--text-1);
  }
  .active {
    background: var(--accent-muted);
    color: var(--accent);
    font-weight: var(--weight-medium);
  }
  .icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    flex-shrink: 0;
    font-size: 16px;
  }
  .label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .icon { position: relative; }
  .dot {
    position: absolute;
    top: 0;
    right: 0;
    width: 8px;
    height: 8px;
    border-radius: var(--radius-full);
    border: 2px solid var(--surface-1);
  }
  .dot.tone-warning { background: var(--warning); }
  .dot.tone-info { background: var(--info); }
  .collapsed {
    justify-content: center;
    padding: var(--space-2);
  }
</style>
