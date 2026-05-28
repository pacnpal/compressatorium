<script>
  /**
   * @typedef {Object} Props
   * @property {boolean} checked
   * @property {string} label
   * @property {string} [description]
   * @property {boolean} [disabled]
   */

  /** @type {Props} */
  let { checked = $bindable(false), label, description, disabled = false } = $props();
</script>

<label class="checkbox" class:disabled>
  <input type="checkbox" bind:checked {disabled} />
  <span class="box" aria-hidden="true"></span>
  <span class="text">
    <span class="title">{label}</span>
    {#if description}<span class="desc">{description}</span>{/if}
  </span>
</label>

<style>
  .checkbox {
    display: inline-flex;
    align-items: flex-start;
    gap: var(--space-2);
    cursor: pointer;
    user-select: none;
    color: var(--text-1);
  }
  .checkbox input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
    width: 1px;
    height: 1px;
  }
  .box {
    width: 18px;
    height: 18px;
    border-radius: var(--radius-sm);
    border: 1.5px solid var(--border-strong);
    background: var(--surface-1);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: background var(--dur-fast) var(--ease-out),
      border-color var(--dur-fast) var(--ease-out);
    margin-top: 2px;
  }
  .checkbox input:checked + .box {
    background: var(--accent);
    border-color: var(--accent);
  }
  .checkbox input:checked + .box::after {
    content: '';
    width: 5px;
    height: 9px;
    border-right: 2px solid var(--accent-contrast);
    border-bottom: 2px solid var(--accent-contrast);
    transform: rotate(45deg) translate(-1px, -1px);
  }
  .checkbox input:focus-visible + .box {
    box-shadow: var(--focus-ring);
  }
  .text {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .title {
    font-size: var(--text-base);
    color: var(--text-1);
  }
  .desc {
    font-size: var(--text-sm);
    color: var(--text-2);
  }
  .disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
</style>
