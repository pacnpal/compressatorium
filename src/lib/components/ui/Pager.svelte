<script>
  import IconButton from './IconButton.svelte';

  /**
   * @typedef {Object} Props
   * @property {number} page
   * @property {number} pageCount
   * @property {(page: number) => void} onpage
   */

  /** @type {Props} */
  let { page, pageCount, onpage } = $props();

  const disabledPrev = $derived(page <= 1);
  const disabledNext = $derived(page >= pageCount);
</script>

{#if pageCount > 1}
  <nav class="pager" aria-label="Pagination">
    <IconButton label="First page" size="sm" disabled={disabledPrev} onclick={() => onpage(1)}>
      «
    </IconButton>
    <IconButton label="Previous page" size="sm" disabled={disabledPrev} onclick={() => onpage(page - 1)}>
      ‹
    </IconButton>
    <span class="status">Page {page} of {pageCount}</span>
    <IconButton label="Next page" size="sm" disabled={disabledNext} onclick={() => onpage(page + 1)}>
      ›
    </IconButton>
    <IconButton label="Last page" size="sm" disabled={disabledNext} onclick={() => onpage(pageCount)}>
      »
    </IconButton>
  </nav>
{/if}

<style>
  .pager {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }
  .status {
    padding: 0 var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-2);
  }
</style>
