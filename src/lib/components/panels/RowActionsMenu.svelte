<script>
  // Per-row file actions menu. Uses bits-ui DropdownMenu so we get a
  // keyboard-accessible, ARIA-correct popover for free; we only style.
  //
  // The visible actions depend on the file's verify/source classification
  // — the registry tells us which tool owns the path (for verify) and
  // whether any tool can convert from it (gating Info/Verify on the
  // current row).

  import { DropdownMenu } from 'bits-ui';
  import { registry } from '$lib/tools/registry.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { toast } from 'svelte-sonner';
  import MoreHorizontal from '@lucide/svelte/icons/ellipsis';
  import Info from '@lucide/svelte/icons/info';
  import ShieldCheck from '@lucide/svelte/icons/shield-check';
  import Pencil from '@lucide/svelte/icons/pencil';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  /** @type {{ entry: any }} */
  let { entry } = $props();

  const path = $derived(entry?.path ?? '');
  const inArchive = $derived(typeof path === 'string' && path.includes('::'));

  // Which tool (if any) owns this file as a verifiable output?
  const verifyTool = $derived(path ? registry.toolForVerifyPath(path) : null);
  const canVerify = $derived(!!verifyTool && !inArchive);
  const canGetInfo = $derived(!!verifyTool && !inArchive);
  // Rename / Delete operate on the filesystem — archive members can't be
  // renamed or deleted in-place, so disable them inside archive views.
  const canRename = $derived(!inArchive);
  const canDelete = $derived(!inArchive);

  async function handleVerify() {
    if (!verifyTool) return;
    try {
      const result = await verification.verifyOne(verifyTool.id, path);
      if (result?.valid) {
        toast.success(`Verified: ${entry?.name ?? path}`);
      } else {
        toast.error(`Verification failed: ${result?.message ?? entry?.name}`);
      }
    } catch (e) {
      toast.error(e?.message ?? 'Verify failed');
    }
  }

  function handleInfo() {
    ui.chdInfoTarget = entry;
  }

  function handleRename() {
    ui.renameTarget = entry;
  }

  function handleDelete() {
    ui.deleteTarget = entry;
  }
</script>

<DropdownMenu.Root>
  <DropdownMenu.Trigger
    class="actions-trigger"
    aria-label={`Actions for ${entry?.name ?? 'file'}`}
    title="More actions"
  >
    <MoreHorizontal size={14} aria-hidden="true" />
  </DropdownMenu.Trigger>

  <DropdownMenu.Portal>
    <DropdownMenu.Content class="row-actions-content" align="end" sideOffset={4}>
      <DropdownMenu.Item
        class="row-actions-item"
        disabled={!canGetInfo}
        onSelect={handleInfo}
      >
        <Info size={14} aria-hidden="true" />
        <span>Info</span>
      </DropdownMenu.Item>
      <DropdownMenu.Item
        class="row-actions-item"
        disabled={!canVerify}
        onSelect={handleVerify}
      >
        <ShieldCheck size={14} aria-hidden="true" />
        <span>Verify</span>
      </DropdownMenu.Item>
      <DropdownMenu.Separator class="row-actions-sep" />
      <DropdownMenu.Item
        class="row-actions-item"
        disabled={!canRename}
        onSelect={handleRename}
      >
        <Pencil size={14} aria-hidden="true" />
        <span>Rename</span>
      </DropdownMenu.Item>
      <DropdownMenu.Item
        class="row-actions-item danger"
        disabled={!canDelete}
        onSelect={handleDelete}
      >
        <Trash2 size={14} aria-hidden="true" />
        <span>Delete</span>
      </DropdownMenu.Item>
    </DropdownMenu.Content>
  </DropdownMenu.Portal>
</DropdownMenu.Root>

<style>
  :global(.actions-trigger) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--text-2);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-out);
  }
  :global(.actions-trigger:hover) {
    background: var(--surface-3);
    color: var(--text-1);
  }
  :global(.actions-trigger:focus-visible) {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  :global(.row-actions-content) {
    min-width: 180px;
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    box-shadow: var(--elev-2);
    padding: var(--space-1);
    z-index: var(--z-modal);
  }
  :global(.row-actions-item) {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: 6px var(--space-2);
    color: var(--text-1);
    font-size: var(--text-sm);
    border-radius: var(--radius-sm);
    cursor: pointer;
    user-select: none;
    outline: none;
  }
  :global(.row-actions-item[data-highlighted]) {
    background: var(--accent-muted);
    color: var(--accent);
  }
  :global(.row-actions-item[data-disabled]) {
    opacity: 0.5;
    cursor: not-allowed;
  }
  :global(.row-actions-item.danger) { color: var(--error); }
  :global(.row-actions-item.danger[data-highlighted]) {
    background: var(--error-muted);
    color: var(--error);
  }
  :global(.row-actions-sep) {
    height: 1px;
    background: var(--border-subtle);
    margin: var(--space-1) 0;
  }
</style>
