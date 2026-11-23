<script>
  /**
   * RomM's Browse tab: pick a console, see what's converted and what's left,
   * and convert from the same panel Files uses. The backend returns a RomM
   * console's ROMs in the same DirectoryListing/FileEntry shape a folder
   * listing uses, so FileList, FileRow, RowActionsMenu and ConvertPanel are
   * reused as they are. The view (LibraryView) loads RomM's status,
   * consoles and Rules, and puts the file browser back when it is left.
   */
  import { toast } from 'svelte-sonner';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { layout } from '$lib/stores/layout.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { convertTargets } from '$lib/tools/convertTargets.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { formatSize } from '$lib/api/format.js';
  import FileList from '$lib/components/panels/FileList.svelte';
  import ConvertPanel from '$lib/components/panels/ConvertPanel.svelte';
  import Splitter from '$lib/components/ui/Splitter.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import {
    ARCHIVE_EXTS, isDatMatchable, needsDatMatch, wantsHasheousIds,
  } from '$lib/util/datMatchable.js';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  let dragStartRight = 0;

  const status = $derived(romm.status);
  const entries = $derived(fileBrowser.entries);

  /**
   * Whether anything here can convert the chosen console, narrowed by the
   * backend's own `mode_ids` / `tool_ids` for it. The Convert panel's own
   * Convert to offers the same narrowed list.
   */
  const modeGroups = $derived.by(() => {
    const platform = romm.platforms.find((p) => p.id === romm.selectedPlatformId);
    return convertTargets({
      perMode: true,
      modeIds: Array.isArray(platform?.mode_ids) ? platform.mode_ids : null,
      toolIds: Array.isArray(platform?.tool_ids) ? platform.tool_ids : null,
    }).filter((g) => g.group !== 'Recommended');
  });
  const hasModes = $derived(modeGroups.length > 0);

  const selectedPlatformName = $derived(
    romm.platforms.find((p) => p.id === romm.selectedPlatformId)?.name
      ?? 'this console',
  );

  const platformOptions = $derived(
    romm.platforms.map((p) => ({
      value: String(p.id),
      label: p.rom_count ? `${p.name} (${p.rom_count})` : p.name,
    })),
  );

  // The mode Convert to names, or for a format (`.rvz`) just its extension:
  // enough for the "RomM can't match" note.
  const activeMode = $derived(
    registry.specFor(conversion.mode)
      ?? (conversion.targetExt ? { outputExt: conversion.targetExt } : null),
  );
  const losesMatch = $derived(romm.losesDatMatch(activeMode));

  /**
   * Library health for the selected platform.
   *
   * `outputs` alone is not "converted": detect_output also reports an output a
   * job is still writing (exists=false, ready=false). Counting those as done
   * would shrink the pending total while the work is still queued, so only a
   * `ready` output counts.
   *
   * "To go" is what the panel could actually submit, asked through the same
   * `allowsInputEntry` gate that decides whether a row is selectable. The
   * row's `convertible_by` is a tool-level, mode-agnostic annotation and the
   * two disagree by design — a PS2 ISO is annotated convertible by CHDMAN, but
   * CHDMAN's extract modes take a `.chd` and cannot consume it — so counting
   * from the annotation promised savings on files the button would refuse.
   *
   * The savings figure uses the mode's expected output/input ratio — the same
   * SIZE_RATIOS table the progress bar estimates from — and is labelled as an
   * estimate, because real ratios vary per title.
   */
  const summary = $derived.by(() => {
    let converted = 0;
    let pending = 0;
    let pendingBytes = 0;
    let convertedBytes = 0;
    // Judge each ROM against the target the panel is actually configured to
    // produce. Counting any ready output from any tool called a PS2 ISO
    // "converted" because a CSO sat beside it, even with CHD selected — so the
    // count and the savings estimate described a different conversion than the
    // one the button would run.
    //
    // The tool alone is still too coarse: dolphin-tool emits RVZ, WIA and GCZ,
    // so a stray .gcz beside a GameCube ISO reported it converted while RVZ was
    // selected. Match the mode's own extension when the mode declares one, and
    // fall back to the tool for the modes that do not (z3ds and nsz derive
    // their suffix from the input).
    // The format Convert to names (`.chd`, or a mode's own); with Best, each
    // game's planned file, where the plan has it.
    const activeToolId = conversion.currentTool?.id ?? null;
    const activeExt = conversion.targetExt?.toLowerCase() ?? null;
    const planned = Object.fromEntries(
      (conversion.plan ?? []).filter((row) => row.output_name)
        .map((row) => [row.file_path, row.output_name.toLowerCase()]),
    );
    for (const e of entries) {
      const outs = (e.outputs ?? []).filter((o) => {
        if (activeToolId && o.tool_id !== activeToolId) return false;
        const name = (o.path ?? '').toLowerCase();
        if (activeExt) return name.endsWith(activeExt);
        const wanted = planned[e.path];
        return !wanted || name.split(/[\\/]/).pop() === wanted;
      });
      const done = outs.some((o) => o.ready);
      // Any non-ready status means work is under way. A detector returns None
      // — and so contributes no status at all — when there is nothing there,
      // so the only way to see one is for a job to hold the destination. It
      // reports exists=false until the tool creates the file, and requiring
      // `exists` counted that whole window as still to do.
      const working = outs.some((o) => !o.ready);
      if (done) {
        converted += 1;
        convertedBytes += e.size ?? 0;
      } else if (!working && conversion.allowsInputEntry(e)) {
        pending += 1;
        pendingBytes += e.size ?? 0;
      }
    }
    const ratio = romm.sizeRatioFor(conversion.mode);
    const estimatedSaving = ratio !== null ? Math.max(0, pendingBytes * (1 - ratio)) : null;
    return {
      converted, pending, pendingBytes, convertedBytes,
      total: entries.length, estimatedSaving,
    };
  });

  /**
   * Whether the counts above can be trusted for the current settings.
   *
   * Each row's `outputs` describes what the backend found *beside the source*.
   * Point the Convert panel at an output folder and the finished files are
   * somewhere else entirely, so every converted ROM reads as still to go — and
   * the savings figure promises space that was already reclaimed. Rather than
   * re-scanning a whole platform against a folder that changes as it is typed,
   * the summary says what it does not cover.
   */
  const summaryCoversOutputs = $derived(!conversion.outputDir);

  /**
   * "Unidentified in RomM" (#427): the rows RomM couldn't identify, and how
   * many of those the app Recognizes from its own DAT or Hasheous match.
   *
   * Read from the match store, which the listing seeds and a match job
   * updates, so the count moves as "Recognize these" runs without reloading
   * the platform, and a policy change clears it with every other badge.
   */
  const allUnidentified = $derived(entries.filter((e) => e.romm_identified === false));
  // Only what the matcher can identify, by the file list's own rule: an
  // archived ROM's match would hash the compressed bytes and record a miss
  // (Codex on PR #442), so those are counted apart and never submitted.
  const matchableExts = registry.allFilterableExts();
  const unidentifiedRows = $derived(
    allUnidentified.filter((e) => isDatMatchable(e, matchableExts)),
  );
  const archivedUnidentified = $derived(
    allUnidentified.filter((e) => ARCHIVE_EXTS.has((e.extension ?? '').toLowerCase())).length,
  );
  const recognizedUnidentified = $derived(
    unidentifiedRows.filter((e) => datMatching.matchFor(e.path)?.matched).length,
  );
  // With the Hasheous ids switch on, a Hasheous hit cached before the ids were
  // kept has none to write; the match job looks it up again (Codex on PR #442).
  // The same gate as the backend's: the switch and re-pinning both on.
  const wantsIds = $derived(wantsHasheousIds(romm.settings));
  // What "Recognize these" hands the match job: the files among those rows,
  // on every page, the app has no verdict for yet (or no Hasheous ids for,
  // when they are wanted). A cached miss is not asked again.
  const toRecognize = $derived(
    unidentifiedRows
      .filter((e) => needsDatMatch(datMatching.matchFor(e.path), wantsIds))
      .map((e) => e.path),
  );
  // The file list re-reads only the rows on screen when a match job ends; the
  // job "Recognize these" started covers every page, so all of it is read back.
  $effect(() => {
    datMatching.hydrateFinishedJobs(jobs.jobs);
  });
  let recognizing = $state(false);

  async function handleRecognize() {
    recognizing = true;
    try {
      const response = await datMatching.startMatchJob(toRecognize);
      // The jobs list may already show this job ended, and the effect below
      // runs only when that list changes: read it back once now as well.
      datMatching.hydrateFinishedJobs(jobs.jobs);
      if (response?.status === 'started') {
        toast.info(`Matching ${toRecognize.length} ${toRecognize.length === 1 ? 'game' : 'games'}. Progress shows in Activity.`);
      } else {
        toast.info('Nothing new to match: these already have an answer');
      }
    } catch (e) {
      toast.error(e?.message ?? 'Could not start the match job');
    } finally {
      recognizing = false;
    }
  }

  async function handleRepin() {
    try {
      const result = await romm.runRepin();
      const done = result?.repinned ?? 0;
      const failed = result?.failed ?? 0;
      if (done > 0) {
        toast.success(`Re-matched ${done} ROM${done === 1 ? '' : 's'} in RomM`);
      } else if (result?.busy) {
        // Another pass owns the lock. The rows are still queued, so reporting
        // "nothing waiting" would contradict the badge beside this button.
        toast.info('A re-match pass is already running — its results will land shortly');
      } else if (failed > 0) {
        toast.error(
          `${failed} re-match${failed === 1 ? '' : 'es'} failed; they stay queued `
          + 'and are retried on the next pass',
        );
      } else if ((result?.waiting ?? 0) > 0) {
        toast.info('RomM has not rescanned these yet — try again after a scan');
      } else if ((result?.pending ?? 0) > 0) {
        // Nothing was reached this pass (the wall-clock budget ran out), but
        // rows remain. "Nothing waiting" would be plainly wrong.
        toast.info('Ran out of time this pass — the rest are picked up next time');
      } else {
        toast.info('Nothing waiting to be re-matched');
      }
    } catch (e) {
      toast.error(e?.message ?? 'Re-match failed');
    }
  }
