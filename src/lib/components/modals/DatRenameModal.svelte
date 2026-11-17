<script>
  /**
   * Rename to DAT name (#425): the preview, the confirmation and the job.
   *
   * Opened on the selected files (`ui.datRenamePaths`). The backend's preview
   * lists each Image's old and new path and every Referrer update ("RomM:
   * rename ROM 12 to …", "games.m3u", "gamelist.xml, 2 entries"), and every
   * Image it skips with the reason. A sheet's Image (#426) also lists each
   * track it renames, the folder renamed with it and each reference the
   * sheet's rewrite changes. Confirm starts a background job that
   * renames exactly what the preview showed; an Image that changed since is
   * skipped by it. The dialog follows the job and lists what it did; closing
   * it leaves the job running (it is in the jobs list).
   */
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import FilePen from '@lucide/svelte/icons/file-pen';

  const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
  const STATUS_TONE = { renamed: 'success', skipped: 'neutral', not_renamed: 'error' };
  const STATUS_LABEL = { renamed: 'Renamed', skipped: 'Skipped', not_renamed: 'Not renamed' };

  const paths = $derived(ui.datRenamePaths);
  let preview = $state(null);
  let error = $state('');
  let starting = $state(false);
  let jobId = $state(null);
  // Only the latest preview request may write it.
  let request = 0;

  // The job, as the jobs list has it (``jobs.track`` keeps it there).
  const job = $derived(jobId ? jobs.jobs.find((j) => j.id === jobId) ?? null : null);
  const finished = $derived(!!job && TERMINAL.has(job.status));
  const items = $derived(Array.isArray(preview?.items) ? preview.items : []);
  const renames = $derived(items.filter((i) => i.status === 'rename'));
  const skipped = $derived(items.filter((i) => i.status !== 'rename'));
  const results = $derived(Array.isArray(job?.result?.results) ? job.result.results : []);

  $effect(() => {
    const asked = paths;
    preview = null;
    error = '';
    starting = false;
    jobId = null;
    if (!asked?.length) return;
    const mine = ++request;
    api.datRenamePreview(asked).then(
      (answer) => { if (mine === request) preview = answer; },
      (e) => { if (mine === request) error = e?.message ?? 'Could not preview the rename'; },
    );
  });

  function shortName(p) {
    return p?.split(/[/\\]/).pop() ?? p ?? '';
  }

  // The Image's other files the rename moves (a sheet's tracks, #426): one
  // the folder rename carries under the same name too.
  function otherFiles(item) {
    const files = Array.isArray(item?.files) ? item.files : [];
    return files.filter((f) => f.path !== item.path && f.new_path !== f.path);
  }

  // One preview item as confirmed: its new path and each file's (#426), so
  // the job renames exactly what was shown.
  function confirmed(i) {
    return {
      path: i.path, new_path: i.new_path ?? null,
      files: Array.isArray(i.files) ? i.files.map((f) => ({ path: f.path, new_path: f.new_path })) : null,
    };
  }

  function changeText(change) {
    if (change.summary) return change.summary;
    const name = shortName(change.target);
    return change.entries > 1 ? `update ${name}, ${change.entries} entries` : `update ${name}`;
  }

  async function confirm() {
    if (!renames.length || starting || jobId) return;
    // The dialog session this start belongs to: an answer that comes after
    // it closed (and maybe opened on another selection) writes nothing here.
    const mine = request;
    starting = true;
    error = '';
    try {
      // Every preview item goes back, skipped ones with no new path.
      const started = await api.datRename(items.map(confirmed));
      const id = started?.job_id ?? null;
      // Tracked by the jobs store, whichever dialog is open, so the job
      // reaches the jobs list even if it ends before the stream sees it.
      if (id) {
        jobs.track(id).then((ended) => {
          if (ended === null && mine === request) {
            error = 'The rename job is no longer known; see the jobs list';
          }
        });
      }
      if (mine !== request) return;
      jobId = id;
    } catch (e) {
      if (mine === request) error = e?.message ?? 'Could not start the rename';
    } finally {
      if (mine === request) starting = false;
    }
  }

  function close() {
    request += 1;
    const ran = !!jobId;
    ui.datRenamePaths = null;
    if (ran) {
      fileBrowser.clearSelection();
      fileBrowser.refresh().catch(() => {});
    }
  }
</script>

