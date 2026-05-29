<script>
  // Generic dashboard tile. Title + (optional) icon, then body slot for
  // anything — a big number, a list, a progress bar. Footer slot for an
  // action or hint.

  /**
   * @typedef {Object} Props
   * @property {string} title
   * @property {string} [subtitle]
   * @property {import('svelte').Snippet} [icon]
   * @property {import('svelte').Snippet} [body]
   * @property {import('svelte').Snippet} [footer]
   * @property {string} [accent]
   */

  /** @type {Props} */
  let { title, subtitle, icon, body, footer, accent } = $props();
</script>

<article class="stat-card" style:--card-accent={accent ?? 'var(--accent)'}>
  <header class="card-head">
    {#if icon}<span class="card-icon">{@render icon()}</span>{/if}
    <div class="card-titles">
      <h3 class="card-title">{title}</h3>
      {#if subtitle}<p class="card-sub">{subtitle}</p>{/if}
    </div>
  </header>
  {#if body}<div class="card-body">{@render body()}</div>{/if}
  {#if footer}<footer class="card-footer">{@render footer()}</footer>{/if}
</article>

<style>
  .stat-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
    min-width: 0;
    border-top: 3px solid var(--card-accent);
  }
  .card-head { display: flex; align-items: center; gap: var(--space-2); }
  .card-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    background: color-mix(in srgb, var(--card-accent) 14%, transparent);
    color: var(--card-accent);
    border-radius: var(--radius-sm);
    flex-shrink: 0;
  }
  .card-titles { flex: 1; min-width: 0; }
  .card-title {
    margin: 0;
    font-size: var(--text-xs);
    font-weight: var(--weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
  }
  .card-sub { margin: 0; color: var(--text-3); font-size: var(--text-xs); }
  .card-body { color: var(--text-1); min-width: 0; }
  .card-footer { color: var(--text-3); font-size: var(--text-xs); }
</style>
