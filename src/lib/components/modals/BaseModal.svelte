<script>
  // Shared Dialog chrome for every confirmation / info / form modal.
  // Owns header (title + close), description, body, footer; callers
  // hand it `open` + `onClose` + slot snippets for the body and footer.
  //
  // Built on bits-ui Dialog so we get focus-trap, Escape-to-close,
  // overlay-click-to-close, ARIA labelling, and Portal mounting for free.

  import { Dialog } from 'bits-ui';
  import XIcon from '@lucide/svelte/icons/x';

  /**
   * @typedef {Object} Props
   * @property {boolean} open
   * @property {() => void} onClose
   * @property {string} title
   * @property {string} [description]
   * @property {'sm'|'md'|'lg'} [size]
   * @property {import('svelte').Snippet} [titleIcon]
   * @property {import('svelte').Snippet} [body]
   * @property {import('svelte').Snippet} [footer]
   */

  /** @type {Props} */
  let { open, onClose, title, description, size = 'md', titleIcon, body, footer } = $props();

  function handleOpenChange(value) {
    if (!value) onClose();
  }
</script>

<Dialog.Root {open} onOpenChange={handleOpenChange}>
  <Dialog.Portal>
    <Dialog.Overlay class="mdl-overlay" />
    <Dialog.Content class="mdl-content mdl-size-{size}">
      <header class="mdl-header">
        <Dialog.Title class="mdl-title">
          {#if titleIcon}{@render titleIcon()}{/if}
          {title}
        </Dialog.Title>
        <Dialog.Close class="mdl-close" aria-label="Close">
          <XIcon size={16} />
        </Dialog.Close>
      </header>
      {#if description}
        <Dialog.Description class="mdl-desc">{description}</Dialog.Description>
      {/if}
      {#if body}<div class="mdl-body">{@render body()}</div>{/if}
      {#if footer}<footer class="mdl-footer">{@render footer()}</footer>{/if}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

<style>
  :global(.mdl-overlay) {
    position: fixed;
    inset: 0;
    background: var(--surface-overlay);
    z-index: var(--z-modal-backdrop);
  }
  :global(.mdl-content) {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    max-height: calc(100dvh - var(--space-6));
    overflow-y: auto;
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-3);
    padding: var(--space-5);
    z-index: var(--z-modal);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  :global(.mdl-size-sm) { width: min(420px, calc(100vw - var(--space-5))); }
  :global(.mdl-size-md) { width: min(560px, calc(100vw - var(--space-5))); }
  :global(.mdl-size-lg) { width: min(760px, calc(100vw - var(--space-5))); }
  :global(.mdl-header) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  :global(.mdl-title) {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  :global(.mdl-close) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--text-2);
    cursor: pointer;
  }
  :global(.mdl-close:hover) {
    background: var(--surface-2);
    color: var(--text-1);
    border-color: var(--border-subtle);
  }
  :global(.mdl-desc) {
    color: var(--text-2);
    font-size: var(--text-sm);
    margin: 0;
  }
  .mdl-body { color: var(--text-1); }
  :global(.mdl-footer) {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
</style>
