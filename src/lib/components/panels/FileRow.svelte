<script>
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { chdMetadata } from '$lib/stores/chdMetadata.svelte.js';
  import { formatSize } from '$lib/api/format.js';
  import Folder from '@lucide/svelte/icons/folder';
  import Archive from '@lucide/svelte/icons/archive';
  import Disc3 from '@lucide/svelte/icons/disc-3';
  import Disc from '@lucide/svelte/icons/disc';
  import Gamepad2 from '@lucide/svelte/icons/gamepad-2';
  import File from '@lucide/svelte/icons/file';
  import CircleCheck from '@lucide/svelte/icons/circle-check';
  import BadgeCheck from '@lucide/svelte/icons/badge-check';
  import MoreHorizontal from '@lucide/svelte/icons/ellipsis';
  import Badge from '$lib/components/ui/Badge.svelte';

  /** @type {{ entry: any }} */
  let { entry } = $props();

  const path = $derived(entry?.path);
  const isDirectory = $derived(entry?.type === 'directory');
  const isArchive = $derived(entry?.type === 'archive');
  const isFile = $derived(!isDirectory && !isArchive);

  const selected = $derived(fileBrowser.selectedFiles.has(path));
  const verified = $derived(verification.isVerified(path));
  const datMatch = $derived(datMatching.matchFor(path));
  const chdMeta = $derived(chdMetadata.metadataFor(path));

  const mediaType = $derived(chdMeta?.media_type ?? entry?.media_type ?? null);

  const convertibleBy = $derived.by(() => {
    if (Array.isArray(entry?.convertible_by) && entry.convertible_by.length) {
      return entry.convertible_by;
    }
    const legacy = [];
    if (entry?.convertible) legacy.push('chdman');
    if (entry?.dolphin_convertible) legacy.push('dolphin');
    if (entry?.z3ds_convertible) legacy.push('z3ds');
    return legacy;
  });

  const outputs = $derived(Array.isArray(entry?.outputs) ? entry.outputs : []);

  function iconComponent() {
    if (isDirectory) return Folder;
    if (isArchive) return Archive;
    const ext = entry?.extension?.toLowerCase() ?? '';
    if (ext === '.chd') return Disc3;
    if (['.rvz', '.wia', '.gcz', '.wbfs', '.3ds', '.cci', '.cia', '.z3ds', '.zcci', '.zcia'].includes(ext)) return Gamepad2;
    if (['.iso', '.gdi', '.cue', '.bin'].includes(ext)) return Disc;
    return File;
  }

  const Icon = $derived(iconComponent());

  function handleRowClick(e) {
    if (isDirectory) {
      fileBrowser.navigate(entry.path);
      return;
    }
    if (isArchive) {
      fileBrowser.browseArchive(entry.path);
      return;
    }
    fileBrowser.toggleSelect(entry, { shift: e.shiftKey });
  }

  function handleCheckboxChange(e) {
    e.stopPropagation();
    fileBrowser.toggleSelect(entry, { shift: e.shiftKey });
  }

  function handleNameKey(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handleRowClick(e);
    }
  }

  function archiveItemSummary(e) {
    const total = e?.archive_items;
    const hasChd = e?.archive_has_chd;
    if (typeof total !== 'number') return null;
    if (typeof hasChd === 'number' && hasChd > 0) return `${total} items · ${hasChd} converted`;
    return `${total} items`;
  }
</script>

<tr
  class="row"
  class:selected
  class:dir={isDirectory}
  class:archive={isArchive}
  ondblclick={handleRowClick}
>
  <td class="sel">
    {#if isFile}
      <input
        type="checkbox"
        checked={selected}
        onclick={handleCheckboxChange}
        aria-label={selected ? `Deselect ${entry.name}` : `Select ${entry.name}`}
      />
    {/if}
  </td>
  <td class="name">
    <button
      type="button"
      class="name-button"
      onclick={handleRowClick}
      onkeydown={handleNameKey}
      title={entry?.path}
    >
      <Icon size={16} class="ftype" />
      <span class="name-text">{entry?.name}</span>
      {#if isArchive}
        {@const summary = archiveItemSummary(entry)}
        {#if summary}<span class="archive-summary">{summary}</span>{/if}
      {/if}
    </button>
  </td>
  <td class="size">{isFile ? formatSize(entry?.size) : ''}</td>
  <td class="ext">{entry?.extension ?? ''}</td>
  <td class="badges">
    {#if mediaType === 'cd'}<Badge tone="cd">CD</Badge>{/if}
    {#if mediaType === 'dvd'}<Badge tone="dvd">DVD</Badge>{/if}
    {#if isArchive}<Badge tone="archive">ARC</Badge>{/if}
    {#if verified}
      <Badge tone="verified" title="Verified">
        <CircleCheck size={11} aria-hidden="true" /> OK
      </Badge>
    {/if}
    {#if datMatch && datMatch.matched}
      <Badge tone="dat-match" title={datMatch.title ?? 'Matches DAT entry'}>
        <BadgeCheck size={11} aria-hidden="true" /> DAT
      </Badge>
    {/if}
    {#each outputs as out (out.tool_id)}
      {#if out.exists}
        <Badge tone="success" title={`${out.tool_id} output ready (${out.path ?? ''})`}>
          {out.tool_id}{out.ready ? '' : '*'}
        </Badge>
      {/if}
    {/each}
    {#if isFile && convertibleBy.length > 0}
      <span class="convertible-hint" title={`Convertible by: ${convertibleBy.join(', ')}`}>
        {convertibleBy.join('·')}
      </span>
    {/if}
  </td>
  <td class="actions">
    {#if isFile}
      <button
        type="button"
        class="actions-trigger"
        title="More actions"
        aria-label={`Actions for ${entry.name}`}
        disabled
      >
        <MoreHorizontal size={14} />
      </button>
    {/if}
  </td>
</tr>

<style>
  .row {
    border-top: 1px solid var(--border-subtle);
    transition: background var(--dur-fast) var(--ease-out);
  }
  .row:hover { background: var(--surface-2); }
  .selected { background: var(--accent-muted); }
  .selected:hover { background: var(--accent-muted); }

  td {
    padding: var(--space-2) var(--space-3);
    vertical-align: middle;
    font-size: var(--text-sm);
    color: var(--text-1);
  }
  .sel { width: 32px; text-align: center; }
  .sel input { cursor: pointer; }

  .name { min-width: 0; }
  .name-button {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    background: none;
    border: none;
    color: inherit;
    text-align: left;
    cursor: pointer;
    font: inherit;
    padding: 0;
    min-width: 0;
  }
  .name-button :global(.ftype) {
    color: var(--text-2);
    flex-shrink: 0;
  }
  .dir .name-button :global(.ftype) { color: var(--accent); }
  .archive .name-button :global(.ftype) { color: var(--badge-archive); }
  .name-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .archive-summary {
    color: var(--text-3);
    font-size: var(--text-xs);
    margin-left: var(--space-2);
  }

  .size {
    width: 80px;
    text-align: right;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .ext {
    width: 60px;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    align-items: center;
    min-width: 0;
  }
  .convertible-hint {
    color: var(--text-3);
    font-size: var(--text-xs);
    font-family: var(--font-mono);
  }

  .actions { width: 40px; text-align: center; }
  .actions-trigger {
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
  .actions-trigger:hover:not(:disabled) { background: var(--surface-3); color: var(--text-1); }
  .actions-trigger:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
