<script>
  /**
   * The Findings view (#304): everything that needs a person's decision.
   *
   * Each folder Library's walk records a Platform conflict (the header and
   * the folder or mapping disagree) or an Unsorted Image (nobody could tell
   * its Platform). Each row links to what fixes it: open the folder in the
   * file browser, or set the folder's Platform. A check Rule (#305) adds a
   * "not Intact" row for an Image that failed its check, in any Library.
   * A Library's find-duplicates run (#388) adds one "Duplicate" row per group
   * of Images holding the same disc, with each copy's path, container and
   * size, and says which formats it could not check. Delete others (#389)
   * deletes the other copies of a group whose key proves them equal, as a
   * background job, after the user confirms its plan.
   * Dismiss hides a row until its cause changes. The app never moves or
   * deletes an Image over a Finding.
   */
  import { untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { findings, findingName, keeperWarning } from '$lib/stores/findings.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { TERMINAL_STATUSES } from '$lib/stores/jobConstants.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import FolderOpen from '@lucide/svelte/icons/folder-open';
  import FolderPen from '@lucide/svelte/icons/folder-pen';
  import EyeOff from '@lucide/svelte/icons/eye-off';
  import Eye from '@lucide/svelte/icons/eye';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import DuplicateDeleteModal from '$lib/components/modals/DuplicateDeleteModal.svelte';

  const KIND_LABEL = {
    platform_conflict: 'Platform conflict',
    unsorted: 'Unsorted',
    not_intact: 'Not Intact',
    source_kept: 'Source kept',
    duplicate: 'Duplicate',
    romm_swap: 'RomM swap unfinished',
  };
  const KIND_TONE = {
    platform_conflict: 'warning', not_intact: 'error', source_kept: 'warning',
    duplicate: 'info', romm_swap: 'warning',
  };
  function bytes(n) {
    const size = Number(n) || 0;
    if (size < 1024) return `${size} bytes`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = size / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(1)} ${units[unit]}`;
  }

  // "3 .cso, 1 .chd": the formats a Library's last run could not check.
  function notChecked(run) {
    return Object.entries(run?.not_checked ?? {})
      .map(([container, count]) => `${count} ${container}`)
      .join(', ');
  }

  // Lists on open, and again when "Show dismissed" flips.
  $effect(() => {
    void findings.showDismissed;
    untrack(() => findings.load());
  });

  // Lists again when a find-duplicates run (#388) ends while the view is open,
  // from the jobs store as the Dashboard and the file list follow finished jobs.
  // Keyed on the finished runs' ids, so a status changing in place counts.
  const finishedRuns = $derived(
    jobs.jobs
      .filter((j) => j.mode === 'find_duplicates' && TERMINAL_STATUSES.has(j.status))
      .map((j) => j.id)
      .join(','),
  );

  $effect(() => {
    const ids = finishedRuns ? finishedRuns.split(',') : [];
    untrack(() => findings.noteFinishedRuns(ids));
  });

  // A duplicate delete this page started (#389) runs as a job: when the jobs
  // store sees it end, its dialog settles, the list is read again, and a
  // toast says what went.
  const finishedDeletes = $derived(
    jobs.jobs.filter(
      (j) => j.mode === 'delete_duplicates' && TERMINAL_STATUSES.has(j.status)
        && findings.runningDeletes.includes(j.id),
    ),
  );

  $effect(() => {
    const ended = finishedDeletes;
    if (!ended.length) return;
    untrack(() => {
      for (const { job, result } of findings.noteFinishedDeletes(ended)) {
        if (job.status === 'completed') cleaned(result);
        else if (job.status === 'cancelled') toast.warning(job.message || 'The delete was cancelled');
        else toast.error(job.error_message || 'The delete did not finish');
      }
    });
  });

  // Whether any folder Library exists, for the empty state.
  $effect(() => {
    untrack(() => {
      if (libraries.list.length === 0) libraries.load();
    });
  });

  const emptyText = $derived.by(() => {
    if (findings.dismissed > 0 && !findings.showDismissed) {
      const n = findings.dismissed;
      return `Nothing open. ${n} dismissed Finding${n === 1 ? ' is' : 's are'} hidden; tick Show dismissed to see ${n === 1 ? 'it' : 'them'}.`;
    }
    if (!libraries.loading && libraries.folders.length === 0) {
      return 'No folder Library yet. Open a folder in the Workspace and pick Register as a Library from its row menu.';
    }
    return 'Nothing needs a decision right now. Findings are recorded each time a folder Library is listed or swept, or when you press Check now.';
  });

  function when(stamp) {
    if (!stamp) return '';
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? stamp : date.toLocaleString();
  }

  async function check() {
    try {
      const result = await findings.check();
      const waiting = (result?.libraries ?? []).filter((lib) => lib.status !== 'ok');
      if (waiting.length) {
        const title = waiting.length === 1
          ? '1 Library could not be checked right now'
          : `${waiting.length} Libraries could not be checked right now`;
        toast.warning(title, {
          description: waiting.map((lib) => `${lib.name ?? lib.id}: ${lib.status}`).join(', '),
        });
      }
    } catch (e) {
      toast.error(e?.message ?? 'Could not check the Libraries');
    }
  }

  // A duplicate delete finished (#389): say what went and what was kept.
  function cleaned(result) {
    const results = result?.results ?? [];
    const gone = results.filter((r) => r.status === 'deleted').length;
    const kept = results.filter((r) => r.status !== 'deleted');
    if (result?.keeper_changed) {
      // Not a plain success: the copy kept changed during the delete.
      toast.warning(keeperWarning(result.keep), {
        description: `Deleted ${gone} cop${gone === 1 ? 'y' : 'ies'}. ${result.keeper_changed}.`,
      });
    } else if (gone) toast.success(`Deleted ${gone} cop${gone === 1 ? 'y' : 'ies'}`);
    if (kept.length) {
      toast.warning(`${kept.length} cop${kept.length === 1 ? 'y was' : 'ies were'} kept`, {
        description: kept.map((r) => `${r.path.split(/[/\\]/).pop()}: ${r.reason ?? r.status}`).join('; '),
      });
    }
  }

  async function undismiss(row) {
    try {
      await findings.undismiss(row.id);
    } catch (e) {
      toast.error(e?.message ?? 'Could not show the Finding again');
      // A 409 means the row changed since it was listed: show what it is now.
      findings.load();
    }
  }

  async function dismiss(row) {
    try {
      await findings.dismiss(row.id);
      toast.success(`Dismissed until its cause changes: ${findingName(row)}`);
    } catch (e) {
      toast.error(e?.message ?? 'Could not dismiss the Finding');
      findings.load();
    }
  }

</script>

<div class="findings-view">
  <header class="head">
    <div class="title">
      <h1>Findings</h1>
      <p class="muted">
        Images that need your decision. Nothing here is moved or deleted by the app:
        open the folder to move a file yourself, or tell the app which Platform a folder holds.
      </p>
    </div>
    <div class="head-actions">
      <Checkbox
        bind:checked={findings.showDismissed}
        label={`Show dismissed (${findings.dismissed})`}
      />
      <Button variant="secondary" onclick={check} loading={findings.checking} icon={refreshIcon}>
        Check now
      </Button>
    </div>
  </header>

  {#if findings.pending}
    <div class="notice pending" role="status">
      <span>{findings.pending}</span>
      <Button variant="ghost" size="sm" onclick={check} loading={findings.checking}>
        Check now
      </Button>
    </div>
  {/if}

  {#if findings.error}
    <div class="notice error" role="alert">Could not list the Findings: {findings.error}</div>
  {/if}

  {#each findings.duplicateRuns as run (run.library_id)}
    <div class="notice runs" role="status">
      <span>
        <strong>{run.library_name}</strong>: duplicates last looked for {when(run.finished_at)}.
        {run.groups} group{run.groups === 1 ? '' : 's'}, {run.keyed} Image{run.keyed === 1 ? '' : 's'} checked.
        {#if notChecked(run)}
          Not checked, no content key for these formats yet: {notChecked(run)}.
        {/if}
        {#if run.unreadable}{run.unreadable} could not be read.{/if}
        {#if run.unlisted}{run.unlisted} folder{run.unlisted === 1 ? '' : 's'} could not be listed.{/if}
        {#if run.held}{run.held} group{run.held === 1 ? '' : 's'} with a copy it could not see kept as before.{/if}
        {#if run.busy}{run.busy} in use by a job, left out.{/if}
      </span>
    </div>
  {/each}

  {#if findings.loading && findings.list.length === 0}
    <p class="muted"><Spinner /> Loading…</p>
  {:else if findings.list.length === 0 && findings.error}
    <!-- The error notice above says it all; no empty table under it. -->
  {:else if findings.list.length === 0}
    <EmptyState title="No Findings" description={emptyText} />
  {:else}
    <div class="table" role="table" aria-label="Findings">
      <div class="row header" role="row">
        <span role="columnheader">Library</span>
        <span role="columnheader">Image</span>
        <span role="columnheader">Kind</span>
        <span role="columnheader">First seen</span>
        <span role="columnheader">Last seen</span>
        <!-- Hidden text inside a cell that still takes its track. -->
        <span role="columnheader"><span class="sr-only">Actions</span></span>
      </div>
      {#each findings.list as row (row.id)}
        <div class="row" class:dismissed={row.dismissed} role="row">
          <span role="cell" class="library">{row.library_name}</span>
          <span role="cell" class="image">
            {#if row.kind === 'duplicate'}
              <span class="reason">{row.reason}</span>
              <ul class="copies" aria-label="Copies">
                {#each row.cause?.copies ?? [] as copy (copy.path)}
                  <li>
                    <code>{copy.path}</code>
                    <span class="muted small">{copy.container} · {bytes(copy.size)}</span>
                    {#if copy.path === row.keeper}
                      <Badge tone="success">Keep</Badge>
                    {/if}
                  </li>
                {/each}
              </ul>
              {#if row.keeper_reason}
                <span class="muted small">Suggested keeper: {row.keeper_reason}.</span>
              {/if}
              {#if row.deletable && row.keeper_problem}
                <!-- Not yet proven to decode to the disc (#389): what to do first. -->
                <span class="muted small">Before deleting: {row.keeper_problem}.</span>
              {/if}
              {#if !row.deletable && row.not_deletable}
                <span class="muted small">No delete offered: {row.not_deletable}.</span>
              {/if}
            {:else}
              <code>{row.image_key}</code>
              <span class="muted reason">{row.reason}</span>
            {/if}
          </span>
          <span role="cell">
            <Badge tone={KIND_TONE[row.kind] ?? 'neutral'}>
              {KIND_LABEL[row.kind] ?? row.kind}
            </Badge>
            {#if row.dismissed}<span class="muted small">dismissed</span>{/if}
          </span>
          <span role="cell" class="muted small"><span class="cell-label">First seen </span>{when(row.first_seen)}</span>
          <span role="cell" class="muted small"><span class="cell-label">Last seen </span>{when(row.last_seen)}</span>
          <span role="cell" class="actions">
            <Button
              variant="ghost" size="sm" icon={openIcon}
              ariaLabel={`Open the folder of ${findingName(row)}`}
              onclick={() => fileBrowser.openFolder(row.folder)}
            >
              Open folder
            </Button>
            <!-- A check Rule's "not Intact" (#305), a Source a Rule could
                 not delete (#329) and an unfinished RomM swap (#392) are about
                 the file, not its Platform: Open folder reaches Verify, the
                 backup and the file itself. -->
            {#if row.kind !== 'not_intact' && row.kind !== 'source_kept' && row.kind !== 'duplicate'
              && row.kind !== 'romm_swap'}
              <Button
                variant="ghost" size="sm" icon={mapIcon}
                ariaLabel={`Set the Platform of the folder holding ${findingName(row)}`}
                onclick={() => findings.openMapping(row.folder)}
              >
                Set Platform
              </Button>
            {/if}
            {#if row.kind === 'duplicate' && row.deletable && row.keeper}
              <Button
                variant="ghost" size="sm" icon={deleteIcon}
                ariaLabel={`Delete the other copies of ${findingName(row)}`}
                onclick={() => findings.openCleanup(row)}
              >
                Delete others…
              </Button>
            {/if}
            {#if row.dismissed}
              <Button
                variant="ghost" size="sm" icon={showIcon}
                ariaLabel={`Show ${findingName(row)} again`}
                onclick={() => undismiss(row)}
              >
                Show again
              </Button>
            {:else}
              <Button
                variant="ghost" size="sm" icon={dismissIcon}
                ariaLabel={`Dismiss ${findingName(row)}`}
                onclick={() => dismiss(row)}
              >
                Dismiss
              </Button>
            {/if}
          </span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<DuplicateDeleteModal />

{#snippet refreshIcon()}<RefreshCw size={14} />{/snippet}
{#snippet deleteIcon()}<Trash2 size={14} />{/snippet}
{#snippet openIcon()}<FolderOpen size={14} />{/snippet}
{#snippet mapIcon()}<FolderPen size={14} />{/snippet}
{#snippet dismissIcon()}<EyeOff size={14} />{/snippet}
{#snippet showIcon()}<Eye size={14} />{/snippet}

<style>
  .findings-view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4, 16px);
    padding: var(--space-6, 24px);
    max-width: 1200px;
    margin: 0 auto;
    width: 100%;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--space-4, 16px);
  }
  .title h1 { margin: 0 0 4px; font-size: var(--text-xl, 1.25rem); }
  .title p { margin: 0; max-width: 70ch; }
  .head-actions { display: flex; align-items: center; gap: var(--space-3, 12px); flex-wrap: wrap; }
  .muted { color: var(--text-3); }
  .small { font-size: var(--text-sm, 0.85rem); }
  .notice.error {
    padding: 8px 12px;
    border: 1px solid var(--danger, #d33);
    border-radius: var(--radius-sm);
    color: var(--danger, #d33);
  }
  .notice.runs {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text-2);
    font-size: var(--text-sm, 0.85rem);
  }
  .copies {
    margin: 2px 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .copies li { display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; min-width: 0; }
  .notice.pending {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2, 8px);
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface-2);
    color: var(--text-2);
  }
  .table {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--radius-md, 8px);
    background: var(--surface-1);
    overflow: hidden;
  }
  .row {
    display: grid;
    /* Every row is its own grid, so every track has a size that does not
       depend on the row's content: the header lines up with the rows. */
    grid-template-columns: 6rem minmax(10rem, 1fr) 8.5rem 7rem 7rem 11.5rem;
    gap: var(--space-3, 12px);
    align-items: center;
    padding: 10px 14px;
    border-top: 1px solid var(--border-subtle, var(--border));
  }
  .row.header {
    border-top: none;
    background: var(--surface-2);
    font-size: var(--text-xs, 0.75rem);
    font-weight: var(--weight-semibold);
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .row.dismissed { opacity: 0.6; }
  .image { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .image code { overflow-wrap: anywhere; font-size: var(--text-sm, 0.85rem); }
  .reason { font-size: var(--text-sm, 0.85rem); }
  .library { overflow-wrap: anywhere; }
  .actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; }
  .cell-label { display: none; }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  /* Below this the tracks (800px at their narrowest) do not fit beside the sidebar. */
  @media (max-width: 1180px) {
    .findings-view { padding: var(--space-4, 16px); }
    .row { grid-template-columns: 1fr; gap: 4px; }
    .row.header { display: none; }
    .actions { justify-content: flex-start; }
    .cell-label { display: inline; }
  }
</style>
