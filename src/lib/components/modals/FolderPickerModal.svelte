<script>
  /**
   * FolderPicker in a dialog. Closing it (Escape, the ×, Cancel) calls
   * `onClose`; picking calls `onpick(path)` and leaves closing to the caller,
   * which may want to keep it open to show an error under the picker.
   *
   * @typedef {Object} Props
   * @property {boolean} open
   * @property {string} title
   * @property {() => void} onClose
   * @property {(path: string) => void} onpick
   * @property {string|null} [start]
   * @property {string} [confirmLabel]
   * @property {string[]} [disabledPaths]
   * @property {string} [error] shown under the picker (the backend's detail)
   * @property {import('svelte').Snippet} [extra] fields under the picker
   */
  import BaseModal from './BaseModal.svelte';
  import FolderPicker from '$lib/components/ui/FolderPicker.svelte';

  /** @type {Props} */
  let {
    open, title, onClose, onpick, start = null, confirmLabel = 'Use this folder',
    disabledPaths = [], error = '', extra,
  } = $props();
</script>

<BaseModal {open} {onClose} {title} size="md">
  {#snippet body()}
    <div class="picker-body">
      <FolderPicker {start} {onpick} onCancel={onClose} {confirmLabel} {disabledPaths} />
      {#if extra}{@render extra()}{/if}
      {#if error}<p class="error" role="alert">{error}</p>{/if}
    </div>
  {/snippet}
</BaseModal>

<style>
  .picker-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .error {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    background: var(--error-muted);
    color: var(--text-1);
    font-size: var(--text-sm);
  }
</style>
