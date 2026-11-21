<script>
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { chdMetadata } from '$lib/stores/chdMetadata.svelte.js';
  import { formatSize } from '$lib/api/format.js';
  import { iconForEntry } from '$lib/util/fileIcon.js';
  import { registry } from '$lib/tools/registry.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { formatOfPath, likelyFormat } from '$lib/tools/convertTargets.js';
  import CircleCheck from '@lucide/svelte/icons/circle-check';
  import ChevronRight from '@lucide/svelte/icons/chevron-right';
  import BadgeCheck from '@lucide/svelte/icons/badge-check';
  import Badge from '$lib/components/ui/Badge.svelte';
  import RowActionsMenu from './RowActionsMenu.svelte';

  /** @type {{ entry: any }} */
  let { entry } = $props();

  const path = $derived(entry?.path);
  const isDirectory = $derived(entry?.type === 'directory');
  const isArchive = $derived(entry?.type === 'archive');
  const isFile = $derived(!isDirectory && !isArchive);

  // A playlist row reads as ticked when all its discs are.
  const selected = $derived(fileBrowser.isRowSelected(entry));
  // Files are always selectable; archives only when the active mode takes the
  // archive directly (e.g. romz_extract on .7z/.zip); directories only under a
  // folder-input mode (makeps3iso) and only when the backend marked them
  // convertible. Drives the checkbox; the name button still navigates.
  const selectable = $derived(fileBrowser.isSelectable(entry));
  // A multi-file game (a sheet and its tracks, a RAR set, a split dump, a
  // playlist and its discs) is one row; its files fold under it (#504).
  const parts = $derived(Array.isArray(entry?.parts) ? entry.parts : []);
  let expanded = $state(false);
  const partWord = $derived(
    `${entry?.part_kind === 'discs' ? 'disc' : 'file'}${parts.length === 1 ? '' : 's'}`,
  );
  // A RomM listing's own matches are seeded into the store (#427), so this is
  // the one answer, and a policy change clears it everywhere at once.
  const datMatch = $derived(datMatching.matchFor(path));
  // A RomM row RomM couldn't identify, that the app Recognizes: the name the
  // DAT or Hasheous gives it goes beside RomM's own title (#427).
  const appName = $derived(
    entry?.romm_identified === false && datMatch?.matched ? datMatch.game_name : null,
  );
  const chdMeta = $derived(chdMetadata.metadataFor(path));

  // A match resolved remotely (Hasheous) instead of from a locally imported
  // DAT. Both carry the same payload keys; `source` is what tells them apart.
  const remoteMatch = $derived(datMatch?.source === 'hasheous');

  // Hasheous returns platform / publisher / year / region / originating DAT
  // that a local match doesn't have. Build the tooltip from whichever of them
  // are present so one expression covers both sources. Multi-line, because a
  // remote match has enough to say that one run-on line stops being readable.
  const matchTooltip = $derived.by(() => {
    if (!datMatch?.matched) return null;
    const lines = [
      datMatch.dat_name ? `Matches ${datMatch.dat_name}` : 'Matches DAT entry',
    ];
    if (datMatch.game_name) lines.push(datMatch.game_name);
    const facts = [datMatch.platform, datMatch.year, datMatch.region, datMatch.publisher]
      .filter(Boolean)
      .join(' · ');
    if (facts) lines.push(facts);
    if (datMatch.rom_name && datMatch.rom_name !== datMatch.game_name) {
      lines.push(datMatch.rom_name);
    }
    const links = Array.isArray(datMatch.metadata_links) ? datMatch.metadata_links : [];
    // Gate on the sources, not on links.length: entries without a `source`
    // would otherwise render a dangling "Listed on ".
    const sources = links.map((l) => l?.source).filter(Boolean);
    if (sources.length) lines.push(`Listed on ${sources.join(', ')}`);
    return lines.join('\n');
  });

  const mediaType = $derived(chdMeta?.media_type ?? entry?.media_type ?? null);

  const convertibleBy = $derived(
    Array.isArray(entry?.convertible_by) ? entry.convertible_by : [],
  );

  const outputs = $derived(Array.isArray(entry?.outputs) ? entry.outputs : []);
  const readyOutputs = $derived(outputs.filter((o) => o?.exists));

  // The check badge is this file's own check only. An original .cue or .bin
  // was not checked when its .chd was, so the new file's chip carries the
  // tick instead (below). Null when nothing passed.
  const check = $derived(verification.checkFor(path) ?? null);
  const CHECK_BADGES = {
    round_tripped: ['Exact copy', 'Exact copy: reads back the same as its original'],
    recognized: ['Known good dump', 'Known good dump: checked, and its checksum is in one of your game lists or on Hasheous'],
    intact: ['Checked', 'Checked: passed its own check'],
  };

  // "→ CHD" when this could be made smaller here and nothing has been made
  // yet. The plan decides; this is the likely answer.
  const ready = $derived(ui.toolsKnown ? ui.visibleTools.map((t) => t.id) : null);
  const willBecome = $derived(
    (isFile || isDirectory) && convertibleBy.length > 0 && readyOutputs.length === 0
      ? likelyFormat(entry, ready) : null,
  );

  // Shared file-type icon map (src/lib/util/fileIcon.js) so new formats only
  // need to be added in one place.
  const Icon = $derived(iconForEntry(entry));

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
    const converted = e?.archive_has_output;
    if (typeof total !== 'number') return null;
    if (typeof converted === 'number' && converted > 0) return `${total} items · ${converted} converted`;
    return `${total} items`;
  }
