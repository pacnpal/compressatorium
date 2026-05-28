<script>
  // Generic confirm / cancel dialog used by Cancel-all / Clear-completed
  // / Delete. Wraps BaseModal; the message goes in either the
  // `description` prop or the `body` snippet (use the snippet when you
  // need to render structured content like a file list).

  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';

  /**
   * @typedef {Object} Props
   * @property {boolean} open
   * @property {() => void} onClose
   * @property {() => Promise<void>|void} onConfirm
   * @property {string} title
   * @property {string} [description]
   * @property {string} [confirmLabel]
   * @property {string} [cancelLabel]
   * @property {'primary'|'destructive'} [confirmVariant]
   * @property {boolean} [busy]
   * @property {import('svelte').Snippet} [titleIcon]
   * @property {import('svelte').Snippet} [body]
   */

  /** @type {Props} */
  let {
    open,
    onClose,
    onConfirm,
    title,
    description,
    confirmLabel = 'Confirm',
    cancelLabel = 'Cancel',
    confirmVariant = 'primary',
    busy = false,
    titleIcon,
    body: bodySlot,
  } = $props();

  async function handleConfirm() {
    await onConfirm();
  }
</script>

<BaseModal {open} {onClose} {title} {description} size="sm" {titleIcon}>
  {#snippet body()}
    {#if bodySlot}{@render bodySlot()}{/if}
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={onClose} disabled={busy}>{cancelLabel}</Button>
    <Button variant={confirmVariant} onclick={handleConfirm} disabled={busy} loading={busy}>
      {confirmLabel}
    </Button>
  {/snippet}
</BaseModal>
