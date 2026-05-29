<script>
  /**
   * @typedef {Object} Props
   * @property {string} title
   * @property {string} [description]
   * @property {string} [glyph]
   * @property {import('svelte').Snippet} [actions]
   */

  /** @type {Props} */
  let { title, description, glyph = '∅', actions } = $props();
</script>

<div class="empty" role="status">
  <span class="glyph" aria-hidden="true">{glyph}</span>
  <h3>{title}</h3>
  {#if description}<p>{description}</p>{/if}
  {#if actions}<div class="actions">{@render actions()}</div>{/if}
</div>

<style>
  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    padding: var(--space-7) var(--space-4);
    text-align: center;
    color: var(--text-2);
  }
  .glyph {
    font-size: 36px;
    color: var(--text-3);
  }
  h3 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  p {
    margin: 0;
    color: var(--text-2);
    max-width: 36ch;
  }
  .actions {
    margin-top: var(--space-3);
    display: flex;
    gap: var(--space-2);
  }
</style>
