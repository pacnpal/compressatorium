<script>
  // Per-row file actions menu. Uses bits-ui DropdownMenu so we get a
  // keyboard-accessible, ARIA-correct popover for free; we only style.
  //
  // The visible actions depend on the file's verify/source classification,
  // the registry tells us which tool owns the path (for verify) and
  // whether any tool can convert from it (gating Info/Verify on the
  // current row).

  import { DropdownMenu } from 'bits-ui';
  import { registry } from '$lib/tools/registry.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import { toast } from 'svelte-sonner';
  import MoreHorizontal from '@lucide/svelte/icons/ellipsis';
  import Info from '@lucide/svelte/icons/info';
  import ShieldCheck from '@lucide/svelte/icons/shield-check';
  import Pencil from '@lucide/svelte/icons/pencil';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import FolderCog from '@lucide/svelte/icons/folder-cog';
  import FolderPen from '@lucide/svelte/icons/folder-pen';
  import Bandage from '@lucide/svelte/icons/bandage';
  import { canPatch } from '$lib/util/patchDialog.js';

  /** @type {{ entry: any }} */
  let { entry } = $props();

  const path = $derived(entry?.path ?? '');
  const inArchive = $derived(typeof path === 'string' && path.includes('::'));
  const isArchive = $derived(entry?.type === 'archive');
  const outputs = $derived(Array.isArray(entry?.outputs) ? entry.outputs : []);

  // Direct verify target for this row, if any (tool that owns the path as a
  // verify-class output, .chd, .rvz, .z3ds, etc.), narrowed by the backend's
  // per-file `verifiable_by` (romz only claims single-ROM .7z/.zip archives).
  // See registry.verifyToolForPath for the precedence rule.
  const directVerifyTool = $derived(
    registry.verifyToolForPath(path, entry?.verifiable_by),
  );

  // Fallback: if the row itself isn't verifiable, look at any
  // declared outputs (`entry.outputs[].path`) for an existing
  // verifiable product, e.g. a .cue source row whose sibling .chd
  // already exists. Lets the user verify the replacement directly
  // from the source row before cleanup.
  const outputVerifyTarget = $derived.by(() => {
    if (directVerifyTool) return null;
    for (const out of outputs) {
      if (!out?.exists || !out?.path) continue;
      const t = registry.toolForVerifyPath(out.path);
      if (t) return { tool: t, path: out.path };
    }
    return null;
  });

  // The path we'll actually verify when the user picks Verify.
  const verifyTool = $derived(directVerifyTool ?? outputVerifyTarget?.tool ?? null);
  const verifyPath = $derived(outputVerifyTarget?.path ?? path);
  const canVerify = $derived(!!verifyTool && !inArchive);

  // Info is broader than verify: tools like z3ds expose /api/z3ds-info
  // for source ROMs (.3ds/.cci/.cia) as well as compressed outputs.
  // Try the registry's verify match first; if that fails, fall back
  // to any tool that lists the row's extension as a source. The
  // Info action targets the row's own path.
  // The rule (verify owner first, then a source claim, and only a tool with
  // a getInfo binding) lives in the registry so it is tested there.
  const infoTool = $derived(
    registry.infoToolForRow(path, entry?.verifiable_by, isArchive),
  );
  const canGetInfo = $derived(!!infoTool && !inArchive);

  // A folded makeps3iso split set (Game.iso.0/.1/…) is shown as one row whose
  // path is just the .0 part, so the single-path rename/delete endpoints would
  // only touch .0 and orphan the rest. Disable both until the backend can act
  // on the whole set (the parts are still individually manageable if needed).
  const isSplitSet = $derived(entry?.split_parts != null);

  // Rename / Delete operate on the filesystem, archive members can't be
  // renamed or deleted in-place, so disable them inside archive views.
  const canRename = $derived(!inArchive && !isSplitSet);
  const canDelete = $derived(!inArchive && !isSplitSet);

  async function handleVerify() {
    if (!verifyTool || !verifyPath) return;
    // Surface a live spinner toast for the run, matching how a running
    // job is toasted (loading toast that updates in place, then resolves
    // to success/error). Verify can take a while on big files, so the
    // user gets the same feedback they'd get from a conversion.
    const name = verifyPath.split(/[/\\]/).pop() ?? verifyPath;
    const toastId = toast.loading(name, {
      description: 'Verifying…',
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      const result = await verification.verifyOne(verifyTool.id, verifyPath, {
        onProgress: ({ percent, message }) => {
          const pct =
            typeof percent === 'number' && percent > 0
              ? `${Math.round(percent)}%`
              : null;
          const lead = message || 'Verifying…';
          toast.loading(name, {
            id: toastId,
            description: pct ? `${lead} · ${pct}` : lead,
          });
        },
      });
      if (result?.valid) {
        toast.success(name, {
          id: toastId,
          description: 'Intact',
          duration: 4000,
        });
      } else if (result?.uncheckable || result?.inconclusive) {
        // Refused by a safety rule, or the file could not be read (#359): not
        // damaged, no verdict either way.
        toast.warning(name, {
          id: toastId,
          description: result.message || "Can't be checked here",
          duration: 6000,
        });
      } else {
        toast.error(name, {
          id: toastId,
          // `||` not `??`: an empty-string message should still fall back
          // to a real description, not render a blank toast.
          description: result?.message || 'Verification failed',
          duration: 6000,
        });
      }
    } catch (e) {
      toast.error(name, {
        id: toastId,
        description: e?.message || 'Verify failed',
        duration: 6000,
      });
    }
  }

  function handleInfo() {
    // FileInfoModal resolves candidate tools from the target's path via
    // registry.infoToolsForPath. For source rows (z3ds), the row's path
    // isn't a verify path, so we surface the tool another way: set an
    // inline `_infoTool` hint the modal prepends to its candidate list.
    if (!directVerifyTool && infoTool) {
      ui.infoTarget = { ...entry, _infoTool: infoTool.id };
    } else {
      ui.infoTarget = entry;
    }
  }

  // A folder (not an archive, not inside one) can become a Library (#303).
  // The backend checks it against the Volumes and refuses an overlap.
  const canRegister = $derived(entry?.type === 'directory' && !inArchive);

  async function handleRegister() {
    try {
      const library = await libraries.register(path);
      toast.success(`${library.name} is now a Library`, {
        action: { label: 'Open Rules', onClick: () => libraries.open(library.id) },
      });
    } catch (e) {
      toast.error(e?.message ?? 'Could not register the Library');
    }
  }

  function handleRename() {
    ui.renameTarget = entry;
  }

  // Patch (#428): a cartridge Image, loose or in a .zip/.7z. Disc Images by
  // the registry's discExts, the mirror of the backend's disc_extensions().
  const patchable = $derived(
    canPatch(entry, (p) => registry.discImages([p]).length > 0),
  );

  function handlePatch() {
    ui.patchTarget = entry;
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
      {#if canRegister}
        <DropdownMenu.Item class="row-actions-item" onSelect={handleRegister}>
          <FolderCog size={14} aria-hidden="true" />
          <span>Register as a Library</span>
        </DropdownMenu.Item>
        <!-- "This folder is PSP" (#304); the backend refuses a folder outside a Library. -->
        <DropdownMenu.Item
          class="row-actions-item"
          onSelect={() => findings.openMapping(path, entry?.name)}
        >
          <FolderPen size={14} aria-hidden="true" />
          <span>Set Platform…</span>
        </DropdownMenu.Item>
      {/if}
      {#if patchable}
        <DropdownMenu.Item class="row-actions-item" onSelect={handlePatch}>
          <Bandage size={14} aria-hidden="true" />
          <span>Patch…</span>
        </DropdownMenu.Item>
      {/if}
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
