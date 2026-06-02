<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { formatSize } from '$lib/api/format.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Info from '@lucide/svelte/icons/info';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  const open = $derived(!!ui.infoTarget);
  const target = $derived(ui.infoTarget);

  // Which tools might describe this file, richest-first. A path can be
  // claimed by more than one tool (a raw .iso is both a chdman create
  // source and a Dolphin disc), and only one of them actually reads it
  // (chdman says "Not a CHD file", Dolphin returns the disc header). So
  // we don't pick a single tool up front: we try each candidate in turn
  // and keep the first that returns. The path-based ordering lives in
  // registry.infoToolsForPath; we just prepend the explicit hint that
  // RowActionsMenu sets for source rows (z3ds ROMs), deduped by id.
  const candidates = $derived.by(() => {
    const p = target?.path;
    if (!p) return [];
    const list = [];
    const add = (t) => {
      if (t && typeof t.getInfo === 'function' && !list.some((x) => x.id === t.id)) {
        list.push(t);
      }
    };
    if (target?._infoTool) add(registry.forTool(target._infoTool));
    for (const t of registry.infoToolsForPath(p)) add(t);
    return list;
  });

  // Whatever the directory listing already knows about the file. Used as
  // a last resort so the modal always shows something rather than just an
  // error when no tool can parse the file.
  function basicInfo(entry) {
    if (!entry) return {};
    const out = {};
    if (entry.name) out.name = entry.name;
    if (entry.path) out.path = entry.path;
    if (entry.type) out.type = entry.type;
    if (typeof entry.size === 'number') out.size = formatSize(entry.size);
    if (entry.extension) out.extension = entry.extension;
    if (entry.media_type) out.media_type = entry.media_type;
    return out;
  }

  // Walk the candidate tools and keep the first getInfo() that returns.
  // getInfo is the registry binding (chdman: getCHDInfo, dolphin:
  // getDolphinInfo, z3ds: getZ3DSInfo, nsz: getNszInfo), no per-tool
  // branches here. If none can parse the file, fall back to the basics
  // the listing already knew; `fellBack` flags that for the UI.
  async function loadInfo(tools, path, entry) {
    for (const t of tools) {
      try {
        return { info: await t.getInfo(path), fellBack: false };
      } catch (_e) {
        // This tool can't read the file; try the next candidate.
      }
    }
    const basics = basicInfo(entry);
    if (Object.keys(basics).length) return { info: basics, fellBack: true };
    throw new Error('No info available for this file.');
  }

  // Derive the request as a promise and await it in the markup. Deriving
  // (rather than fetching into $state from an $effect) means {#await}
  // owns the loading/resolved/error states and discards a stale request
  // on its own when the target changes, so there's no manual guard.
  const request = $derived(
    open && target?.path ? loadInfo(candidates, target.path, target) : null,
  );

  function close() { ui.infoTarget = null; }

  const entriesOf = (info) =>
    info && typeof info === 'object' ? Object.entries(info) : [];
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
    {#if !request}
      <p class="ci-empty">No info returned.</p>
    {:else}
      {#await request}
        <div class="ci-loading"><Spinner size="md" /> Loading…</div>
      {:then { info, fellBack }}
        {@const entries = entriesOf(info)}
        {#if entries.length === 0}
          <p class="ci-empty">No info returned.</p>
        {:else}
          {#if fellBack}
            <p class="ci-note">No tool could read this file, showing the basics.</p>
          {/if}
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
      {:catch e}
        <div class="ci-error" role="alert">
          <TriangleAlert size={14} aria-hidden="true" /> {e?.message ?? 'Failed to load info'}
        </div>
      {/await}
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
  .ci-note {
    color: var(--text-3);
    font-size: var(--text-xs);
    margin: 0 0 var(--space-2);
  }
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
