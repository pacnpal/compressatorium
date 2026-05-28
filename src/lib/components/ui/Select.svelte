<script>
  /**
   * @typedef {Object} Option
   * @property {string} value
   * @property {string} label
   *
   * @typedef {Object} Props
   * @property {string} value
   * @property {Option[]} options
   * @property {string} [label]
   * @property {boolean} [disabled]
   * @property {(value: string) => void} [onchange]
   */

  /** @type {Props} */
  let { value = $bindable(), options, label, disabled = false, onchange } = $props();

  function handle(e) {
    value = e.currentTarget.value;
    onchange?.(value);
  }
</script>

<label class="select" class:disabled>
  {#if label}<span class="label">{label}</span>{/if}
  <select bind:value {disabled} onchange={handle}>
    {#each options as opt (opt.value)}
      <option value={opt.value}>{opt.label}</option>
    {/each}
  </select>
</label>

<style>
  .select {
    display: inline-flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }
  .label {
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  select {
    background: var(--surface-2);
    color: var(--text-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: 8px 12px;
    font-size: var(--text-base);
    line-height: 1;
    cursor: pointer;
    appearance: none;
    transition: border-color var(--dur-fast) var(--ease-out);
  }
  select:hover {
    border-color: var(--border-strong);
  }
  select:focus-visible {
    outline: none;
    border-color: var(--accent);
  }
  .disabled select {
    opacity: 0.55;
    cursor: not-allowed;
  }
</style>
