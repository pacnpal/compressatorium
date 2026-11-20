<script>
  /**
   * Settings → Defaults: what a new conversion starts with.
   *
   * - The safety choices, in the shared `defaults` store (the Convert panel
   *   and a new console row start from it).
   * - The remembered compression per format: the same per-tool preference the
   *   Convert panel saves (`/api/preferences/conversion`), one picker per tool
   *   whose default mode compresses. The PUT replaces the whole object, so
   *   it's merged here, and the Convert panel's copy is reloaded after.
   * - How long History keeps its rows (`/api/ledger/settings`).
   *
   * Each part loads on its own and shows its own error.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { defaults } from '$lib/stores/defaults.svelte.js';
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { registry } from '$lib/tools/registry.js';
  import { targetLabel } from '$lib/tools/convertTargets.js';
  import { formatCompression, parseCompression } from '$lib/tools/compression.js';
  import SafetyOptions from '$lib/components/panels/SafetyOptions.svelte';
  import CompressionPicker from '$lib/components/panels/CompressionPicker.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';

  // ─── When converting ────────────────────────────────────────────────────
  // Every mode that can delete its original, so every choice shows.
  const deleteSpecs = $derived(
    registry.all().flatMap((t) => t.modes).filter((s) => s.supportsDeleteOnVerify),
  );

  async function saveSafety(s) {
    try {
      await defaults.save({
        verify_after: s.checkAfter,
        delete_on_verify: s.deleteOriginal,
        container_only: s.quickCheck,
      });
    } catch (e) {
      toast.error(e?.message ?? "Couldn't save the defaults");
    }
  }

  // ─── Compression ────────────────────────────────────────────────────────
  const pickers = $derived(
    registry.all()
      .map((tool) => ({ tool, mode: tool.defaultMode, spec: registry.specFor(tool.defaultMode) }))
      .filter(({ spec }) => spec?.supportsCompression || spec?.supportsCompressionLevel)
      .map((p) => ({ ...p, label: targetLabel(p.spec.outputExt || p.mode) })),
  );
  let prefs = $state(null);
  let prefsError = $state(null);
  let saveTimer = null;
  let saveChain = Promise.resolve();

  async function loadPrefs() {
    prefsError = null;
    try {
      prefs = (await api.getConversionPrefs()) ?? {};
    } catch (e) {
      prefsError = e?.message ?? 'Request failed';
    }
  }

  function setCompression(p, value) {
    prefs = { ...prefs, [p.tool.id]: formatCompression(p.mode, value) };
    // Debounced (a level box sends every keystroke); saves go one at a time.
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const body = Object.fromEntries(Object.entries(prefs).filter(([, v]) => v != null));
      saveChain = saveChain.then(async () => {
        try {
          await api.putConversionPrefs(body);
          await conversion.loadServerPrefs();
        } catch (e) {
          toast.error(e?.message ?? "Couldn't save the compression");
        }
      });
    }, 600);
  }

  // ─── History ────────────────────────────────────────────────────────────
  const RETENTION = [
    { value: '', label: 'Forever' },
    { value: '30', label: '30 days' },
    { value: '90', label: '90 days' },
    { value: '365', label: '1 year' },
  ];
  let retention = $state(null);
  let retentionError = $state(null);
  // A value set through the API that isn't one of the four still shows.
  const retentionOptions = $derived(
    retention && !RETENTION.some((o) => o.value === retention)
      ? [...RETENTION, { value: retention, label: `${retention} days` }]
      : RETENTION,
  );
  const asOption = (r) => (r?.retention_days ? String(r.retention_days) : '');

  async function loadRetention() {
    retentionError = null;
    try {
      retention = asOption(await api.getLedgerSettings());
    } catch (e) {
      retentionError = e?.message ?? 'Request failed';
    }
  }

  async function setRetention(value) {
    const before = retention;
    retention = value;
    try {
      retention = asOption(await api.putLedgerSettings({ retention_days: value ? Number(value) : null }));
    } catch (e) {
      retention = before;
      toast.error(e?.message ?? "Couldn't save the History setting");
    }
  }

  onMount(() => {
    loadPrefs();
    loadRetention();
    return () => clearTimeout(saveTimer);
  });
</script>

{#snippet failed(detail, retry)}
  <div class="error" role="alert">
    Couldn't load this. {detail}
    <Button size="sm" variant="secondary" onclick={retry}>Try again</Button>
  </div>
{/snippet}

<div class="parts">
  <section class="part" aria-labelledby="defaults-safety">
    <h3 id="defaults-safety">When converting</h3>
    <p class="hint">New conversions and new console settings start with these. You can change them each time.</p>
    {#if !defaults.loaded}
      <div class="state"><Spinner size="sm" /> Loading…</div>
    {:else}
      <SafetyOptions
        specs={deleteSpecs}
        checkAfter={defaults.values.verify_after}
        deleteOriginal={defaults.values.delete_on_verify}
        quickCheck={defaults.values.container_only}
        onchange={saveSafety}
        idPrefix="defaults-safety-options"
      />
    {/if}
  </section>

  <section class="part" aria-labelledby="defaults-compression">
    <h3 id="defaults-compression">Compression</h3>
    <p class="hint">Used whenever a game is converted to this format.</p>
    {#if prefsError}
      {@render failed(prefsError, loadPrefs)}
    {:else if prefs == null}
      <div class="state"><Spinner size="sm" /> Loading…</div>
    {:else}
      <div class="pickers">
        {#each pickers as p (p.tool.id)}
          <CompressionPicker
            mode={p.mode}
            label={p.label}
            value={parseCompression(p.mode, prefs[p.tool.id])}
            onchange={(v) => setCompression(p, v)}
          />
        {/each}
      </div>
    {/if}
  </section>

  <section class="part" aria-labelledby="defaults-history">
    <h3 id="defaults-history">History</h3>
    {#if retentionError}
      {@render failed(retentionError, loadRetention)}
    {:else if retention == null}
      <div class="state"><Spinner size="sm" /> Loading…</div>
    {:else}
      <div class="retention">
        <Select
          label="Keep history for"
          value={retention}
          options={retentionOptions}
          onchange={setRetention}
        />
      </div>
    {/if}
  </section>
</div>

<style>
  .parts { display: flex; flex-direction: column; gap: var(--space-5); }
  .part { min-width: 0; }
  .part h3 {
    margin: 0;
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .hint { margin: var(--space-1) 0 var(--space-3); color: var(--text-2); font-size: var(--text-sm); }
  .part h3 + :not(.hint) { margin-top: var(--space-3); }
  .pickers {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
    gap: var(--space-3);
  }
  .retention { max-width: 240px; }
  .state { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); font-size: var(--text-sm); }
  .error {
    display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2);
    color: var(--error); background: var(--error-muted);
    padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); font-size: var(--text-sm);
  }
</style>
