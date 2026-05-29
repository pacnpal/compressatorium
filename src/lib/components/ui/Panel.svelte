<script>
  /**
   * @typedef {Object} Props
   * @property {string} [title]
   * @property {boolean} [padded]
   * @property {import('svelte').Snippet} [actions]
   * @property {import('svelte').Snippet} [children]
   */

  /** @type {Props} */
  let { title, padded = true, actions, children } = $props();
</script>

<article class="panel" class:padded>
  {#if title || actions}
    <header class="panel-head">
      {#if title}<h2 class="panel-title">{title}</h2>{/if}
      {#if actions}<div class="panel-actions">{@render actions()}</div>{/if}
    </header>
  {/if}
  {#if children}<div class="panel-body">{@render children()}</div>{/if}
</article>

<style>
  .panel {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-1);
    min-width: 0;
  }
  .panel.padded {
    padding: var(--space-4);
  }
  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    margin-bottom: var(--space-3);
  }
  .panel-title {
    margin: 0;
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .panel-actions {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }
  .panel-body {
    color: var(--text-1);
    min-width: 0;
  }
</style>
