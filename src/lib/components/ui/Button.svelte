<script>
  /**
   * @typedef {Object} Props
   * @property {'primary'|'secondary'|'ghost'|'destructive'} [variant]
   * @property {'sm'|'md'|'lg'} [size]
   * @property {'button'|'submit'|'reset'} [type]
   * @property {boolean} [disabled]
   * @property {boolean} [loading]
   * @property {boolean} [fullWidth]
   * @property {string} [title]
   * @property {(e: MouseEvent) => void} [onclick]
   * @property {import('svelte').Snippet} [children]
   * @property {import('svelte').Snippet} [icon]
   */

  /** @type {Props} */
  let {
    variant = 'primary',
    size = 'md',
    type = 'button',
    disabled = false,
    loading = false,
    fullWidth = false,
    title,
    onclick,
    children,
    icon,
  } = $props();
</script>

<button
  {type}
  {title}
  class="btn {variant} size-{size}"
  class:full-width={fullWidth}
  class:loading
  disabled={disabled || loading}
  {onclick}
>
  {#if icon}<span class="icon">{@render icon()}</span>{/if}
  {#if children}<span class="label">{@render children()}</span>{/if}
</button>

<style>
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    border-radius: var(--radius-md);
    font-weight: var(--weight-medium);
    line-height: 1;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-out),
      color var(--dur-fast) var(--ease-out),
      box-shadow var(--dur-fast) var(--ease-out),
      transform var(--dur-fast) var(--ease-out);
    border: 1px solid transparent;
    white-space: nowrap;
    user-select: none;
  }
  .btn:active:not(:disabled) {
    transform: translateY(1px);
  }
  .btn:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  .full-width {
    width: 100%;
  }

  .size-sm {
    padding: 6px 10px;
    font-size: var(--text-sm);
  }
  .size-md {
    padding: 8px 14px;
    font-size: var(--text-base);
  }
  .size-lg {
    padding: 10px 18px;
    font-size: var(--text-lg);
  }

  .primary {
    background: var(--accent);
    color: var(--accent-contrast);
  }
  .primary:hover:not(:disabled) {
    background: var(--accent-hover);
  }

  .secondary {
    background: var(--surface-2);
    color: var(--text-1);
    border-color: var(--border-subtle);
  }
  .secondary:hover:not(:disabled) {
    background: var(--surface-3);
    border-color: var(--border-strong);
  }

  .ghost {
    background: transparent;
    color: var(--text-2);
  }
  .ghost:hover:not(:disabled) {
    background: var(--surface-2);
    color: var(--text-1);
  }

  .destructive {
    background: var(--error);
    color: var(--text-inverse);
  }
  .destructive:hover:not(:disabled) {
    background: var(--error);
    filter: brightness(1.1);
  }

  .icon {
    display: inline-flex;
    align-items: center;
  }
  .label {
    display: inline-flex;
    align-items: center;
  }
</style>
