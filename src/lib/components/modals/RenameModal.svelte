<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { toast } from 'svelte-sonner';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Pencil from '@lucide/svelte/icons/pencil';

  const open = $derived(!!ui.renameTarget);
  const target = $derived(ui.renameTarget);

  let newName = $state('');
  let busy = $state(false);

  // Reset the input whenever the dialog opens with a new target. We
  // don't bind directly to `target.name` because the user is editing
  // a copy; closing without saving must leave the original untouched.
  $effect(() => {
    if (target?.name) newName = target.name;
  });

  function close() {
    if (busy) return;
    ui.renameTarget = null;
  }

  async function handleSubmit() {
    if (!target?.path) return;
    const trimmed = newName.trim();
    if (!trimmed || trimmed === target.name) {
      close();
      return;
    }
    busy = true;
    try {
      await api.renameFile(target.path, trimmed);
      toast.success(`Renamed to ${trimmed}`);
      await fileBrowser.refresh({ force: true });
      ui.renameTarget = null;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to rename');
    } finally {
      busy = false;
    }
  }

  function handleKeydown(e) {
    if (e.key === 'Enter' && !busy) {
      e.preventDefault();
      handleSubmit();
    }
  }
</script>

<BaseModal
  {open}
  onClose={close}
  title="Rename file"
  description={target ? `Renaming ${target.name}.` : ''}
  size="sm"
>
  {#snippet titleIcon()}<Pencil size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <label class="rn-label">
      <span class="rn-text">New name</span>
      <input
        type="text"
        class="rn-input"
        bind:value={newName}
        onkeydown={handleKeydown}
        autocomplete="off"
        spellcheck="false"
        aria-label="New file name"
      />
    </label>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={close} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={handleSubmit} disabled={busy || !newName.trim()} loading={busy}>
      Save
    </Button>
  {/snippet}
</BaseModal>

<style>
  .rn-label { display: flex; flex-direction: column; gap: var(--space-1); }
  .rn-text {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  .rn-input {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
    font-family: var(--font-mono);
  }
  .rn-input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
</style>
