<script>
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import FileRow from './FileRow.svelte';
  import Breadcrumb from './Breadcrumb.svelte';
  import Pager from '$lib/components/ui/Pager.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import Search from '@lucide/svelte/icons/search';
  import XIcon from '@lucide/svelte/icons/x';
  import ChevronUp from '@lucide/svelte/icons/chevron-up';
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import ChevronsUpDown from '@lucide/svelte/icons/chevrons-up-down';
  import Filter from '@lucide/svelte/icons/funnel';
  import Loader from '@lucide/svelte/icons/loader-circle';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import ArrowLeft from '@lucide/svelte/icons/arrow-left';
  import ShieldCheck from '@lucide/svelte/icons/shield-check';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  const entries = $derived(fileBrowser.visibleEntries);
  const pageCount = $derived(fileBrowser.pageCount);
  const page = $derived(fileBrowser.page);
  const sortBy = $derived(fileBrowser.sortBy);
  const sortOrder = $derived(fileBrowser.sortOrder);
  const filter = $derived(fileBrowser.filter);
  const loading = $derived(fileBrowser.loading);
  const error = $derived(fileBrowser.entriesError);
  const allSelected = $derived(fileBrowser.allVisibleSelected);
  const selectedCount = $derived(fileBrowser.selectedFiles.size);
  const searchMode = $derived(fileBrowser.searchMode);
  const archiveMode = $derived(!!fileBrowser.currentArchivePath);
  const autoRefresh = $derived(fileBrowser.autoRefresh);
  const jobsActive = $derived(jobs.hasActive);

  // Extension filter options come from the registry — every tool's
  // source + verify extensions, deduped, in declaration order. Adding
  // a new tool surfaces its inputs/outputs in this dropdown automatically.
  const filterableExts = $derived(registry.allFilterableExts());

  // Selection bulk actions: "Verify selected" surfaces only when at
  // least one selected file has a verify-extension match in the
  // registry — sources (.iso, .cue) can't be verified, so we hide the
  // button entirely rather than ship a noop.
  const selectedHasVerifiable = $derived.by(() => {
    const sel = Array.from(fileBrowser.selectedFiles.values());
    return sel.some((e) => e?.path && registry.toolForVerifyPath(e.path));
  });

  let searchInput = $state('');

  function openBulkVerify() {
    ui.bulkVerifyItems = Array.from(fileBrowser.selectedFiles.values());
  }

  function openBulkDelete() {
    ui.bulkDeleteEntries = Array.from(fileBrowser.selectedFiles.values());
  }

  function sortIcon(field) {
    if (sortBy !== field) return ChevronsUpDown;
    return sortOrder === 'asc' ? ChevronUp : ChevronDown;
  }

  function submitSearch(e) {
    e?.preventDefault?.();
    fileBrowser.search(searchInput.trim());
  }

  function clearSearch() {
    searchInput = '';
    fileBrowser.exitSearch();
  }
</script>

