<script>
  /**
   * A Library's Automation tab: whether it converts on its own, each
   * console's format (the console table), its watch folders, where new games
   * go and the game list app it feeds. Every change saves on its own. The
   * view (LibraryView) loads the source's consoles and Rules.
   *
   * @typedef {Object} Props
   * @property {object} source the Library's rules source
   * @property {object|null} library the Library row
   */
  import { toast } from 'svelte-sonner';
  import { registry } from '$lib/tools/registry.js';
  import { defaults } from '$lib/stores/defaults.svelte.js';
  import { newCheckRule } from '$lib/rules/checkRule.js';
  import { newRuleSafety, nightlyCheckState } from '$lib/rules/ruleTable.js';
  import { nothingReason } from '$lib/rules/sweepSummary.js';
  import RuleTable from './RuleTable.svelte';
  import WatchFolders from './WatchFolders.svelte';
  import FrontendSelect from './FrontendSelect.svelte';
  import LibraryPlacement from '$lib/components/views/LibraryPlacement.svelte';
  import Panel from '$lib/components/ui/Panel.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Eye from '@lucide/svelte/icons/eye';
  import Play from '@lucide/svelte/icons/play';

  /** @type {Props} */
  let { source, library } = $props();

  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  // A folder is read one listing at a time: while one runs, look again in 3 s.
  const RETRY_MS = 3000;
  $effect(() => {
    if (!source.platformsBusy) return;
    const timer = setTimeout(() => source.loadPlatforms(), RETRY_MS);
    return () => clearTimeout(timer);
  });
  const platforms = $derived(source.platforms ?? []);
  const ids = $derived(platforms.map((p) => String(p.id)));
  const nightly = $derived(nightlyCheckState(ids, source.rules, source.checkRules));
  const converting = $derived(ids.filter((id) => source.rules[id]?.mode));
  const unset = $derived(
    platforms.filter((p) => source.imageCount(p) > 0 && !source.rules[String(p.id)]?.mode),
  );
  const converter = $derived(source.activeConverter ?? null);
  const converterCount = $derived(Object.keys(converter?.platformFormats ?? {}).length);
  let applying = $state(false);
  let importing = $state(false);

  // Why a console produced nothing in a run, in plain words.
  const SWEEP_ERROR_LABELS = {
    tool_not_ready: "the converter for this format isn't ready here",
    tool_wrong_for_platform: "the saved format isn't for this console",
    output_dir_outside_volumes: "its folder for new files isn't on a Drive",
    unreadable: "couldn't read this console's games",
    root_unavailable: "couldn't reach the folder (a drive may be down); the next run tries again",
    listing_in_progress: 'the folder is still being read, try again shortly',
    queue_failed: 'the queue refused these games',
    destination_claimed: 'another job took one of the new file names; the next run picks it up',
    library_unresponsive: 'the Library stopped answering partway',
    output_excluded: 'RomM ignores this format, so converted games would vanish from it',
    romm_layout_unread: "RomM's folders couldn't be read yet; the next run asks again",
    libraries_changed: 'a Library was added or removed inside this one; the next run lists it again',
  };

  /** One line for what a Preview or Run now did. */
  function summary(r) {
    const preview = !!r.dry_run;
    const queued = r.queued ?? 0;
    const checks = r.checks ?? 0;
    const done = r.skipped_existing ?? 0;
    const parts = [];
    if (queued) parts.push(`${queued} ${preview ? 'would convert' : 'converting'}`);
    if (checks) parts.push(`${checks} ${preview ? 'would be checked' : 'being checked'}`);
    if (done) parts.push(`${done} already done`);
    if (r.skipped_unsafe) parts.push(`${r.skipped_unsafe} skipped: a track sheet names a file outside your Drives`);
    if (r.stopped_reason === 'limit') parts.push('stopped at the per-run limit');
    if (!queued && !checks) return `Nothing to do: ${nothingReason(r)}`;
    return `${preview ? 'Preview' : 'Running'}: ${parts.join(' · ')}`;
  }

  async function committed() {
    if (await source.flush()) return true;
    toast.error(`Couldn't save first: ${source.saveError ?? 'try again in a moment'}`);
    return false;
  }

  async function preview() {
    if (!(await committed())) return;
    try {
      await source.previewSweep();
    } catch (e) {
      toast.error(e?.message ?? "Couldn't preview");
    }
  }

  async function runNow() {
    if (!(await committed())) return;
    try {
      await source.runSweep();
    } catch (e) {
      toast.error(e?.message ?? "Couldn't run");
    }
  }

  // Turning it on saves first: the scheduler reads the server's Rules, so an
  // edit still waiting would run on the previous ones. Off needs no care.
  async function setAuto(enabled) {
    if (enabled && !(await committed())) return;
    try {
      await source.setScheduleEnabled(enabled);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't save");
    }
  }

  /** Every converting console's nightly re-check on, or all of them off. */
  function setNightly(on) {
    for (const id of on ? converting : Object.keys(source.checkRules)) {
      const current = source.checkRuleFor(id)
        ?? { ...newCheckRule(source.ruleDefaults), timezone: zone };
      source.setCheckRule(id, { ...current, enabled: on });
    }
    source.scheduleSave(0);
  }

  async function applyRecommended() {
    applying = true;
    try {
      const { added, skipped } = await source.applyRecommended(
        (mode) => newRuleSafety(registry.specFor(mode), defaults.values),
      );
      const n = added.length;
      const left = skipped.length
        ? `${skipped.length} have no converter here: ${skipped.map((s) => s.name).join(', ')}.`
        : undefined;
      if (!n) {
        toast.info('Nothing to set up', { description: left });
      } else {
        toast.success(`Set up ${n} ${n === 1 ? 'console' : 'consoles'}.`, {
          description: left,
          action: source.scheduleEnabled
            ? undefined
            : { label: 'Turn on automation', onClick: () => setAuto(true) },
        });
      }
    } catch (e) {
      toast.error(e?.message ?? "Couldn't work out the best formats");
    } finally {
      applying = false;
    }
  }

  async function useRommChoices() {
    importing = true;
    try {
      const { created } = await source.importConverterTargets();
      toast.success(created ? `Took RomM's choice for ${created} ${created === 1 ? 'console' : 'consoles'}` : 'Every console RomM converts is set already');
    } catch (e) {
      toast.error(e?.message ?? "Couldn't read RomM's choices");
    } finally {
      importing = false;
    }
  }
