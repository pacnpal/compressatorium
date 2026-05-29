<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Info from '@lucide/svelte/icons/info';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  const open = $derived(!!ui.chdInfoTarget);
  const target = $derived(ui.chdInfoTarget);
  // Tool resolution: prefer the explicit hint set by RowActionsMenu
  // for source rows (z3ds ROMs whose paths aren't in any tool's
  // verify_extensions but whose tool still exposes getInfo). Fall
  // back to the verify-path lookup for normal verifiable outputs.
  const tool = $derived(
    (target?._infoTool && registry.forTool(target._infoTool))
    ?? (target?.path ? registry.toolForVerifyPath(target.path) : null),
  );

  let info = $state(null);
  let loading = $state(false);
  let error = $state(null);

  // Reset + fetch when the dialog opens with a new target. tool.getInfo
  // is the registry-provided binding (chdman: getCHDInfo, dolphin:
  // getDolphinInfo, z3ds: getZ3DSInfo) — no per-tool branches here.
  // Stale-request guard: an older getInfo() can still resolve after the
  // modal closes (or the target changes). The `active` closure captures
  // the current effect run; the cleanup function flips it false so a
  // late resolution can't overwrite info/error/loading for a different
  // file or after dismiss.
  $effect(() => {
    if (!open) {
      info = null;
      error = null;
      loading = false;
      return;
    }
    if (!tool?.getInfo || !target?.path) return;
    let active = true;
    loading = true;
    error = null;
    tool.getInfo(target.path)
      .then((result) => { if (active) info = result; })
      .catch((e) => { if (active) error = e?.message ?? 'Failed to load info'; })
      .finally(() => { if (active) loading = false; });
    return () => { active = false; };
  });

  function close() { ui.chdInfoTarget = null; }

  // Render the raw object as a labelled list. The backend payload shape
  // differs per tool (CHD vs Dolphin vs z3ds); rather than tool-branch
  // here, just present whatever the backend returned as key/value pairs.
  const entries = $derived(info && typeof info === 'object' ? Object.entries(info) : []);
</script>

<BaseModal
  {open}
  onClose={close}
  title="File info"
  description={target ? target.path : ''}
  size="md"
>
  {#snippet titleIcon()}<Info size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    {#if loading}
      <div class="ci-loading"><Spinner size="md" /> Loading…</div>
    {:else if error}
      <div class="ci-error" role="alert">
        <TriangleAlert size={14} aria-hidden="true" /> {error}
      </div>
    {:else if !tool}
      <p class="ci-empty">No tool can describe this file.</p>
    {:else if entries.length === 0}
      <p class="ci-empty">No info returned.</p>
    {:else}
      <dl class="ci-list">
        {#each entries as [key, value] (key)}
          <div class="ci-row">
            <dt class="ci-key">{key}</dt>
            <dd class="ci-val">
              {#if value && typeof value === 'object'}
                <pre class="ci-pre">{JSON.stringify(value, null, 2)}</pre>
              {:else}
                {value ?? ''}
              {/if}
            </dd>
          </div>
        {/each}
      </dl>
    {/if}
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={close}>Close</Button>
  {/snippet}
</BaseModal>

<style>
  .ci-loading {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .ci-error {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    background: var(--error-muted);
    color: var(--error);
    border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
  }
  .ci-empty { color: var(--text-3); font-size: var(--text-sm); margin: 0; }
  .ci-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin: 0;
  }
  .ci-row {
    display: grid;
    grid-template-columns: minmax(120px, 28%) 1fr;
    gap: var(--space-3);
    padding: 6px var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .ci-row:last-child { border-bottom: none; }
  .ci-key {
    color: var(--text-2);
    font-size: var(--text-xs);
    font-weight: var(--weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .ci-val {
    color: var(--text-1);
    font-size: var(--text-sm);
    margin: 0;
    word-break: break-word;
  }
  .ci-pre {
    margin: 0;
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    padding: var(--space-2);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-1);
    overflow-x: auto;
  }
</style>
