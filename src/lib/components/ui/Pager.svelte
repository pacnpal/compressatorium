<script>
  import IconButton from './IconButton.svelte';
  import ChevronsLeft from '@lucide/svelte/icons/chevrons-left';
  import ChevronLeft from '@lucide/svelte/icons/chevron-left';
  import ChevronRight from '@lucide/svelte/icons/chevron-right';
  import ChevronsRight from '@lucide/svelte/icons/chevrons-right';

  /** @type {{page: number, pageCount: number, onpage: (p: number) => void}} */
  let { page, pageCount, onpage } = $props();

  const disabledPrev = $derived(page <= 1);
  const disabledNext = $derived(page >= pageCount);
</script>

{#if pageCount > 1}
  <nav class="pager" aria-label="Pagination">
    <IconButton label="First page" size="sm" disabled={disabledPrev} onclick={() => onpage(1)}>
      <ChevronsLeft size={14} />
    </IconButton>
    <IconButton label="Previous page" size="sm" disabled={disabledPrev} onclick={() => onpage(page - 1)}>
      <ChevronLeft size={14} />
    </IconButton>
    <span class="status">Page {page} of {pageCount}</span>
    <IconButton label="Next page" size="sm" disabled={disabledNext} onclick={() => onpage(page + 1)}>
      <ChevronRight size={14} />
    </IconButton>
    <IconButton label="Last page" size="sm" disabled={disabledNext} onclick={() => onpage(pageCount)}>
      <ChevronsRight size={14} />
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