</script>

<div class="automation">
  <Panel>
    <div class="run">
      <label class="switch">
        <input
          type="checkbox" role="switch"
          checked={source.scheduleEnabled}
          onchange={(e) => setAuto(e.currentTarget.checked)}
        />
        <span>
          <strong>Convert automatically</strong>
          <span class="muted">Every hour, new and changed games are converted using the settings below.</span>
        </span>
      </label>
      <div class="buttons">
        <Button variant="secondary" onclick={preview} loading={source.sweeping} icon={eyeIcon}>Preview</Button>
        <Button onclick={runNow} loading={source.sweeping} icon={playIcon}>Run now</Button>
      </div>
    </div>
    {#if source.sweepResult}
      {@const r = source.sweepResult}
      <p class="result" role="status">{summary(r)}</p>
      {#if r.errors?.length}
        <ul class="errors">
          {#each r.errors as e, i (i)}
            <li>{source.platformName(e.platform_id)}: {SWEEP_ERROR_LABELS[e.error] ?? e.error}</li>
          {/each}
        </ul>
      {/if}
    {/if}
    <label class="nightly">
      <input
        type="checkbox"
        checked={nightly === true}
        indeterminate={nightly === 'mixed'}
        disabled={converting.length === 0}
        onchange={(e) => setNightly(e.currentTarget.checked)}
      />
      <span>
        Re-check games every night
        <span class="muted">Reads converted games back to catch damage early.</span>
      </span>
    </label>
    {#if converter && converterCount}
      <div class="banner" role="status">
        <span>RomM's own converter is set for {converterCount} {converterCount === 1 ? 'console' : 'consoles'}.</span>
        <Button size="sm" variant="secondary" loading={importing} onclick={useRommChoices}>
          Use RomM's choices
        </Button>
      </div>
    {/if}
  </Panel>

  <Panel>
    <div class="table-head">
      <h2>Consoles</h2>
      <div class="table-actions">
        {#if source.saveState === 'saving'}
          <span class="muted">Saving…</span>
        {:else if source.saveState === 'saved'}
          <span class="muted">Saved</span>
        {:else if source.saveState === 'error'}
          <span class="save-error" role="alert">
            Couldn't save: {source.saveError}
            <Button size="sm" variant="ghost" onclick={() => source.scheduleSave(0)}>Try again</Button>
          </span>
        {/if}
        {#if unset.length > 0}
          <span title="Sets each console to its best format, checks every new file, and keeps originals.">
            <Button variant="secondary" loading={applying} onclick={applyRecommended}>
              Apply recommended
            </Button>
          </span>
        {/if}
      </div>
    </div>
    {#if source.rulesError}
      <p class="state" role="alert">
        Couldn't load this Library's settings. {source.rulesError}
        <Button size="sm" variant="secondary" onclick={() => source.loadRules().catch(() => {})}>Try again</Button>
      </p>
    {:else if source.platformsBusy}
      <!-- A listing of the folder is still running: a wait, not an error. -->
      <p class="state" role="status">
        <Spinner size="sm" label="Reading" /> The folder is still being read.
        <Button size="sm" variant="ghost" onclick={() => source.loadPlatforms()}>Try again</Button>
      </p>
    {:else if source.platformsError}
      <p class="state" role="alert">
        Couldn't list this Library's consoles. {source.platformsError}
        <Button size="sm" variant="secondary" onclick={() => source.loadPlatforms()}>Try again</Button>
      </p>
    {:else if (source.rulesLoading || source.platformsLoading) && platforms.length === 0}
      <p class="muted"><Spinner size="sm" label="Loading" /> Loading…</p>
    {:else if platforms.length === 0}
      <p class="muted">No consoles found yet. Add games, or a watch folder, and they show up here.</p>
    {:else}
      <RuleTable {source} />
    {/if}
  </Panel>

  {#if library}
    <Panel>
      <WatchFolders {library} />
    </Panel>
    <Panel>
      <LibraryPlacement
        {library}
        listing={source.platformsLoading ?? false}
        platforms={source.platforms ?? null}
      />
    </Panel>
  {/if}

  <Panel>
    <FrontendSelect {source} />
  </Panel>
</div>

{#snippet eyeIcon()}<Eye size={14} />{/snippet}
{#snippet playIcon()}<Play size={14} />{/snippet}

<style>
  .automation { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
  .run {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .switch, .nightly { display: flex; align-items: flex-start; gap: var(--space-3); cursor: pointer; }
  .switch > span, .nightly > span { display: flex; flex-direction: column; gap: 2px; }
  .switch input, .nightly input { accent-color: var(--accent); width: 18px; height: 18px; margin-top: 2px; }
  .nightly { margin-top: var(--space-3); font-size: var(--text-sm); }
  .buttons { display: flex; gap: var(--space-2); }
  .result { margin: var(--space-3) 0 0; font-size: var(--text-sm); color: var(--text-1); }
  .errors { margin: var(--space-2) 0 0; padding-left: var(--space-4); font-size: var(--text-sm); color: var(--text-1); }
  .banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    margin-top: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--warning);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
  }
  .table-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  .table-head h2 { margin: 0; font-size: var(--text-lg); font-weight: var(--weight-semibold); }
  .table-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); }
  .save-error { display: inline-flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm); color: var(--error); }
  .state { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin: 0; }
  .muted { color: var(--text-2); font-size: var(--text-sm); }
</style>
