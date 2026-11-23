<script>
  /**
   * The plan for the selected games, as lines: one per console and format
   * ("12 PlayStation games → CHD"), largest first, then what is skipped and
   * what is already done. Each line opens to its files. A game whose console
   * is unknown or unclear gets "Pick console": its folder's console in a
   * folder Library (`onmap`), else a choice that only re-plans this session
   * (`onoverride`). The games a converter that can't run here would take are
   * one plain line with See Converters; the server's reason is said once,
   * behind the line's fold (#591).
   *
   * @typedef {Object} Props
   * @property {object[]|null} plan the `/api/jobs/auto-plan` rows
   * @property {boolean} [planning]
   * @property {string|null} [error]
   * @property {() => void} [onretry]
   * @property {(paths: string[], slug: string) => void} [onoverride]
   * @property {(paths: string[]) => string|null} [mapFolder] the Library folder to set, or null
   * @property {(folder: string) => void} [onmap]
   */
  import { onMount } from 'svelte';
  import { findings } from '$lib/stores/findings.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { convertTargets, formatOfPath, targetLabel } from '$lib/tools/convertTargets.js';
  import { skipLine, missingLabel } from '$lib/util/planLines.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';

  /** @type {Props} */
  let {
    plan, planning = false, error = null, onretry, onoverride, mapFolder = () => null, onmap,
  } = $props();

  onMount(() => {
    findings.loadPlatforms().catch(() => {});
  });

  const names = $derived(Object.fromEntries(findings.platforms.map((p) => [p.slug, p.name])));
  const consoles = $derived(
    [...findings.platforms].sort((a, b) => a.name.localeCompare(b.name)),
  );

  function baseName(path) {
    return String(path ?? '').split('::').pop().split(/[\\/]/).pop();
  }

  function games(n) {
    return n === 1 ? 'game' : 'games';
  }

  // What a missing converter would make of these games: the first Make
  // smaller format that tool writes from them, by the registry. Null when the
  // tool or its format isn't known here.
  function missingFormat(line) {
    const tool = registry.all().find((t) => t.label === line.tool);
    if (!tool) return null;
    const entries = line.rows.map((r) => ({ path: r.file_path, type: 'file' }));
    const smaller = convertTargets({ entries, toolIds: [tool.id] })
      .find((g) => g.group === 'Make smaller');
    return smaller?.options[0]?.label ?? null;
  }

  const lines = $derived.by(() => {
    const groups = {};
    for (const row of plan ?? []) {
      let key;
      let line;
      if (row.mode) {
        const format = formatOfPath(row.output_name) ?? targetLabel(row.mode);
        key = `go|${row.platform ?? ''}|${format}`;
        line = { kind: 'go', platform: row.platform, format };
      } else {
        const skip = skipLine(row.skip_reason);
        key = `${skip.kind}|${skip.key}`;
        line = skip;
      }
      if (!groups[key]) groups[key] = { key, ...line, rows: [] };
      groups[key].rows.push(row);
    }
    const order = { go: 0, missing: 1, pick: 1, skip: 1, done: 2 };
    return Object.values(groups).sort(
      (a, b) => order[a.kind] - order[b.kind] || b.rows.length - a.rows.length,
    );
  });

  const nothing = $derived(
    (plan?.length ?? 0) > 0 && !(plan ?? []).some((row) => row.mode),
  );

  // The line whose console choice is open.
  let picking = $state(null);

  function pick(line) {
    const paths = line.rows.map((r) => r.file_path);
    const folder = mapFolder(paths);
    if (folder) {
      onmap?.(folder);
      return;
    }
    picking = picking === line.key ? null : line.key;
  }

  function choose(line, slug) {
    if (!slug) return;
    picking = null;
    onoverride?.(line.rows.map((r) => r.file_path), slug);
  }
</script>

<div class="convert-plan" aria-live="polite">
  {#if planning && !plan}
    <p class="status"><Spinner size="sm" label="Planning" /> Working out the best format…</p>
  {:else if error}
    <p class="status error">Couldn't plan this: {error}</p>
    {#if onretry}<Button variant="secondary" size="sm" onclick={onretry}>Try again</Button>{/if}
  {:else if plan}
    {#if nothing}
      <p class="status">None of these can be converted here.</p>
    {/if}
    <ul class="lines" class:stale={planning}>
      {#each lines as line (line.key)}
        <li class="line {line.kind}">
          <details>
            <summary>
              {#if line.kind !== 'missing'}<strong>{line.rows.length}</strong>{/if}
              {#if line.kind === 'go'}
                {line.platform ? `${names[line.platform] ?? line.platform} ` : ''}{games(line.rows.length)}
                → <strong>{line.format}</strong>
              {:else if line.kind === 'done'}
                {line.label}
              {:else if line.kind === 'missing'}
                {missingLabel(line.rows.length, line.tool, missingFormat(line), line.installed)}
              {:else}
                {games(line.rows.length)} {line.label}
              {/if}
            </summary>
            {#if line.kind === 'missing'}
              <p class="why">{line.why}</p>
            {/if}
            <ul class="files">
              {#each line.rows as row (row.file_path)}
                <li title={row.file_path}>
                  {#if row.mode}
                    {baseName(row.file_path)} → {row.output_name ?? line.format}
                  {:else if line.kind === 'missing'}
                    {baseName(row.file_path)}
                  {:else}
                    {baseName(row.file_path)}: {row.skip_reason}
                  {/if}
                </li>
              {/each}
            </ul>
          </details>
          {#if line.kind === 'missing'}
            <div class="pick">
              <Button
                variant="secondary"
                size="sm"
                onclick={() => ui.navigate('settings', { tab: 'converters' })}
              >See Converters</Button>
            </div>
          {/if}
          {#if line.kind === 'pick' && onoverride}
            <div class="pick">
              <Button variant="secondary" size="sm" onclick={() => pick(line)}>
                {line.rows.length === 1 ? 'Pick console' : `Pick console for all ${line.rows.length}`}
              </Button>
              {#if picking === line.key}
                <select
                  aria-label="Pick the console"
                  onchange={(e) => choose(line, e.currentTarget.value)}
                >
                  <option value="">{line.rows.length === 1 ? 'This is a…' : 'These are all…'}</option>
                  {#each consoles as c (c.slug)}
                    <option value={c.slug}>{c.name}</option>
                  {/each}
                </select>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .convert-plan {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  .status {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .status.error { color: var(--error); }
  .lines {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .lines.stale { opacity: 0.6; }
  .line {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--text-sm);
    color: var(--text-1);
  }
  .line.skip, .line.pick, .line.missing, .line.done { color: var(--text-2); }
  summary {
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .files {
    margin: var(--space-1) 0 0;
    padding-left: var(--space-4);
    color: var(--text-2);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }
  .why {
    margin: var(--space-1) 0 0;
    padding-left: var(--space-4);
    color: var(--text-2);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }
  .pick {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
    padding-left: var(--space-4);
  }
  .pick select {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-1) var(--space-2);
    max-width: 100%;
  }
</style>