</script>

<tr
  class="row"
  class:selected
  class:dir={isDirectory}
  class:archive={isArchive}
>
  <td class="sel">
    {#if selectable}
      <input
        type="checkbox"
        checked={selected}
        onclick={handleCheckboxChange}
        aria-label={selected ? `Deselect ${entry.name}` : `Select ${entry.name}`}
      />
    {/if}
  </td>
  <td class="name">
    <div class="name-cell">
    {#if parts.length}
      <button
        type="button"
        class="chevron"
        class:open={expanded}
        aria-expanded={expanded}
        aria-label={`Show the ${parts.length} ${partWord} of ${entry?.name}`}
        onclick={() => (expanded = !expanded)}
      ><ChevronRight size={14} aria-hidden="true" /></button>
    {/if}
    <button
      type="button"
      class="name-button"
      onclick={handleRowClick}
      onkeydown={handleNameKey}
      title={entry?.path}
    >
      <Icon size={16} class="ftype" />
      <!-- A library manager's curated title when there is one (the RomM
           catalog), with the real filename beneath it. `name` stays the
           filename everywhere else, because rename/delete/convert act on it. -->
      <span class="name-stack">
        <span class="name-text">{entry?.display_name || entry?.name}</span>
        {#if entry?.display_name}
          <span class="file-name" title={entry.name}>{entry.name}</span>
        {/if}
        {#if parts.length}
          <span class="file-name">{parts.length} {partWord}</span>
        {/if}
        {#if appName}
          <span class="app-name" title={matchTooltip}>
            {remoteMatch ? 'Hasheous' : 'DAT'}: {appName}
          </span>
        {/if}
      </span>
      {#if isArchive}
        {@const summary = archiveItemSummary(entry)}
        {#if summary}<span class="archive-summary">{summary}</span>{/if}
      {/if}
    </button>
    </div>
  </td>
  <td class="size">{isFile || parts.length ? formatSize(entry?.size) : ''}</td>
  <td class="ext">{registry.displayExtension(entry)}</td>
  <td class="badges">
    {#if mediaType === 'cd'}<Badge tone="cd">CD</Badge>{/if}
    {#if mediaType === 'dvd'}<Badge tone="dvd">DVD</Badge>{/if}
    {#if isArchive}<Badge tone="archive">ARC</Badge>{/if}
    {#if check}
      <Badge tone="verified" title={CHECK_BADGES[check][1]}>
        <CircleCheck size={11} aria-hidden="true" />
        {CHECK_BADGES[check][0]}
      </Badge>
    {/if}
    {#if datMatch && datMatch.matched}
      <Badge tone={remoteMatch ? 'info' : 'dat-match'} title={matchTooltip}>
        <BadgeCheck size={11} aria-hidden="true" /> {remoteMatch ? 'HASH' : 'DAT'}
      </Badge>
    {/if}
    {#each readyOutputs as out (out.path ?? out.tool_id)}
      {@const outCheck = out.path ? verification.checkFor(out.path) : null}
      {@const format = formatOfPath(out.path) ?? 'New file'}
      <Badge
        tone="success"
        title={`${format} ${out.ready ? 'ready' : 'still being written'}${outCheck ? `, ${CHECK_BADGES[outCheck]?.[0] ?? 'checked'}` : ''}: ${out.path ?? ''}`}
      >
        {#if outCheck}<CircleCheck size={11} aria-hidden="true" />{/if}
        {format}{out.ready ? ' ✓' : '…'}
      </Badge>
    {/each}
    {#if willBecome}
      <span class="convertible-hint" title={`Can be made smaller: ${willBecome}`}>→ {willBecome}</span>
    {/if}
  </td>
  <td class="actions">
    <RowActionsMenu {entry} />
  </td>
</tr>
{#if expanded}
  {#each parts as part (part.path)}
    <tr class="row part">
      <td class="sel"></td>
      <td class="name">
        <span class="part-name" title={part.path}>
          {part.name}
          {#if part.parts?.length}
            <span class="file-name">{part.parts.length} {part.parts.length === 1 ? 'file' : 'files'}</span>
          {/if}
        </span>
      </td>
      <td class="size">{formatSize(part.size)}</td>
      <td class="ext">{registry.displayExtension(part)}</td>
      <td class="badges"></td>
      <td class="actions"></td>
    </tr>
  {/each}
{/if}

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
  /* The row is a flex ROW, so title and filename were siblings side by side.
     Stacking them needs their own column box. */
  .name-stack {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
  }
  .file-name {
    color: var(--text-2);
    font-size: var(--text-xs);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
  .app-name {
    color: var(--text-2);
    font-size: var(--text-xs);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
  /* A game's name is never cut: it wraps to a second line instead. */
  .name-text {
    overflow-wrap: anywhere;
  }
  .name-cell { display: flex; align-items: center; gap: var(--space-1); }
  .chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    width: 20px;
    height: 20px;
    padding: 0;
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--text-2);
    cursor: pointer;
    transition: transform var(--dur-fast) var(--ease-out);
  }
  .chevron:hover { background: var(--surface-3); color: var(--text-1); }
  .chevron:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .chevron.open { transform: rotate(90deg); }
  .part td { color: var(--text-2); }
  .part-name {
    display: flex;
    flex-direction: column;
    padding-left: calc(20px + var(--space-6));
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
    min-width: 0;
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
</style>
