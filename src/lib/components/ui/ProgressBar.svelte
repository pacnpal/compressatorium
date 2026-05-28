<script>
  /**
   * @typedef {Object} Props
   * @property {number|null} [value] - 0-100; null = indeterminate
   * @property {'sm'|'md'|'lg'} [size]
   * @property {'accent'|'success'|'warning'|'error'} [tone]
   * @property {string} [label]
   */

  /** @type {Props} */
  let { value, size = 'md', tone = 'accent', label = 'Progress' } = $props();

  const indeterminate = $derived(value == null);
  const clamped = $derived(
    typeof value === 'number' ? Math.max(0, Math.min(100, value)) : 0,
  );
</script>

<div
  class="track size-{size} tone-{tone}"
  role="progressbar"
  aria-valuenow={indeterminate ? undefined : clamped}
  aria-valuemin={0}
  aria-valuemax={100}
  aria-label={label}
>
  <div class="fill" class:indeterminate style:width={indeterminate ? '40%' : `${clamped}%`}></div>
</div>

<style>
  .track {
    position: relative;
    width: 100%;
    background: var(--surface-3);
    border-radius: var(--radius-full);
    overflow: hidden;
  }
  .size-sm {
    height: 4px;
  }
  .size-md {
    height: 8px;
  }
  .size-lg {
    height: 12px;
  }
  .fill {
    height: 100%;
    background: var(--accent);
    border-radius: inherit;
    transition: width var(--dur-base) var(--ease-out);
  }
  .tone-success .fill {
    background: var(--success);
  }
  .tone-warning .fill {
    background: var(--warning);
  }
  .tone-error .fill {
    background: var(--error);
  }
  .indeterminate {
    animation: slide 1.4s var(--ease-out) infinite;
  }
  @keyframes slide {
    0% {
      transform: translateX(-100%);
    }
    100% {
      transform: translateX(250%);
    }
  }
</style>
