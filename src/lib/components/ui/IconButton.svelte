<script>
  /**
   * Generic small icon button. Pass `toggle` for true on/off semantics so the
   * button reports aria-pressed; otherwise we omit it and `active` is purely
   * a visual style for "currently scoped" hints (e.g. selected nav item).
   *
   * @typedef {Object} Props
   * @property {string} label - aria-label for the button
   * @property {(e: MouseEvent) => void} [onclick]
   * @property {'sm'|'md'|'lg'} [size]
   * @property {boolean} [disabled]
   * @property {boolean} [active]
   * @property {boolean} [toggle]
   * @property {string} [title]
   * @property {import('svelte').Snippet} [children]
   */

  /** @type {Props} */
  let {
    label,
    onclick,
    size = 'md',
    disabled = false,
    active = false,
    toggle = false,
    title,
    children,
  } = $props();
</script>

<button
  type="button"
  class="icon-btn size-{size}"
  class:active
  aria-label={label}
  aria-pressed={toggle ? active : undefined}
  title={title ?? label}
  {disabled}
  {onclick}
>
  {#if children}{@render children()}{/if}
</button>

<style>
  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    color: var(--text-2);
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-out),
      color var(--dur-fast) var(--ease-out),
      border-color var(--dur-fast) var(--ease-out);
  }
  .icon-btn:hover:not(:disabled) {
    background: var(--surface-2);
    color: var(--text-1);
    border-color: var(--border-subtle);
  }
  .icon-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .active {
    background: var(--accent-muted);
    color: var(--accent);
  }
  .size-sm {
    width: 28px;
    height: 28px;
    font-size: 14px;
  }
  .size-md {
    width: 36px;
    height: 36px;
    font-size: 16px;
  }
  .size-lg {
    width: 44px;
    height: 44px;
    font-size: 18px;
  }
</style>
