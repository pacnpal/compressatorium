<script>
  /**
   * @typedef {Object} Props
   * @property {string} label
   * @property {boolean} [active]
   * @property {boolean} [collapsed]
   * @property {string} [badge]
   * @property {string} [title]
   * @property {(e: MouseEvent) => void} [onclick]
   * @property {import('svelte').Snippet} [icon]
   */

  /** @type {Props} */
  let { label, active = false, collapsed = false, badge, title, onclick, icon } = $props();
</script>

<button
  type="button"
  class="sidebar-item"
  class:active
  class:collapsed
  title={title ?? label}
  aria-current={active ? 'page' : undefined}
  {onclick}
>
  <span class="icon">{#if icon}{@render icon()}{:else}•{/if}</span>
  {#if !collapsed}
    <span class="label">{label}</span>
    {#if badge}<span class="badge">{badge}</span>{/if}
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
  .badge {
    background: var(--badge-bg);
    color: var(--badge-text);
    border-radius: var(--radius-full);
    padding: 2px 8px;
    font-size: var(--text-xs);
    font-weight: var(--weight-semibold);
    line-height: 1.4;
  }
  .active .badge {
    background: var(--accent);
    color: var(--accent-contrast);
  }
  .collapsed {
    justify-content: center;
    padding: var(--space-2);
  }
</style>
