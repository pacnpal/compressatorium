<script>
  /**
   * @typedef {Object} Props
   * @property {'neutral'|'verified'|'dat-match'|'cd'|'dvd'|'archive'|'converting'|'success'|'warning'|'error'|'info'} [tone]
   * @property {'pill'|'square'} [shape]
   * @property {'sm'|'md'} [size]
   * @property {string} [title]
   * @property {import('svelte').Snippet} [children]
   */

  /** @type {Props} */
  let { tone = 'neutral', shape = 'pill', size = 'sm', title, children } = $props();
</script>

<span class="badge tone-{tone} shape-{shape} size-{size}" {title}>
  {#if children}{@render children()}{/if}
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-weight: var(--weight-semibold);
    line-height: 1.4;
    white-space: nowrap;
    background: var(--badge-bg);
    color: var(--badge-text);
  }
  .shape-pill {
    border-radius: var(--radius-full);
  }
  .shape-square {
    border-radius: var(--radius-sm);
  }
  .size-sm {
    font-size: var(--text-xs);
    padding: 2px 8px;
  }
  .size-md {
    font-size: var(--text-sm);
    padding: 3px 10px;
  }
  .tone-verified {
    background: color-mix(in srgb, var(--badge-verified) 16%, transparent);
    color: var(--badge-verified);
  }
  .tone-dat-match {
    background: color-mix(in srgb, var(--badge-dat-match) 16%, transparent);
    color: var(--badge-dat-match);
  }
  .tone-cd {
    background: color-mix(in srgb, var(--badge-cd) 16%, transparent);
    color: var(--badge-cd);
  }
  .tone-dvd {
    background: color-mix(in srgb, var(--badge-dvd) 16%, transparent);
    color: var(--badge-dvd);
  }
  .tone-archive {
    background: color-mix(in srgb, var(--badge-archive) 16%, transparent);
    color: var(--badge-archive);
  }
  .tone-converting {
    background: color-mix(in srgb, var(--badge-converting) 16%, transparent);
    color: var(--badge-converting);
  }
  .tone-success {
    background: var(--success-muted);
    color: var(--success);
  }
  .tone-warning {
    background: var(--warning-muted);
    color: var(--warning);
  }
  .tone-error {
    background: var(--error-muted);
    color: var(--error);
  }
  .tone-info {
    background: var(--info-muted);
    color: var(--info);
  }
</style>
