<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { registry } from '$lib/tools/registry.js';
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
      // Backend returns { success, old_path, new_path, message } and
      // also moves the verification record server-side for .chd → .chd
      // renames (app/routes/files.py rename_file). Mirror that move in
      // our local verification.statuses so the OK badge survives the
      // rename without waiting for a verified-set reload.
      const result = await api.renameFile(target.path, trimmed);
      const oldPath = result?.old_path ?? target.path;
      const newPath = result?.new_path;
      if (newPath && verification.statuses.has(oldPath)) {
        verification.statuses.delete(oldPath);
        // Preserve the OK badge across renames inside the same
        // verify-class (e.g. .rvz → .rvz, .chd → .chd, .z3ds → .z3ds).
        // Cross-class renames (.chd → .iso, .rvz → .iso) drop the
        // badge because the new file is no longer the verifiable
        // product. Backend's verification_store only moves the
        // record for any verify-class extension AS LONG AS it stays
        // in the same format, `.chd → .chd`, `.rvz → .rvz`, etc.
        // Cross-format renames (`.chd → .rvz`) clear the badge
        // because the file's actual content wasn't reverified under
        // its new format; carrying the OK would mislead the user.
        // Same rule the backend now applies in files.py rename_file.
        const oldExt = oldPath.toLowerCase().match(/\.[^./\\]+$/)?.[0] ?? '';
        const newExt = newPath.toLowerCase().match(/\.[^./\\]+$/)?.[0] ?? '';
        const oldTool = registry.toolForVerifyPath(oldPath);
        const newTool = registry.toolForVerifyPath(newPath);
        if (oldTool && newTool && oldTool.id === newTool.id && oldExt === newExt) {
          verification.statuses.add(newPath);
        }
      }
      // Re-key the selection if the renamed file was selected.
      // Otherwise the selection-count + ConvertPanel would keep
      // pointing at the now-nonexistent old path and the next batch
      // submission would skip/fail the stale input.
      if (newPath && fileBrowser.selectedFiles.has(oldPath)) {
        const existing = fileBrowser.selectedFiles.get(oldPath);
        fileBrowser.selectedFiles.delete(oldPath);
        fileBrowser.selectedFiles.set(newPath, {
          ...(existing ?? {}),
          path: newPath,
          name: trimmed,
        });
      }
      ui.renameTarget = null;
      toast.success(`Renamed to ${trimmed}`);
      // Refresh is a best-effort follow-up. A refresh failure here
      // must not surface as "Failed to rename", the rename already
      // succeeded server-side.
      try {
        await fileBrowser.refresh({ force: true });
      } catch (_e) {
        toast.warning('Rename succeeded; refreshing the listing failed');
      }
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