</script>

<section class="browse">
  {#if romm.statusLoading && !romm.status}
    <div class="notice"><Spinner /> Checking RomM connection…</div>
  {:else if !romm.configured}
    <EmptyState
      title="RomM isn't connected yet"
      description="Connect it once and its consoles show up here."
    >
      {#snippet actions()}
        <Button onclick={() => ui.navigate('libraries', { id: 'romm', tab: 'connection' })}>
          Connect RomM
        </Button>
      {/snippet}
    </EmptyState>
  {:else if !romm.connected}
    <div class="notice error">
      <TriangleAlert size={16} />
      <span>Couldn't reach RomM.{status?.error ? ` ${String(status.error).replace(/^[^:]*:\s*/, '')}` : ''}</span>
      <Button size="sm" variant="secondary" onclick={() => romm.loadStatus()}>Try again</Button>
      <Button size="sm" variant="ghost" onclick={() => ui.navigate('libraries', { id: 'romm', tab: 'connection' })}>
        Check the connection
      </Button>
    </div>
  {:else if status && !status.libraryRootMounted}
    <div class="notice error">
      <TriangleAlert size={16} />
      <span>
        RomM's games folder ({status.libraryRoot || 'not set'}) isn't here. Add it to
        the container, then check the Connection tab.
      </span>
    </div>
  {:else}
    <div class="toolbar">
      <div class="picker">
        {#if romm.platformsLoading}
          <Spinner />
        {:else if romm.platformsError}
          <!-- The heartbeat is unauthenticated, so a bad token surfaces here
               rather than as a connection failure. Saying "no platforms" would
               make a broken token look like an empty library. -->
          <span class="error-text">{romm.platformsError}</span>
        {:else if platformOptions.length > 0}
          <Select
            label="Console"
            value={String(romm.selectedPlatformId ?? '')}
            options={platformOptions}
            onchange={(v) => romm.selectPlatform(v)}
          />
        {:else}
          <span class="muted">No consoles in RomM.</span>
        {/if}
        {#if romm.pendingRepins > 0}
          <Badge tone="warning">{romm.pendingRepins} awaiting re-match</Badge>
          <Button
            size="sm" variant="secondary" loading={romm.repinning}
            onclick={handleRepin} icon={refreshIcon}
          >Re-match in RomM</Button>
        {/if}
      </div>

      {#if summary.total > 0 && summaryCoversOutputs}
        <div class="summary" aria-live="polite">
          <span><strong>{summary.converted}</strong> converted</span>
          <span><strong>{summary.pending}</strong> to go</span>
          {#if summary.pendingBytes > 0}
            <span class="muted">({formatSize(summary.pendingBytes)} of sources)</span>
          {/if}
          {#if summary.estimatedSaving}
            <span class="saving">
              ≈ {formatSize(summary.estimatedSaving)} could be saved
            </span>
          {/if}
        </div>
      {:else if summary.total > 0}
        <div class="summary" aria-live="polite">
          <span class="muted">
            {summary.total} games. Progress isn't counted while new files go to another
            folder.
          </span>
        </div>
      {/if}
    </div>

    {#if romm.selectedPlatformId !== null}
      <div class="unidentified" aria-live="polite">
        <Checkbox
          checked={fileBrowser.rommUnidentified}
          label={fileBrowser.rommUnidentified || fileBrowser.rommSummary == null
            ? 'Unidentified in RomM'
            : `Unidentified in RomM (${fileBrowser.rommSummary.unidentified})`}
          onchange={(v) => fileBrowser.setRommUnidentified(v)}
        />
        {#if fileBrowser.rommUnidentified && !fileBrowser.loading}
          <span class="muted">
            <strong>{recognizedUnidentified}</strong> of {unidentifiedRows.length} are known
            good dumps from your game lists or Hasheous, so a console set to "Only known
            good dumps" converts them too.
          </span>
          {#if archivedUnidentified > 0}
            <span class="muted">
              Archived ROMs (zip, 7z) aren't recognized yet, so
              {archivedUnidentified === 1 ? 'the 1 here is' : `the ${archivedUnidentified} here are`}
              left out of the count and the match.
            </span>
          {/if}
          {#if toRecognize.length > 0}
            <Button
              size="sm" variant="secondary" loading={recognizing}
              onclick={handleRecognize}
            >Recognize these ({toRecognize.length})</Button>
          {/if}
        {/if}
      </div>
    {/if}

    {#if losesMatch}
      <div class="notice warn">
        <TriangleAlert size={16} />
        <span>
          RomM can't match {activeMode?.outputExt?.replace(/^\./, '').toUpperCase()} files by
          hash.
          <!-- The promise follows the setting: with it off (or unread) nothing
               is saved, so these lose their RomM match. -->
          {#if romm.settings && romm.settings.repin_enabled !== false}
            Compressatorium keeps their RomM details and puts them back after RomM's
            next scan.
          {:else}
            Keeping RomM's details is off or couldn't be read (Connection, More), so
            these lose their RomM match and need identifying by hand.
          {/if}
        </span>
      </div>
    {/if}

    <div class="grid" style="--ws-right: {layout.panels.right}px;">
      <article class="main">
        {#if fileBrowser.entriesError}
          <div class="notice error">
            <TriangleAlert size={16} />
            <span>{fileBrowser.entriesError}</span>
          </div>
        {:else if !fileBrowser.loading && entries.length === 0
          && fileBrowser.rommUnidentified && (fileBrowser.rommDiagnostics?.total ?? 0) === 0}
          <EmptyState
            title="RomM identified every ROM here"
            description="RomM knows every game on this console. Clear the filter to see them all."
          />
        {:else if !fileBrowser.loading && entries.length === 0 && romm.selectedPlatformId !== null}
          {@const diag = fileBrowser.rommDiagnostics}
          {#if diag && diag.total > 0}
            <div class="diag-empty">
              <span class="diag-glyph" aria-hidden="true">∅</span>
              <h3>None of these games are on your Drives</h3>
              <p>
                RomM lists {diag.total} {diag.total === 1 ? 'game' : 'games'} for this console,
                but none resolved to a file on disk.
              </p>
              <dl class="diag-breakdown">
                {#if diag.not_found > 0}
                  <div class="diag-row">
                    <dt>{diag.not_found}</dt>
                    <dd>file not found on disk</dd>
                  </div>
                {/if}
                {#if diag.outside_volumes > 0}
                  <div class="diag-row">
                    <dt>{diag.outside_volumes}</dt>
                    <dd>not on any of your Drives</dd>
                  </div>
                {/if}
                {#if diag.no_path > 0}
                  <div class="diag-row">
                    <dt>{diag.no_path}</dt>
                    <dd>no usable path in ROM record</dd>
                  </div>
                {/if}
                {#if diag.not_readable > 0}
                  <div class="diag-row">
                    <dt>{diag.not_readable}</dt>
                    <dd>file not readable (permissions or stale mount)</dd>
                  </div>
                {/if}
                {#if diag.not_regular > 0}
                  <div class="diag-row">
                    <dt>{diag.not_regular}</dt>
                    <dd>not a regular file or directory</dd>
                  </div>
                {/if}
              </dl>
              {#if diag.library_root || diag.volumes.length > 0}
                <dl class="diag-config">
                  {#if diag.library_root}
                    <div class="diag-row">
                      <dt>Library root</dt>
                      <dd><code>{diag.library_root}</code></dd>
                    </div>
                  {/if}
                  {#if diag.volumes.length > 0}
                    <div class="diag-row">
                      <dt>Drives</dt>
                      <dd><code>{diag.volumes.join(', ')}</code></dd>
                    </div>
                  {/if}
                </dl>
              {/if}
              {#if diag.sample_paths.length > 0}
                <details class="diag-samples">
                  <summary>Sample paths attempted</summary>
                  <ul>
                    {#each diag.sample_paths as p, i (i)}
                      <li><code>{p}</code></li>
                    {/each}
                  </ul>
                </details>
              {/if}
            </div>
          {:else}
            <EmptyState
              title="None of these games are on your Drives"
              description="RomM lists games for this console, but none of them are files on your Drives."
            />
          {/if}
        {:else}
          <FileList />
        {/if}
      </article>

      <Splitter
        variant="panel"
        label="Resize the convert panel"
        value={layout.panels.right}
        min={280}
        max={640}
        onstart={() => (dragStartRight = layout.panels.right)}
        onmove={(d) => layout.setPanelWidth('right', dragStartRight - d)}
        onstep={(d) => layout.setPanelWidth('right', layout.panels.right - d)}
        onreset={() => layout.resetPanel('right')}
      />

      <article class="right">
        {#if romm.selectedPlatformUnsupported || !hasModes}
          <!-- Nothing installed converts this platform. The panel would still
               submit against whatever tool the workspace was last left on —
               a PS2 ISO to Dolphin RVZ — so it is replaced rather than
               disabled, and the reason is named. -->
          <div class="notice warn">
            <TriangleAlert size={16} />
            <span>
              Nothing here converts <strong>{selectedPlatformName}</strong> games. Pick
              another console.
            </span>
          </div>
        {:else}
          <ConvertPanel />
        {/if}
      </article>
    </div>
  {/if}
</section>

{#snippet refreshIcon()}<RefreshCw size={14} />{/snippet}

<style>
  .browse {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    min-width: 0;
    /* The panel layout follows this view's width, not the window's (#562). */
    container-type: inline-size;
  }
  .convert-to { display: inline-flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .convert-to .label {
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .convert-to select {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
    max-width: 100%;
  }
  .toolbar {
    display: flex;
    align-items: flex-end;
    gap: var(--space-4);
    flex-wrap: wrap;
  }
  .picker { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--space-3); min-width: 0; }
  .summary {
    display: flex;
    gap: var(--space-3);
    align-items: baseline;
    flex-wrap: wrap;
    color: var(--text-2);
  }
  .summary strong { color: var(--text-1); }
  .unidentified {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    flex-wrap: wrap;
  }
  .unidentified strong { color: var(--text-1); }
  .saving { color: var(--accent); font-weight: var(--weight-medium); }
  .muted { color: var(--text-2); }
  .error-text { color: var(--error); }

  .notice {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--surface-2);
    color: var(--text-2);
  }
  .notice.error {
    border-color: var(--danger-border, var(--border-subtle));
    color: var(--error);
  }
  .notice.warn {
    border-color: var(--warning);
    color: var(--text-1);
  }

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
    min-width: 0;
  }
  .grid > :global(.splitter) { display: none; }
  @media (min-width: 900px) {
    /* Two-column mode stacks the convert/jobs panel full-width below the file
       list; without the span it auto-places into a narrow column. */
    .right { grid-column: 1 / -1; }
  }
  /* Convert beside the list only when the list still holds the whole table
     at its default widths (#562): the Convert panel (360), a 9px splitter and
     two 8px gaps, and the 36px of padding and borders (the list and its table
     frame) around a 676px table. Narrower, it stacks below. */
  @container (min-width: 1100px) {
    .grid {
      grid-template-columns: minmax(0, 1fr) auto var(--ws-right, 360px);
      gap: var(--space-2);
    }
    .grid > :global(.splitter) { display: block; }
    .right { grid-column: auto; }
  }

  .main, .right {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
    min-width: 0;
  }
  .right { display: flex; flex-direction: column; gap: var(--space-4); }

  .diag-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-7) var(--space-4);
    text-align: center;
    color: var(--text-2);
  }
  .diag-glyph {
    font-size: 36px;
    color: var(--text-3);
  }
  .diag-empty h3 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .diag-empty p {
    margin: 0;
    max-width: 44ch;
    color: var(--text-2);
  }
  .diag-breakdown, .diag-config {
    margin: var(--space-3) 0 0;
    padding: var(--space-3);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    text-align: left;
    width: 100%;
    max-width: 400px;
  }
  .diag-breakdown .diag-row {
    display: flex;
    gap: var(--space-2);
    padding: var(--space-1) 0;
  }
  .diag-breakdown dt {
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    min-width: 3ch;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .diag-breakdown dd { margin: 0; }
  .diag-config { margin-top: var(--space-2); }
  .diag-config .diag-row {
    display: flex;
    gap: var(--space-2);
    padding: var(--space-1) 0;
    flex-wrap: wrap;
  }
  .diag-config dt {
    font-weight: var(--weight-medium);
    color: var(--text-2);
    white-space: nowrap;
  }
  .diag-config dd { margin: 0; }
  .diag-config code, .diag-samples code {
    font-size: var(--text-xs);
    background: var(--surface-3, var(--surface-2));
    padding: 1px 4px;
    border-radius: var(--radius-sm);
    word-break: break-all;
  }
  .diag-samples {
    margin-top: var(--space-2);
    text-align: left;
    width: 100%;
    max-width: 400px;
  }
  .diag-samples summary {
    cursor: pointer;
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .diag-samples ul {
    list-style: none;
    padding: var(--space-2) 0 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .diag-samples li {
    font-size: var(--text-xs);
    color: var(--text-2);
  }
</style>
