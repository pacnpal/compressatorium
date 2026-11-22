<script>
  /**
   * Activity › Needs attention: every problem Compressatorium can't fix on its
   * own, in one sentence, with the one button that fixes it.
   *
   * Each folder Library's walk records a console it can't tell or that is
   * unclear (#304); a check (#305) records a game that failed it; a delete a
   * Rule couldn't make (#329) keeps its original; a find-duplicates run
   * (#388) records each group of copies, and Delete the others (#389)
   * deletes them as a background job after the user confirms its plan; a
   * watch folder (#431) records a file it couldn't take in. Place it (plan
   * item 13) moves a watch folder's file into the folder of the console
   * picked, and it's taken in at once. Ignore hides a row until its cause
   * changes. Nothing else here moves or deletes a file.
   */
  import { onMount, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import { findings, findingName, keeperWarning } from '$lib/stores/findings.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { TERMINAL_STATUSES, terminalJobIds } from '$lib/stores/jobConstants.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import FolderOpen from '@lucide/svelte/icons/folder-open';
  import FolderPen from '@lucide/svelte/icons/folder-pen';
  import FolderInput from '@lucide/svelte/icons/folder-input';
  import EyeOff from '@lucide/svelte/icons/eye-off';
  import Eye from '@lucide/svelte/icons/eye';
  import ListOrdered from '@lucide/svelte/icons/list-ordered';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import DuplicateDeleteModal from '$lib/components/modals/DuplicateDeleteModal.svelte';
  import AddLibraryModal from '$lib/components/modals/AddLibraryModal.svelte';

  // What each problem is called, the one sentence that says it, and the one
  // fix it offers: `console` (set the folder's console), `folder` (open it),
  // `duplicates` (delete the other copies), `names` (choose the name order),
  // `place` (Place it) or `delete` (delete the file). Ignore goes with each.
  const KIND_COPY = {
    unsorted: { label: 'Unknown console', tone: 'warning', fix: 'console',
      say: () => "Compressatorium can't tell which console this is for." },
    platform_conflict: { label: 'Console unclear', tone: 'warning', fix: 'console',
      say: (row) => clash(row) },
    not_intact: { label: 'Failed its check', tone: 'error', fix: 'folder',
      say: () => 'It no longer reads back the same. Replace it from a good copy.' },
    source_kept: { label: 'Original kept', tone: 'warning', fix: 'folder',
      say: (row) => `The new file is fine, but the original wasn't deleted: ${keptWhy(row)}.` },
    duplicate: { label: 'Duplicate', tone: 'info', fix: 'duplicates',
      say: (row) => `${row.cause?.copies?.length ?? 2} copies of the same game.` },
    romm_swap: { label: 'RomM update unfinished', tone: 'warning', fix: 'folder',
      say: (row) => row.reason },
    rename_conflict: { label: 'Name unclear', tone: 'warning', fix: 'names',
      say: () => 'Your game lists disagree on its name.' },
    inbox_unsorted: { label: 'Unknown console (watch folder)', tone: 'warning', fix: 'place',
      say: (row) => `Dropped in ${row.inbox_name ?? 'a watch folder'}. Pick its console and it's sorted in.` },
    inbox_conflict: { label: 'Console unclear (watch folder)', tone: 'warning', fix: 'place',
      say: (row) => clash(row) },
    inbox_dat_conflict: { label: 'Name unclear (watch folder)', tone: 'warning', fix: 'names',
      say: () => 'Your game lists disagree on its name.' },
    inbox_failed: { label: "Couldn't take it in", tone: 'error', fix: 'delete',
      say: (row) => takeInWhy(row) },
  };
  const copyOf = (row) => KIND_COPY[row?.kind]
    ?? { label: row?.kind ?? '', tone: 'neutral', fix: 'folder', say: (r) => r.reason };

  // A console's name; its slug when the list hasn't loaded.
  const consoleNames = $derived(new Map(findings.platforms.map((p) => [p.slug, p.name])));
  const consoleName = (slug) => consoleNames.get(slug) ?? slug;

  function clash(row) {
    const cause = row.cause ?? {};
    if ((cause.headers?.length ?? 0) > 1) {
      return `Its own data names more than one console: ${cause.headers.map(consoleName).join(', ')}.`;
    }
    const said = cause.header;
    const folder = cause.folder_platform ?? cause.folder;
    if (said && folder) return `The file says ${consoleName(said)}, its folder says ${consoleName(folder)}.`;
    return row.reason;
  }

  function keptWhy(row) {
    return String(row.reason ?? 'it could not be deleted safely')
      .replace(/^Source kept:\s*/i, '')
      .replace(/\.$/, '');
  }

  function takeInWhy(row) {
    const why = String(row.reason ?? '');
    if (/archive/i.test(why) && /(damaged|corrupt|incomplete|truncated|crc|unexpected end)/i.test(why)) {
      return 'This archive is damaged or incomplete. Download it again.';
    }
    return why;
  }

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

  const base = (path) => String(path ?? '').split(/[/\\]/).filter(Boolean).pop() ?? '';

  // The Drive holding *path*: the configured folder it sits under.
  function driveOf(path) {
    return fileBrowser.volumes
      .filter((v) => v?.path && (path === v.path || path?.startsWith(`${v.path.replace(/\/+$/, '')}/`)))
      .sort((a, b) => b.path.length - a.path.length)[0] ?? null;
  }

  // What a row's game is called, and where it sits: "{drive} › {folder}",
  // or for a watch folder's file "{watch folder} › {folder in it}".
  function gameName(row) {
    if (row.kind === 'duplicate') return findingName(row);
    return base(row.relative ?? row.path) || row.image_key;
  }
  function gamePlace(row) {
    if (row.inbox_name) {
      const inside = String(row.relative ?? '').split('/').slice(0, -1);
      return [row.inbox_name, ...inside].join(' › ');
    }
    return onDrive(row.folder);
  }
  // A path as "{drive} › {folder} › …", or as it is outside every Drive.
  function onDrive(path) {
    const drive = driveOf(path);
    if (!drive) return path ?? '';
    const inside = path.slice(drive.path.replace(/\/+$/, '').length).split('/').filter(Boolean);
    return [drive.name ?? base(drive.path), ...inside].join(' › ');
  }

  function when(stamp) {
    if (!stamp) return '';
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? stamp : date.toLocaleString();
  }
  const seen = (row) => `First seen ${when(row.first_seen)} · Last seen ${when(row.last_seen)}`;

  // The Library filter, from the URL (`#/activity/attention/<library id>`).
  const libraryFilter = $derived(ui.activityLibrary ?? '');
  const libraryOptions = $derived.by(() => {
    const named = Object.fromEntries(findings.list.map((r) => [r.library_id, r.library_name]));
    const seen = { has: (id) => Object.hasOwn(named, id) };
    // A Library from the URL that has nothing listed is still the one shown.
    if (libraryFilter && !seen.has(libraryFilter)) {
      named[libraryFilter] = libraries.list.find((l) => l.id === libraryFilter)?.name;
    }
    return [
      { value: '', label: 'All Libraries' },
      ...Object.entries(named).map(([value, label]) => ({ value, label: label ?? value })),
    ];
  });
  const rows = $derived(
    libraryFilter ? findings.list.filter((r) => r.library_id === libraryFilter) : findings.list,
  );
  function pickLibrary(id) {
    ui.navigate('activity', { tab: 'attention', id: id || null });
  }

  // Lists on open, and again when "Show ignored" flips.
  $effect(() => {
    void findings.showDismissed;
    untrack(() => findings.load());
  });

  // How many conversions failed in the last day: a line that links to them.
  // Read again whenever a job ends; only the newest read may write.
  let failedToday = $state(0);
  let failedRead = 0;
  const ended = $derived(terminalJobIds(jobs.jobs).join(','));
  $effect(() => {
    void ended;
    const mine = ++failedRead;
    api.getLedgerPage({ status: 'failed', since: '24h', limit: 1 })
      .then((page) => { if (mine === failedRead) failedToday = page?.total ?? 0; })
      .catch(() => {});
  });
  onMount(() => {
    fileBrowser.volumes.length || fileBrowser.fetchVolumes();
    findings.loadPlatforms().catch(() => {});
  });

  // Lists again when a find-duplicates run (#388) ends while the view is open,
  // from the jobs store as the file list follows finished jobs. Keyed on the
  // finished runs' ids, so a status changing in place counts.
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

  // Whether any Library exists, for the empty state.
  $effect(() => {
    untrack(() => {
      if (libraries.list.length === 0) libraries.load();
    });
  });
  const noLibrary = $derived(!libraries.loading && libraries.list.length === 0);
  let adding = $state(false);

  const ignoredNote = $derived.by(() => {
    const n = findings.dismissed;
    if (!n || findings.showDismissed) return '';
    return ` ${n} ignored ${n === 1 ? 'problem is' : 'problems are'} hidden.`;
  });

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
      toast.error(e?.message ?? "Couldn't check the Libraries");
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

  // The rows with an action in flight, by id: a second click waits for it.
  let busy = $state({});
  const settle = (id) => {
    const { [id]: _done, ...rest } = busy;
    busy = rest;
  };

  async function undismiss(row) {
    if (busy[row.id]) return;
    busy = { ...busy, [row.id]: true };
    try {
      await findings.undismiss(row.id);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't show it again");
      // A 409 means the row changed since it was listed: show what it is now.
      findings.load();
    } finally {
      settle(row.id);
    }
  }

  async function dismiss(row) {
    if (busy[row.id]) return;
    busy = { ...busy, [row.id]: true };
    try {
      await findings.dismiss(row.id);
      toast.success(`Ignored until it changes: ${gameName(row)}`);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't ignore it");
      findings.load();
    } finally {
      settle(row.id);
    }
  }

  // Place it: the console picked for each row is kept in the store
  // (`findings.placeChoices`), so switching tabs doesn't lose it.

  // The consoles to offer: the ones the file or its folder name first, then
  // the rest by name.
  function consoleOptions(row) {
    const cause = row.cause ?? {};
    const likely = [cause.header, cause.folder, ...(cause.headers ?? [])].filter(Boolean);
    const sorted = [...findings.platforms].sort((a, b) => a.name.localeCompare(b.name));
    const first = sorted.filter((p) => likely.includes(p.slug));
    return [
      { value: '', label: 'This is a…' },
      ...[...first, ...sorted.filter((p) => !likely.includes(p.slug))]
        .map((p) => ({ value: p.slug, label: p.name })),
    ];
  }
  const choiceOf = (row) => findings.placeChoices[row.id] ?? row.cause?.header ?? '';

  async function place(row) {
    const platform = choiceOf(row);
    if (!platform) return;
    if (busy[row.id]) return;
    busy = { ...busy, [row.id]: true };
    try {
      await findings.place(row.id, platform);
      const { [row.id]: _placed, ...left } = findings.placeChoices;
      findings.placeChoices = left;
      toast.success(`Placing ${gameName(row)} as ${consoleName(platform)}…`);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't place it");
      findings.load();
    } finally {
      settle(row.id);
    }
  }

  // Delete opens on the file's real listing entry, with its replacement
  // outputs, so the dialog still asks before an original whose new file
  // isn't checked goes. Not found: it doesn't open.
  async function remove(row) {
    if (busy[row.id]) return;
    busy = { ...busy, [row.id]: true };
    try {
      const folder = row.path.slice(0, row.path.lastIndexOf('/')) || '/';
      const listing = await api.listFiles(folder);
      const entry = (listing?.entries ?? []).find((e) => e.path === row.path);
      if (!entry) {
        toast.error(`Couldn't open ${gameName(row)} to delete it: it isn't in its folder any more`);
        findings.load();
        return;
      }
      ui.deleteTarget = { ...entry, name: entry.name ?? gameName(row) };
    } catch (e) {
      toast.error(e?.message ?? `Couldn't open ${gameName(row)} to delete it`);
    } finally {
      settle(row.id);
    }
  }
</script>

<div class="attention">
  <div class="controls">
    <Select
      label="Library"
      value={libraryFilter}
      options={libraryOptions}
      onchange={pickLibrary}
    />
    <span class="spacer"></span>
    <Checkbox
      bind:checked={findings.showDismissed}
      label={`Show ignored (${findings.dismissed})`}
    />
    <Button variant="secondary" onclick={check} loading={findings.checking} icon={refreshIcon}>
      Check again
    </Button>
  </div>

  {#if failedToday > 0}
    <div class="notice failed" role="status">
      <span>
        {failedToday} conversion{failedToday === 1 ? '' : 's'} failed in the last day.
      </span>
      <a href="#/activity/history?status=failed">See them in History</a>
    </div>
  {/if}

  {#if findings.pending}
    <div class="notice" role="status">
      <span>{findings.pending}</span>
      <Button variant="ghost" size="sm" onclick={check} loading={findings.checking}>
        Check again
      </Button>
    </div>
  {/if}

  {#if findings.error}
    <div class="notice error" role="alert">
      <span>Couldn't load this. {findings.error}</span>
      <Button variant="ghost" size="sm" onclick={() => findings.load()}>Try again</Button>
    </div>
  {/if}

  {#each findings.duplicateRuns as run (run.library_id)}
    <div class="notice" role="status">
      <span>
        <strong>{run.library_name}</strong>: duplicates last looked for {when(run.finished_at)}.
        {run.groups} group{run.groups === 1 ? '' : 's'}, {run.keyed} game{run.keyed === 1 ? '' : 's'} checked.
        {#if notChecked(run)}
          Not compared yet, no way to tell these formats apart: {notChecked(run)}.
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
  {:else if rows.length === 0 && findings.error}
    <!-- The error notice above says it all; no empty table under it. -->
  {:else if rows.length === 0}
    <EmptyState
      glyph="✓"
      title="Nothing needs you"
      description={`Problems Compressatorium can't fix on its own show up here.${ignoredNote}${noLibrary ? " Add a Library and it's watched for problems too." : ''}`}
    >
      {#snippet actions()}
        {#if noLibrary}
          <Button variant="secondary" onclick={() => (adding = true)}>Add a Library</Button>
        {/if}
      {/snippet}
    </EmptyState>
  {:else}
    <div class="table" role="table" aria-label="Needs attention">
      <div class="row header" role="row">
        <span role="columnheader">Library</span>
        <span role="columnheader">Game</span>
        <span role="columnheader">Problem</span>
        <!-- Hidden text inside a cell that still takes its track. -->
        <span role="columnheader"><span class="sr-only">Actions</span></span>
      </div>
      {#each rows as row (row.id)}
        {@const about = copyOf(row)}
        <div class="row" class:dismissed={row.dismissed} role="row" title={seen(row)}>
          <span role="cell" class="library">{row.library_name}</span>
          <span role="cell" class="game">
            <span class="name">{gameName(row)}</span>
            {#if row.kind === 'duplicate'}
              <ul class="copies" aria-label="Copies">
                {#each row.cause?.copies ?? [] as copy (copy.path)}
                  <li>
                    <span class="copy" title={copy.path}>{onDrive(copy.path)}</span>
                    <span class="muted small">{copy.container} · {bytes(copy.size)}</span>
                    {#if copy.path === row.keeper}
                      <Badge tone="success">Keep</Badge>
                    {/if}
                  </li>
                {/each}
              </ul>
            {:else}
              <span class="muted small where">{gamePlace(row)}</span>
            {/if}
          </span>
          <span role="cell" class="problem">
            <span class="label">
              <Badge tone={about.tone}>{about.label}</Badge>
              {#if row.dismissed}<span class="muted small">ignored</span>{/if}
            </span>
            <span class="say">{about.say(row)}</span>
            {#if row.kind === 'duplicate'}
              {#if row.keeper_reason}
                <span class="muted small">Suggested copy to keep: {row.keeper_reason}.</span>
              {/if}
              {#if row.deletable && row.keeper_problem}
                <!-- Not yet proven to decode to the disc (#389): what to do first. -->
                <span class="muted small">Before deleting: {row.keeper_problem}.</span>
              {/if}
              {#if !row.deletable && row.not_deletable}
                <span class="muted small">No delete offered: {row.not_deletable}.</span>
              {/if}
            {/if}
          </span>
          <span role="cell" class="actions">
            {#if about.fix === 'console'}
              <Button
                variant="ghost" size="sm" icon={mapIcon}
                ariaLabel={`Pick the console of ${findingName(row)}`}
                onclick={() => findings.openMapping(row.folder)}
              >
                Pick console
              </Button>
            {:else if about.fix === 'duplicates'}
              {#if row.kind === 'duplicate' && row.deletable && row.keeper}
                <Button
                  variant="ghost" size="sm" icon={deleteIcon}
                  ariaLabel={`Delete the other copies of ${findingName(row)}`}
                  onclick={() => findings.openCleanup(row)}
                >
                  Delete the others…
                </Button>
              {/if}
            {:else if about.fix === 'names'}
              <Button
                variant="ghost" size="sm" icon={orderIcon}
                ariaLabel={`Choose the name order for ${gameName(row)}`}
                onclick={() => ui.navigate('settings', { tab: 'identification' })}
              >
                Choose name order
              </Button>
            {:else if about.fix === 'place' && !row.dismissed}
              <span class="place">
                <Select
                  ariaLabel={`The console of ${gameName(row)}`}
                  value={choiceOf(row)}
                  options={consoleOptions(row)}
                  onchange={(v) => (findings.placeChoices = { ...findings.placeChoices, [row.id]: v })}
                />
                <Button
                  size="sm" icon={placeIcon}
                  disabled={!choiceOf(row)}
                  loading={!!busy[row.id]}
                  onclick={() => place(row)}
                >
                  Place it
                </Button>
              </span>
            {/if}
            {#if (about.fix === 'delete' || row.kind === 'inbox_unsorted') && driveOf(row.path)}
              <Button
                variant="ghost" size="sm" icon={deleteIcon}
                ariaLabel={`Delete ${gameName(row)}`}
                loading={!!busy[row.id]}
                onclick={() => remove(row)}
              >
                Delete
              </Button>
            {/if}
            {#if row.folder}
              <Button
                variant="ghost" size="sm" icon={openIcon}
                ariaLabel={`Open the folder of ${findingName(row)}`}
                onclick={() => fileBrowser.openFolder(row.folder)}
              >
                Open folder
              </Button>
            {/if}
            {#if row.dismissed}
              <Button
                variant="ghost" size="sm" icon={showIcon}
                ariaLabel={`Show ${findingName(row)} again`}
                disabled={!!busy[row.id]}
                onclick={() => undismiss(row)}
              >
                Show again
              </Button>
            {:else}
              <Button
                variant="ghost" size="sm" icon={dismissIcon}
                ariaLabel={`Ignore ${findingName(row)}`}
                disabled={!!busy[row.id]}
                onclick={() => dismiss(row)}
              >
                Ignore
              </Button>
            {/if}
          </span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<DuplicateDeleteModal />
<AddLibraryModal open={adding} onClose={() => (adding = false)} />

{#snippet refreshIcon()}<RefreshCw size={14} />{/snippet}
{#snippet deleteIcon()}<Trash2 size={14} />{/snippet}
{#snippet openIcon()}<FolderOpen size={14} />{/snippet}
{#snippet mapIcon()}<FolderPen size={14} />{/snippet}
{#snippet placeIcon()}<FolderInput size={14} />{/snippet}
{#snippet orderIcon()}<ListOrdered size={14} />{/snippet}
{#snippet dismissIcon()}<EyeOff size={14} />{/snippet}
{#snippet showIcon()}<Eye size={14} />{/snippet}

<style>
  .attention {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    min-width: 0;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-3);
  }
  .spacer { flex: 1; }
  .muted { color: var(--text-2); }
  .small { font-size: var(--text-sm); }
  .notice {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--surface-1);
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .notice.error {
    border-color: var(--error);
    color: var(--error);
  }
  .notice.failed a { color: var(--accent); font-weight: var(--weight-medium); }
  .copies {
    margin: 2px 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .copies li { display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; min-width: 0; }
  .copies .copy { overflow-wrap: anywhere; font-size: var(--text-sm); }
  .table {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    background: var(--surface-1);
    box-shadow: var(--elev-1);
    overflow: hidden;
  }
  .row {
    display: grid;
    /* Every row is its own grid, so every track has a size that does not
       depend on the row's content: the header lines up with the rows. */
    grid-template-columns: 7rem minmax(12rem, 1fr) minmax(14rem, 1.2fr) 14rem;
    gap: var(--space-3);
    align-items: start;
    padding: 10px 14px;
    border-top: 1px solid var(--border-subtle);
  }
  .row.header {
    border-top: none;
    background: var(--surface-2);
    font-size: var(--text-xs);
    font-weight: var(--weight-semibold);
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .row.dismissed { opacity: 0.6; }
  .game, .problem { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .name { font-weight: var(--weight-medium); color: var(--text-1); overflow-wrap: anywhere; }
  .where { overflow-wrap: anywhere; }
  .label { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .say { font-size: var(--text-sm); color: var(--text-1); overflow-wrap: anywhere; }
  .library { overflow-wrap: anywhere; }
  .actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; }
  .place { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; justify-content: flex-end; }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  /* Below this the tracks do not fit beside the sidebar. */
  @media (max-width: 1180px) {
    .row { grid-template-columns: 1fr; gap: 6px; }
    .row.header { display: none; }
    .actions, .place { justify-content: flex-start; }
  }
</style>
