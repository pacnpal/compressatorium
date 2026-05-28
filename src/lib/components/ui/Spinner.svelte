<script>
  /**
   * @typedef {Object} Props
   * @property {'sm'|'md'|'lg'} [size]
   * @property {string} [label]
   */

  /** @type {Props} */
  let { size = 'md', label = 'Loading' } = $props();
</script>

<span class="spinner size-{size}" role="status" aria-label={label}>
  <span class="ring"></span>
</span>

<style>
  .spinner {
    display: inline-flex;
  }
  .ring {
    display: inline-block;
    border-radius: 50%;
    border-style: solid;
    border-color: var(--surface-3);
    border-top-color: var(--accent);
    animation: spin 0.9s linear infinite;
  }
  .size-sm .ring {
    width: 14px;
    height: 14px;
    border-width: 2px;
  }
  .size-md .ring {
    width: 20px;
    height: 20px;
    border-width: 2.5px;
  }
  .size-lg .ring {
    width: 28px;
    height: 28px;
    border-width: 3px;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
