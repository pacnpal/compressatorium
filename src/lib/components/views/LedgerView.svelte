<script>
  import { onMount } from 'svelte';
  import { api } from '$lib/api/endpoints.js';
  import { buildApiUrl } from '$lib/api/client.js';
  import { formatSize } from '$lib/api/format.js';
  import { registry } from '$lib/tools/registry.js';
  import Badge from '$lib/components/ui/Badge.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Pager from '$lib/components/ui/Pager.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Download from '@lucide/svelte/icons/download';
  import Search from '@lucide/svelte/icons/search';

  const PAGE_SIZE = 25;
  const SEARCH_DEBOUNCE_MS = 300;

  let q = $state('');
  let status = $state('');
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
  const filter = $derived({ q: q.trim(), status, tool, platform });
  const filtered = $derived(Boolean(q.trim() || status || tool || platform));
  const exportHref = $derived(buildApiUrl('/ledger/export.csv', filter));

  const withAll = (label, values) => [
    { value: '', label },
    ...values.map((v) => ({ value: v, label: v })),
  ];
  const statusOptions = $derived(withAll('All statuses', facets.status));
  const platformOptions = $derived(withAll('All Platforms', facets.platform));
  const toolOptions = $derived([
    { value: '', label: 'All tools' },
    ...facets.tool.map((t) => ({ value: t, label: registry.forTool(t)?.label ?? t })),
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
      // A prune can shrink the Ledger under the page being viewed; step back
      // to the last page that still exists and load it.
      const last = Math.max(1, Math.ceil(res.total / PAGE_SIZE));
      if (page > last) {
        page = last;
        return load();
      }
    } catch (e) {
      if (mine !== seq) return;
      error = e?.message ?? 'Could not load the Ledger';
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

  function goto(p) {
    page = p;
    load();
  }

  onMount(() => {
    load();
    api.getLedgerFacets().then((f) => (facets = f)).catch(() => {});
    return () => clearTimeout(searchTimer);
  });

  function name(path) {
    return (path ?? '').split(/[/\\]/).pop() ?? path;
  }

  function when(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  function tone(s) {
    if (s === 'completed') return 'success';
    if (s === 'failed') return 'error';
    return 'neutral';
  }

  // Intact and Round-tripped are the two checks; "none" shows nothing.
  const CHECK_LABEL = { intact: 'Intact', round_tripped: 'Round-tripped' };
</script>

<section class="view" aria-labelledby="ledger-title">
  <header class="header">
    <div>
      <h1 id="ledger-title">Ledger</h1>
      <p class="hint">Every finished job, kept across restarts. Search by path to see what happened to an Image.</p>
    </div>
    <a class="export" href={exportHref} download="ledger.csv">
      <Download size={14} aria-hidden="true" />
      Export CSV
    </a>
  </header>

  <form class="filters" onsubmit={(e) => { e.preventDefault(); refilter(); }} role="search">
    <label class="search">
      <span class="label">Search</span>
      <span class="search-box">
        <Search size={14} aria-hidden="true" />
        <input
          type="search"
          placeholder="Game.iso"
          bind:value={q}
          oninput={onSearch}
          aria-label="Search by path"
        />
      </span>
    </label>
    <Select label="Status" bind:value={status} options={statusOptions} onchange={refilter} />
    <Select label="Tool" bind:value={tool} options={toolOptions} onchange={refilter} />
    <Select label="Platform" bind:value={platform} options={platformOptions} onchange={refilter} />
  </form>

  {#if error}
    <div class="error" role="alert">{error}</div>
  {/if}

  <div class="panel">
    {#if loading && items.length === 0}
      <div class="loading"><Spinner size="sm" /> Loading</div>
    {:else if items.length === 0 && !error}
      <EmptyState
        title={filtered ? 'No rows match' : 'No jobs recorded yet'}
        description={filtered ? 'Try a different search or filter.' : 'Finished conversions will appear here.'}
        glyph="∅"
      />
    {:else}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Finished</th>
              <th>Source</th>
              <th>Tool</th>
              <th>Status</th>
              <th class="num">In</th>
              <th class="num">Out</th>
              <th>Check</th>
              <th>Source file</th>
            </tr>
          </thead>
          <tbody>
            {#each items as row (row.id)}
              {@const spec = registry.specFor(row.mode)}
              <tr>
                <td class="when">{when(row.finished_at)}</td>
                <td class="path" title={row.source_path}>
                  <span class="file">{name(row.source_path)}</span>
                  {#if row.error}<span class="err" title={row.error}>{row.error}</span>{/if}
                </td>
                <td>{spec?.label ?? row.mode}</td>
                <td><Badge tone={tone(row.status)} size="sm">{row.status}</Badge></td>
                <td class="num">{formatSize(row.input_bytes)}</td>
                <td class="num">{formatSize(row.output_bytes)}</td>
                <td>{CHECK_LABEL[row.check_passed] ?? ''}</td>
                <td>{row.source_deleted ? 'Deleted' : 'Kept'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <footer class="foot">
        <span class="count">{total} {total === 1 ? 'row' : 'rows'}</span>
        <Pager {page} {pageCount} onpage={goto} />
      </footer>
    {/if}
  </div>
</section>

<style>
  .view { display: flex; flex-direction: column; gap: var(--space-4); padding: var(--space-5); max-width: var(--container-max); margin: 0 auto; width: 100%; min-width: 0; }
  .header { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-3); flex-wrap: wrap; }
  .header h1 { margin: 0; font-size: var(--text-2xl); font-weight: var(--weight-semibold); color: var(--text-1); }
  .hint { color: var(--text-2); margin-top: var(--space-1); }
  .export { display: inline-flex; align-items: center; gap: var(--space-2); padding: 8px 12px; border-radius: var(--radius-md); border: 1px solid var(--border-strong); background: var(--surface-2); color: var(--text-1); font-weight: var(--weight-medium); text-decoration: none; }
  .export:hover { border-color: var(--accent); }
  .export:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .filters { display: flex; gap: var(--space-3); flex-wrap: wrap; align-items: flex-end; }
  .search { display: inline-flex; flex-direction: column; gap: var(--space-1); flex: 1; min-width: 200px; }
  .label { font-size: var(--text-xs); font-weight: var(--weight-medium); color: var(--text-2); text-transform: uppercase; letter-spacing: 0.05em; }
  .search-box { display: flex; align-items: center; gap: var(--space-2); background: var(--surface-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 0 12px; color: var(--text-3); }
  .search-box:focus-within { border-color: var(--accent); }
  .search-box input { flex: 1; min-width: 0; background: none; border: none; outline: none; color: var(--text-1); padding: 8px 0; font-size: var(--text-base); }

  .panel { background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--space-4); box-shadow: var(--elev-1); }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  th { text-align: left; color: var(--text-3); font-size: var(--text-xs); font-weight: var(--weight-medium); text-transform: uppercase; letter-spacing: 0.05em; padding: var(--space-2); border-bottom: 1px solid var(--border-subtle); white-space: nowrap; }
  td { padding: var(--space-2); border-bottom: 1px solid var(--border-subtle); color: var(--text-1); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .when { color: var(--text-2); white-space: nowrap; }
  .path { max-width: 320px; }
  .file { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .err { display: block; color: var(--error); font-size: var(--text-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .foot { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); margin-top: var(--space-3); flex-wrap: wrap; }
  .count { color: var(--text-3); font-size: var(--text-sm); }
  .loading { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); font-size: var(--text-sm); }
  .error { color: var(--error); background: var(--error-muted); padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); font-size: var(--text-sm); }
</style>