<section class="filelist" aria-label="Files">
  <div class="toolbar">
    <div class="crumbs">
      {#if archiveMode}
        <IconButton label="Leave archive" size="sm" onclick={() => fileBrowser.leaveArchive()}>
          <ArrowLeft size={14} />
        </IconButton>
      {/if}
      <Breadcrumb />
    </div>

    <form class="search" onsubmit={submitSearch} role="search">
      <Search size={14} class="search-icon" />
      <input
        type="search"
        placeholder="Search this folder…"
        bind:value={searchInput}
        aria-label="Search files"
      />
      {#if searchMode}
        <IconButton label="Clear search" size="sm" onclick={clearSearch}>
          <XIcon size={12} />
        </IconButton>
      {/if}
    </form>

    <div class="actions">
      <label class="filter">
        <Filter size={12} />
        <select
          aria-label="Filter by extension"
          value={filter ?? ''}
          onchange={(e) => fileBrowser.setFilter(e.currentTarget.value || null)}
        >
          <option value="">All files</option>
          {#each filterableExts as ext (ext)}
            <option value={ext}>{ext}</option>
          {/each}
        </select>
      </label>
      <IconButton
        label="Refresh"
        size="sm"
        title={jobsActive && !autoRefresh ? 'Auto-refresh paused while jobs active' : 'Refresh listing'}
        onclick={() => fileBrowser.refresh({ force: true })}
      >
        {#if loading}<Loader class="spin" size={14} />{:else}<RefreshCw size={14} />{/if}
      </IconButton>
    </div>
  </div>

  {#if selectedCount > 0}
    <div class="selection-bar" role="status">
      <span class="count-label">{selectedCount} selected</span>
      {#if selectedHasVerifiable}
        <button type="button" class="bulk-action" onclick={openBulkVerify}>
          <ShieldCheck size={12} aria-hidden="true" /> Verify
        </button>
      {/if}
      <button type="button" class="bulk-action danger" onclick={openBulkDelete}>
        <Trash2 size={12} aria-hidden="true" /> Delete
      </button>
      <button type="button" class="link" onclick={() => fileBrowser.clearSelection()}>
        Clear
      </button>
    </div>
  {/if}

  {#if error}
    <div class="error-state"><TriangleAlert size={14} /> {error}</div>
  {/if}

  {#if entries.length === 0 && !loading}
    {#if searchMode}
      <EmptyState
        title="No matches"
        description={`Nothing here matched "${fileBrowser.searchQuery}". Try a different query, or clear search to browse normally.`}
        glyph="∅"
      />
    {:else}
      <EmptyState
        title="Empty folder"
        description="No convertible files here. Use the breadcrumb to navigate up, or pick a different volume."
        glyph="∅"
      />
    {/if}
  {:else}
    <div class="table-wrap">
      <table class="table" aria-label={searchMode ? 'Search results' : 'Files'}>
        <thead>
          <tr>
            <th class="sel">
              <input
                type="checkbox"
                checked={allSelected}
                onchange={() => fileBrowser.toggleSelectAll()}
                aria-label={allSelected ? 'Deselect all' : 'Select all visible'}
              />
            </th>
            <th>
              <button type="button" class="th-button" onclick={() => fileBrowser.setSort('name')}>
                Name {#if sortBy === 'name'}{@const Icon = sortIcon('name')}<Icon size={12} class="sort-i" />{:else}<ChevronsUpDown size={12} class="sort-i muted" />{/if}
              </button>
            </th>
            <th class="size">
              <button type="button" class="th-button" onclick={() => fileBrowser.setSort('size')}>
                Size {#if sortBy === 'size'}{@const Icon = sortIcon('size')}<Icon size={12} class="sort-i" />{:else}<ChevronsUpDown size={12} class="sort-i muted" />{/if}
              </button>
            </th>
            <th class="ext">
              <button type="button" class="th-button" onclick={() => fileBrowser.setSort('extension')}>
                Ext {#if sortBy === 'extension'}{@const Icon = sortIcon('extension')}<Icon size={12} class="sort-i" />{:else}<ChevronsUpDown size={12} class="sort-i muted" />{/if}
              </button>
            </th>
            <th>Status</th>
            <th class="actions"><span class="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {#each entries as entry (entry.path)}
            <FileRow {entry} />
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <div class="footer">
    <Pager {page} {pageCount} onpage={(p) => fileBrowser.setPage(p)} />
    {#if !searchMode && entries.length > 0}
      <span class="muted">{entries.length} shown</span>
    {/if}
  </div>
</section>

<style>
  .filelist { display: flex; flex-direction: column; min-width: 0; gap: var(--space-3); }

  .toolbar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    gap: var(--space-3);
    align-items: center;
  }
  @media (max-width: 700px) {
    .toolbar { grid-template-columns: 1fr; }
  }
  .crumbs { display: flex; align-items: center; gap: var(--space-1); min-width: 0; }

  .search {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 2px var(--space-2);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    min-width: 220px;
  }
  .search :global(.search-icon) { color: var(--text-3); flex-shrink: 0; }
  .search input {
    background: none;
    border: none;
    padding: var(--space-2) var(--space-1);
    color: var(--text-1);
    font-size: var(--text-sm);
    width: 100%;
    outline: none;
  }
  .search input::placeholder { color: var(--text-3); }
  .search:focus-within {
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }

  .actions { display: inline-flex; align-items: center; gap: var(--space-2); }
  .filter {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0 var(--space-2);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-2);
    font-size: var(--text-xs);
  }
  .filter select {
    background: none;
    border: none;
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) 0;
    cursor: pointer;
  }
  .filter select:focus-visible { outline: none; }

  .selection-bar {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    background: var(--accent-muted);
    border-radius: var(--radius-md);
    color: var(--accent);
    font-size: var(--text-sm);
    font-weight: var(--weight-medium);
    flex-wrap: wrap;
  }
  .count-label { margin-right: var(--space-2); }
  .bulk-action {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    background: var(--surface-1);
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 4px 10px;
    border-radius: var(--radius-sm);
    font-size: var(--text-xs);
    cursor: pointer;
  }
  .bulk-action:hover { background: var(--accent); color: var(--accent-contrast); }
  .bulk-action.danger { border-color: var(--error); color: var(--error); }
  .bulk-action.danger:hover { background: var(--error); color: var(--text-inverse); }
  .link {
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    text-decoration: underline;
    font: inherit;
    padding: 0;
    margin-left: auto;
  }

  .error-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3);
    background: var(--error-muted);
    border-radius: var(--radius-md);
    color: var(--error);
    font-size: var(--text-sm);
  }

  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-1);
  }
  .table {
    width: 100%;
    border-collapse: collapse;
    table-layout: auto;
  }
  thead th {
    text-align: left;
    padding: var(--space-2) var(--space-3);
    background: var(--surface-2);
    color: var(--text-2);
    font-weight: var(--weight-semibold);
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border-subtle);
    white-space: nowrap;
  }
  thead th.sel { width: 32px; text-align: center; padding: var(--space-2); }
  thead th.size { width: 80px; text-align: right; }
  thead th.ext { width: 60px; }
  thead th.actions { width: 40px; }
  .th-button {
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    font: inherit;
    padding: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }
  .th-button:hover { color: var(--text-1); }
  .th-button :global(.sort-i) { flex-shrink: 0; }
  .th-button :global(.sort-i.muted) { opacity: 0.4; }

  .footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-1);
  }
  .muted { color: var(--text-3); font-size: var(--text-sm); }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0,0,0,0);
    white-space: nowrap;
    border: 0;
  }

  :global(.filelist .spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
