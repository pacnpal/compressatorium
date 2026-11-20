<script>
  /**
   * Settings → Connections: what Compressatorium talks to. RomM is edited on
   * its Library page (one home), so this only says whether it answers. The
   * Hasheous switch lives here; turning it on tests it at once
   * (`datMatching.hasheousTest`). The *arr webhook is set up per watch folder.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { hashFor } from '$lib/router/routes.js';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import HasheousQuestion from './HasheousQuestion.svelte';

  const rommHost = $derived.by(() => {
    try {
      return new URL(romm.settings?.url ?? '').host;
    } catch {
      return '';
    }
  });

  const stats = $derived(datMatching.stats);
  const hasheousOn = $derived(Boolean(stats?.hasheous_enabled));
  // Asked once: until the user answers, this is a yes/no question.
  const undecided = $derived(Boolean(stats) && !stats.hasheous_overridden && !hasheousOn);
  const test = $derived(datMatching.hasheousTest);
  let hasheousLoading = $state(false);
  let saving = $state(false);

  async function loadHasheous() {
    hasheousLoading = true;
    try {
      await datMatching.refreshMatchingAvailability();
    } finally {
      hasheousLoading = false;
    }
  }

  async function setHasheous(on) {
    saving = true;
    try {
      await datMatching.setHasheousEnabled(on);
    } catch (e) {
      toast.error(e?.message ?? "Couldn't change the Hasheous setting");
    } finally {
      saving = false;
    }
  }

  function loadRomm() {
    romm.loadStatus();
    romm.loadSettings();
  }

  onMount(() => {
    if (!romm.status && !romm.statusLoading) romm.loadStatus();
    if (!romm.settings) romm.loadSettings();
    if (!datMatching.stats) loadHasheous();
  });
</script>

<ul class="rows">
  <li class="row">
    <div class="what">
      <span class="name">RomM</span>
      {#if romm.status == null && romm.statusLoading}
        <span class="state"><Spinner size="sm" /> Checking…</span>
      {:else if romm.status == null && romm.statusError}
        <span class="bad">Couldn't load this. {romm.statusError}</span>
        <Button size="sm" variant="secondary" onclick={loadRomm}>Try again</Button>
      {:else if romm.connected}
        <span class="good">✓ Connected{rommHost ? ` to ${rommHost}` : ''}</span>
      {:else if romm.configured}
        <span class="bad">Set up, but not answering{romm.status?.error ? `: ${romm.status.error}` : ''}</span>
      {:else}
        <span class="muted">Not connected</span>
      {/if}
    </div>
    <a class="link" href={hashFor('libraries', { id: 'romm', tab: 'connection' })}>Manage</a>
  </li>

  <li class="row stack">
    {#if stats == null && hasheousLoading}
      <span class="state"><Spinner size="sm" /> Checking…</span>
    {:else if stats == null}
      <div class="what">
        <span class="bad">Couldn't load this.</span>
        <Button size="sm" variant="secondary" onclick={loadHasheous}>Try again</Button>
      </div>
    {:else if undecided}
      <HasheousQuestion busy={saving} onanswer={setHasheous} />
    {:else}
      <Checkbox
        checked={hasheousOn}
        disabled={saving}
        label="Identify games online (Hasheous)"
        description="Looks up each game's checksum on hasheous.org to name it and tell good dumps from bad. Only checksums are sent."
        onchange={setHasheous}
      />
      {#if hasheousOn && test}
        <p class="test" role="status">
          {#if test.pending}
            <span class="state"><Spinner size="sm" /> Asking Hasheous…</span>
          {:else if test.ok}
            <span class="good">✓ Reached Hasheous</span>
          {:else}
            <span class="bad">✗ Couldn't reach Hasheous: {test.error}</span>
          {/if}
        </p>
      {/if}
    {/if}
  </li>

  <li class="row">
    <div class="what">
      <span class="name">Sonarr, Radarr, ROMarr</span>
      <span class="muted">Set up from a Library's watch folders.</span>
    </div>
    <a class="link" href={hashFor('libraries')}>Open Libraries</a>
  </li>
</ul>

<style>
  .rows { list-style: none; margin: 0; padding: 0; }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-3);
    padding: var(--space-3) 0;
    border-bottom: 1px solid var(--border-subtle);
    min-width: 0;
  }
  .row:first-child { padding-top: 0; }
  .row:last-child { border-bottom: none; padding-bottom: 0; }
  .row.stack { flex-direction: column; align-items: stretch; }
  .what { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2) var(--space-3); min-width: 0; }
  .name { font-weight: var(--weight-medium); color: var(--text-1); }
  .muted { color: var(--text-2); }
  .good { color: var(--success); }
  .bad { color: var(--error); overflow-wrap: anywhere; }
  .state { display: inline-flex; align-items: center; gap: var(--space-2); color: var(--text-2); }
  .test { margin: 0; font-size: var(--text-sm); }
  .link { color: var(--accent); text-decoration: none; }
  .link:hover { text-decoration: underline; }
</style>