<BaseModal open={!!paths} onClose={close} title="Rename to DAT name?" size="md">
  {#snippet titleIcon()}<FilePen size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    {#if error}
      <div class="dr-block" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>{error}</div>
      </div>
    {/if}
    {#if jobId}
      {#if !finished}
        <p class="dr-note">
          <Spinner />
          {job?.message || 'Starting the rename…'}{#if job?.progress}&nbsp;({job.progress}%){/if}
        </p>
        <p class="dr-note">It runs as a job: you can close this dialog and follow it in the jobs list.</p>
      {:else}
        <p class="dr-lead">{job.message || job.error_message || 'The rename finished'}</p>
        <ul class="dr-list" aria-label="What the rename did">
          {#each results as r (r.path)}
            <li>
              <span class="dr-row">
                <Badge tone={STATUS_TONE[r.status] ?? 'neutral'}>{STATUS_LABEL[r.status] ?? r.status}</Badge>
                <code title={r.path}>{shortName(r.path)}</code>
                {#if r.status === 'renamed'}<span class="dr-arrow">→</span><code title={r.new_path}>{shortName(r.new_path)}</code>{/if}
              </span>
              {#if r.reason}<span class="dr-extra">{r.reason}</span>{/if}
            </li>
          {/each}
        </ul>
      {/if}
    {:else if !preview && !error}
      <p class="dr-note"><Spinner /> Looking up each Image's DAT name…</p>
    {:else if preview}
      <p class="dr-lead">
        <Badge tone={renames.length ? 'info' : 'neutral'}>{renames.length}</Badge>
        Image{renames.length === 1 ? '' : 's'} will be renamed to the name the top-ranked DAT gives
        {renames.length === 1 ? 'it' : 'them'}, keeping each file's extension. Every playlist,
        gamelist and RomM ROM that points at one follows it.
      </p>
      {#if renames.length}
        <ul class="dr-list" aria-label="Images to rename">
          {#each renames as item (item.path)}
            <li>
              <span class="dr-row">
                <code title={item.path}>{shortName(item.path)}</code>
                <span class="dr-arrow">→</span>
                <code title={item.new_path}>{shortName(item.new_path)}</code>
              </span>
              {#each otherFiles(item) as f (f.path)}
                <span class="dr-row dr-sub">
                  <code title={f.path}>{shortName(f.path)}</code>
                  <span class="dr-arrow">→</span>
                  <code title={f.new_path}>{shortName(f.new_path)}</code>
                </span>
              {/each}
              {#if item.folder}
                <span class="dr-extra" title={item.folder.path}>
                  folder {shortName(item.folder.path)} → {shortName(item.folder.new_path)}
                </span>
              {/if}
              {#each item.sheet_rewrite ?? [] as line (line.old)}
                <span class="dr-extra">{shortName(item.path)}: “{line.old}” → “{line.new}”</span>
              {/each}
              {#if item.dat?.dat_name}<span class="dr-extra">from {item.dat.dat_name}</span>{/if}
              {#each item.referrer_changes ?? [] as change (change.target)}
                <span class="dr-extra" title={change.target}>{changeText(change)}</span>
              {/each}
            </li>
          {/each}
        </ul>
      {/if}
      {#if skipped.length}
        <details class="dr-skipped">
          <summary>{skipped.length} skipped</summary>
          <ul class="dr-list" aria-label="Images skipped">
            {#each skipped as item (item.path)}
              <li>
                <code title={item.path}>{shortName(item.path)}</code>
                <span class="dr-extra">{item.reason}</span>
              </li>
            {/each}
          </ul>
        </details>
      {/if}
    {/if}
  {/snippet}
  {#snippet footer()}
    <!-- Closing never stops a rename already started: say Close, not Cancel. -->
    <Button variant="secondary" onclick={close}>{jobId ? 'Close' : 'Cancel'}</Button>
    {#if !jobId}
      <Button
        variant="primary"
        disabled={!renames.length || starting}
        loading={starting}
        onclick={confirm}
      >
        Rename {renames.length} Image{renames.length === 1 ? '' : 's'}
      </Button>
    {/if}
  {/snippet}
</BaseModal>

<style>
  .dr-lead { margin: 0; font-size: var(--text-sm); color: var(--text-1); display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .dr-note { margin: 0; font-size: var(--text-xs); color: var(--text-2); }
  .dr-block { display: flex; gap: var(--space-2); background: var(--error-muted); color: var(--error); border-radius: var(--radius-md); padding: var(--space-2) var(--space-3); font-size: var(--text-sm); align-items: flex-start; }
  .dr-list { list-style: none; margin: 0; padding: 0; max-height: 280px; overflow-y: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2); }
  .dr-list li { padding: 6px var(--space-2); border-bottom: 1px solid var(--border-subtle); display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .dr-list li:last-child { border-bottom: none; }
  .dr-list code { font-size: var(--text-xs); overflow-wrap: anywhere; color: var(--text-1); }
  .dr-row { display: flex; gap: var(--space-1); align-items: center; flex-wrap: wrap; min-width: 0; }
  .dr-arrow { color: var(--text-3); font-size: var(--text-xs); }
  .dr-sub { padding-left: var(--space-3); }
  .dr-extra { font-size: var(--text-xs); color: var(--text-3); font-family: var(--font-mono); overflow-wrap: anywhere; }
  .dr-skipped summary { cursor: pointer; font-size: var(--text-sm); color: var(--text-2); margin-bottom: var(--space-1); }
</style>
