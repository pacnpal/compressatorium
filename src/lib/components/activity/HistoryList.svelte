<script>
  /**
   * Activity › History: every job that ran, kept across restarts. Check runs,
   * watch-folder copies and duplicate deletes are here too, so the Running
   * list can be the queue only. `#/activity/history?status=failed` opens it
   * on the failures.
   */
  import { onMount } from 'svelte';
  import { api } from '$lib/api/endpoints.js';
  import { buildApiUrl } from '$lib/api/client.js';
  import { formatSize } from '$lib/api/format.js';
  import { registry } from '$lib/tools/registry.js';
  import { targetLabel } from '$lib/tools/convertTargets.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Pager from '$lib/components/ui/Pager.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Download from '@lucide/svelte/icons/download';
  import Search from '@lucide/svelte/icons/search';

  const PAGE_SIZE = 25;
  const SEARCH_DEBOUNCE_MS = 300;

  // The Status filter: a job's status, or "Checked" (its new file passed a check).
  const STATUS_OPTIONS = [
    { value: '', label: 'All' },
    { value: 'completed', label: 'Done' },
    { value: 'failed', label: 'Failed' },
    { value: 'checked', label: 'Checked' },
    { value: 'cancelled', label: 'Cancelled' },
  ];
  const STATUSES = new Set(STATUS_OPTIONS.map((o) => o.value));

  // `?status=failed` (or `checked`) in the URL pre-sets the filter.
  function statusFromUrl() {
    if (typeof window === 'undefined') return '';
    const query = window.location.hash.split('?')[1] ?? '';
    const wanted = new URLSearchParams(query).get('status') ?? '';
    return STATUSES.has(wanted) ? wanted : '';
  }

  let q = $state('');
  let status = $state(statusFromUrl());
  let tool = $state('');
  let platform = $state('');
  let page = $state(1);
  let items = $state([]);
  let total = $state(0);
  let facets = $state({ status: [], tool: [], platform: [] });
  let loading = $state(true);
  let error = $state('');

  // Every load takes a token; only the newest may write, so a slow earlier
  // response can't overwrite a newer filter's rows.
  let seq = 0;
  let searchTimer = null;

  const pageCount = $derived(Math.max(1, Math.ceil(total / PAGE_SIZE)));
  const filter = $derived({
    q: q.trim(),
    status: status === 'checked' ? '' : status,
    checked: status === 'checked' ? true : undefined,
    tool,
    platform,
  });
  const filtered = $derived(Boolean(q.trim() || status || tool || platform));
  const exportHref = $derived(buildApiUrl('/ledger/export.csv', filter));

  const consoleNames = $derived(new Map(findings.platforms.map((p) => [p.slug, p.name])));
  const consoleName = (slug) => (slug ? consoleNames.get(slug) ?? slug : '—');

  // A converter's facet, named by what it makes ("CHD", "RVZ / WIA").
  function formatOf(toolId) {
    const made = (registry.forTool(toolId)?.modes ?? [])
      .filter((mode) => (mode.kind === 'create' || mode.kind === 'compress') && mode.outputExt)
      .map((mode) => targetLabel(mode.outputExt))
      .filter((label, i, all) => all.indexOf(label) === i);
    return made.length ? made.join(' / ') : (registry.forTool(toolId)?.label ?? toolId);
  }
  const formatOptions = $derived([
    { value: '', label: 'All formats' },
    ...facets.tool.map((t) => ({ value: t, label: formatOf(t) })),
  ]);
  const consoleOptions = $derived([
    { value: '', label: 'All consoles' },
    ...facets.platform.map((p) => ({ value: p, label: consoleName(p) })),
  ]);

  async function load() {
    const mine = ++seq;
    loading = true;
    try {
      const res = await api.getLedgerPage({
        ...filter,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      if (mine !== seq) return;
      items = res.items;
      total = res.total;
      error = '';
      // A prune can shrink History under the page being viewed; step back to
      // the last page that still exists and load it.
      const last = Math.max(1, Math.ceil(res.total / PAGE_SIZE));
      if (page > last) {
        page = last;
        return load();
      }
    } catch (e) {
      if (mine !== seq) return;
      error = e?.message ?? "Couldn't load History";
    } finally {
      if (mine === seq) loading = false;
    }
  }

  function refilter() {
    clearTimeout(searchTimer);
    page = 1;
    load();
  }

  function onSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(refilter, SEARCH_DEBOUNCE_MS);
  }

  function clearFilters() {
    q = '';
    status = '';
    tool = '';
    platform = '';
    refilter();
  }

  function goto(p) {
    page = p;
    load();
  }

  onMount(() => {
    load();
    api.getLedgerFacets().then((f) => (facets = f)).catch(() => {});
    findings.loadPlatforms().catch(() => {});
    // A summary link (`?status=checked`) clicked while History is open.
    const follow = () => {
      const wanted = statusFromUrl();
      if (wanted && wanted !== status) {
        status = wanted;
        refilter();
      }
    };
    window.addEventListener('hashchange', follow);
    return () => {
      clearTimeout(searchTimer);
      window.removeEventListener('hashchange', follow);
    };
  });

  function name(path) {
    return (path ?? '').split(/[/\\]/).pop() ?? path;
  }

  function when(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  // What a job that makes no new file did, by its mode.
  const DID = {
    check: 'Re-checked games',
    find_duplicates: 'Looked for duplicates',
    delete_duplicates: 'Deleted the other copies',
    dat_rename: 'Renamed to game-list names',
    inbox_copy: 'Taken in from a watch folder',
    inbox_unpack: 'Unpacked from a watch folder',
    inbox_recognize: 'Identified from a watch folder',
    metadata_scan: 'Read disc details',
    dat_match: 'Matched against game lists',
    patch: 'Patched',
  };

  // "CHD · saved 312 MB", "Failed: {reason}", or what the job did.
  function result(row) {
    if (row.status === 'failed') return `Failed: ${row.error || 'no reason given'}`;
    if (row.status === 'cancelled') return 'Cancelled';
    const did = DID[row.mode];
    if (!row.output_path) return did ?? registry.specFor(row.mode)?.label ?? 'Done';
    const ext = row.output_path.match(/(\.xiso\.iso|\.[^./\\]+)$/)?.[0] ?? '';
    const format = ext ? targetLabel(ext.toLowerCase()) : (did ?? 'Done');
    const saved = (row.input_bytes ?? 0) - (row.output_bytes ?? 0);
    if (row.source_deleted && row.input_bytes != null && row.output_bytes != null && saved > 0) {
      return `${format} · saved ${formatSize(saved)}`;
    }
    return row.output_bytes != null ? `${format} · ${formatSize(row.output_bytes)}` : format;
  }

  const CHECK = { round_tripped: '✓ Exact copy', intact: '✓ Checked', recognized: '✓ Known good dump' };
</script>

<section class="history" aria-label="History">
  <div class="controls">
    <form class="filters" onsubmit={(e) => { e.preventDefault(); refilter(); }} role="search">
      <label class="search">
        <span class="label">Search</span>
        <span class="search-box">
          <Search size={14} aria-hidden="true" />
          <input
            type="search"
            placeholder="Search by name"
            bind:value={q}
            oninput={onSearch}
            aria-label="Search by name"
          />
        </span>
      </label>
      <Select label="Status" bind:value={status} options={STATUS_OPTIONS} onchange={refilter} />
      <Select label="Format" bind:value={tool} options={formatOptions} onchange={refilter} />
      <Select label="Console" bind:value={platform} options={consoleOptions} onchange={refilter} />
    </form>
    <a class="export" href={exportHref} download="history.csv">
      <Download size={14} aria-hidden="true" />
      Export CSV
    </a>
  </div>

  {#if error}
    <div class="error" role="alert">
      <span>Couldn't load this. {error}</span>
      <Button variant="ghost" size="sm" onclick={load}>Try again</Button>
    </div>
  {/if}

  <div class="panel">
    {#if loading && items.length === 0}
      <div class="loading"><Spinner size="sm" /> Loading</div>
    {:else if items.length === 0 && !error}
      <EmptyState glyph="∅" title={filtered ? 'Nothing matches.' : 'Nothing has run yet.'}>
        {#snippet actions()}
          {#if filtered}
            <Button variant="secondary" onclick={clearFilters}>Clear filters</Button>
          {/if}
        {/snippet}
      </EmptyState>
    {:else}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Game</th>
              <th>Console</th>
              <th>Result</th>
              <th>Check</th>
            </tr>
          </thead>
          <tbody>
            {#each items as row (row.id)}
              <tr>
                <td class="when">{when(row.finished_at)}</td>
                <td class="game" title={row.source_path}>
                  <span class="file">{name(row.source_path)}</span>
                  {#each row.extra_inputs ?? [] as input (input.path)}
                    <!-- A patch job's patch file and its SHA1 (#428). -->
                    <span class="extra" title={`${input.path}\nSHA1 ${input.sha1}`}>
                      with {name(input.path)}
                    </span>
                  {/each}
                </td>
                <td>{consoleName(row.platform)}</td>
                <td class="result" class:failed={row.status === 'failed'} title={result(row)}>
                  {result(row)}
                  {#if row.status === 'completed' && row.output_path && !row.source_deleted}
                    <span class="muted">original kept</span>
                  {/if}
                </td>
                <td class="check">
                  {CHECK[row.check_passed] ?? '—'}
                  {#if row.check_dat}<span class="muted" title="The game list that knows it">({row.check_dat})</span>{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <footer class="foot">
        <span class="count">{total} {total === 1 ? 'job' : 'jobs'}</span>
        <Pager {page} {pageCount} onpage={goto} />
      </footer>
    {/if}
  </div>
</section>

<style>
  .history { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
  .controls { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--space-3); flex-wrap: wrap; }
  .export { display: inline-flex; align-items: center; gap: var(--space-2); padding: 8px 12px; border-radius: var(--radius-md); border: 1px solid var(--border-strong); background: var(--surface-2); color: var(--text-1); font-weight: var(--weight-medium); text-decoration: none; }
  .export:hover { border-color: var(--accent); }
  .export:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .filters { display: flex; gap: var(--space-3); flex-wrap: wrap; align-items: flex-end; flex: 1; min-width: 0; }
  .search { display: inline-flex; flex-direction: column; gap: var(--space-1); flex: 1; min-width: 200px; }
  .label { font-size: var(--text-xs); font-weight: var(--weight-medium); color: var(--text-2); text-transform: uppercase; letter-spacing: 0.05em; }
  .search-box { display: flex; align-items: center; gap: var(--space-2); background: var(--surface-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 0 12px; color: var(--text-3); }
  .search-box:focus-within { border-color: var(--accent); }
  .search-box input { flex: 1; min-width: 0; background: none; border: none; outline: none; color: var(--text-1); padding: 8px 0; font-size: var(--text-base); }

  .panel { background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--space-4); box-shadow: var(--elev-1); min-width: 0; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  th { text-align: left; color: var(--text-2); font-size: var(--text-xs); font-weight: var(--weight-medium); text-transform: uppercase; letter-spacing: 0.05em; padding: var(--space-2); border-bottom: 1px solid var(--border-subtle); white-space: nowrap; }
  td { padding: var(--space-2); border-bottom: 1px solid var(--border-subtle); color: var(--text-1); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .muted { display: block; color: var(--text-2); font-size: var(--text-xs); }
  .when { color: var(--text-2); white-space: nowrap; }
  .game { max-width: 320px; }
  .file { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .extra { display: block; color: var(--text-2); font-size: var(--text-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .result { max-width: 320px; overflow-wrap: anywhere; }
  .result.failed { color: var(--error); }
  .check { white-space: nowrap; }

  .foot { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); margin-top: var(--space-3); flex-wrap: wrap; }
  .count { color: var(--text-2); font-size: var(--text-sm); }
  .loading { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); font-size: var(--text-sm); }
  .error { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-2); color: var(--error); background: var(--error-muted); padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); font-size: var(--text-sm); }
</style>
