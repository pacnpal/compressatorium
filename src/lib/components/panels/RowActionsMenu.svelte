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
  const outputs = $derived(Array.isArray(entry?.outputs) ? entry.outputs : []);

  // Direct verify target for this row, if any (tool that owns the
  // path as a verify-class output — .chd, .rvz, .z3ds, etc.).
  const directVerifyTool = $derived(path ? registry.toolForVerifyPath(path) : null);

  // Fallback: if the row itself isn't verifiable, look at any
  // declared outputs (`entry.outputs[].path`) for an existing
  // verifiable product — e.g. a .cue source row whose sibling .chd
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
  const infoTool = $derived.by(() => {
    if (directVerifyTool) return directVerifyTool;
    if (path && typeof path === 'string') {
      const matches = registry.toolsForSourcePath(path);
      // Prefer tools that actually expose Info for that source — for
      // the current registry, every tool exposes `getInfo`, so the
      // first match is fine. Future tools can opt out by leaving
      // getInfo undefined.
      const t = matches.find((tool) => typeof tool.getInfo === 'function');
      if (t) return t;
    }
    return null;
  });
  const canGetInfo = $derived(!!infoTool && !inArchive);

  // Rename / Delete operate on the filesystem — archive members can't be
  // renamed or deleted in-place, so disable them inside archive views.
  const canRename = $derived(!inArchive);
  const canDelete = $derived(!inArchive);

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
          description: 'Verified',
          duration: 4000,
        });
      } else {
        toast.error(name, {
          id: toastId,
          description: result?.message ?? 'Verification failed',
          duration: 6000,
        });
      }
    } catch (e) {
      toast.error(name, {
        id: toastId,
        description: e?.message ?? 'Verify failed',
        duration: 6000,
      });
    }
  }

  function handleInfo() {
    // CHDInfoModal looks up the tool via registry.toolForVerifyPath
    // on the target's path. For source rows (z3ds), the row's path
    // isn't a verify path so we have to surface the tool another way
    // — set the target with an inline `_infoTool` hint the modal can
    // honor without changing every existing call site.
    if (!directVerifyTool && infoTool) {
      ui.chdInfoTarget = { ...entry, _infoTool: infoTool.id };
    } else {
      ui.chdInfoTarget = entry;
    }
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
