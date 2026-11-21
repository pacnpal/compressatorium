<script>
  /**
   * RomM connection settings, editable in-app: one form, one Save (#532).
   *
   * Save tests the connection first and saves only when it works, so a typo
   * never becomes live config. The test reports reachable / token accepted /
   * mounted separately, a check it couldn't run as "not checked", and names
   * the field to fix. On first run the app looks for RomM itself, and the
   * credential field takes RomM's pairing code as well as a pasted token.
   */
  import { toast } from 'svelte-sonner';
  import { romm } from '$lib/stores/romm.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Check from '@lucide/svelte/icons/check';
  import XIcon from '@lucide/svelte/icons/x';
  import Minus from '@lucide/svelte/icons/minus';
  import Save from '@lucide/svelte/icons/save';
  import RotateCw from '@lucide/svelte/icons/rotate-cw';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import Import from '@lucide/svelte/icons/import';

  let { onsaved } = $props();

  const s = $derived(romm.settings);

  // Local edit buffer so a half-typed URL never becomes live config.
  let url = $state('');
  let libraryRoot = $state('');
  // A pairing code from RomM, or a pasted `rmm_` token.
  let credential = $state('');
  let clearToken = $state(false);
  let repinEnabled = $state(true);
  let repinOnLoad = $state(true);
  let repinAbandonDays = $state(7);
  let swapKeepRomId = $state(false);
  let hasheousWriteIds = $state(false);
  // Why the last attempt to turn the switch on was refused, kept on screen
  // (a toast is gone before anyone has read two permission names).
  let swapRefusal = $state('');
  // What the last Save found wrong: `{ field, error, suggestion }`.
  let problem = $state(null);
  // RomM found at one of its usual addresses on first run.
  let found = $state(null);
  let busy = $state(false);
  let loaded = $state(false);

  $effect(() => {
    if (s && !loaded) {
      url = s.url ?? '';
      libraryRoot = s.library_root ?? '';
      repinEnabled = s.repin_enabled !== false;
      repinOnLoad = s.repin_on_load !== false;
      repinAbandonDays = s.repin_abandon_days ?? 7;
      swapKeepRomId = s.swap_keep_rom_id === true;
      hasheousWriteIds = s.hasheous_write_ids === true;
      loaded = true;
      if (!s.url) lookForRomm();
    }
  });

  async function lookForRomm() {
    try {
      const hit = await romm.discover();
      if (hit?.url) found = hit;
    } catch {
      // Nothing found is the same as nothing there: the fields stay empty.
    }
  }

  function useFound() {
    url = found.url;
    if (found.library_root && !libraryRoot.trim()) libraryRoot = found.library_root;
    found = null;
  }

  const test = $derived(romm.testResult);
  const envPinned = $derived(new Set(s?.env_defaults ?? []));

  // RomM's tokens are `rmm_` and 64 hex digits; anything short is a pairing
  // code (8 letters and digits, RomM shows it with a dash).
  function isPairCode(value) {
    const v = value.trim();
    return !v.startsWith('rmm_') && /^[A-Za-z0-9-]{4,16}$/.test(v);
  }

  function patch() {
    const out = {
      url: url.trim(),
      library_root: libraryRoot.trim(),
      repin_enabled: repinEnabled,
      repin_on_load: repinOnLoad,
      repin_abandon_days: Number(repinAbandonDays) || undefined,
      hasheous_write_ids: hasheousWriteIds,
    };
    const typed = credential.trim();
    if (clearToken) out.clear_token = true;
    else if (typed && !isPairCode(typed)) out.token = typed;
    return out;
  }

  // The connection works when nothing that was checked failed.
  const works = (r) => r && !r.error && r.reachable
    && r.authorized !== false && r.library_root_mounted !== false;

  /**
   * Pair if a code was typed, test, then save. `force` saves without the
   * test passing ("Save anyway"), for a RomM that is down right now.
   */
  async function handleSave(force = false) {
    problem = null;
    busy = true;
    try {
      if (isPairCode(credential) && !clearToken) {
        let paired;
        try {
          paired = await romm.pair(url.trim(), credential.trim());
        } catch (e) {
          problem = { field: 'token', error: e?.message ?? 'Pairing with RomM failed' };
          return;
        }
        // Single use: RomM has already spent it.
        credential = '';
        const missing = paired.missing_scopes ?? [];
        toast.success(missing.length
          ? `Paired with RomM. The token lacks ${missing.join(', ')}, so the features that need them stay off.`
          : 'Paired with RomM');
      }
      if (!force) {
        let result = await romm.testConnection(patch());
        // RomM's library found in the Volumes and the field still empty:
        // fill it in and check again.
        if (result?.library_root_suggestion && !libraryRoot.trim()) {
          libraryRoot = result.library_root_suggestion;
          result = await romm.testConnection(patch());
        }
        if (!works(result)) {
          problem = {
            field: result?.field ?? null,
            error: result?.error ?? 'The connection test failed',
            suggestion: result?.library_root_suggestion ?? null,
          };
          return;
        }
      }
      await save(patch());
    } catch (e) {
      problem = { field: null, error: e?.message ?? 'Failed to save settings' };
    } finally {
      busy = false;
    }
  }

  async function save(body) {
    await romm.saveSettings(body);
    // Reset the credential buffers only when the request actually carried
    // them, so a pairing code that failed stays there to fix.
    if ('token' in body || 'clear_token' in body) {
      credential = '';
      clearToken = false;
    }
    // "Keep the ROM id" (#390) is saved on its own, and only when it changed:
    // turning it on makes the backend check the token, and a refusal names
    // the missing permissions beside the switch without undoing the rest.
    if (swapKeepRomId !== (s?.swap_keep_rom_id === true)) await saveSwap();
    toast.success(test?.version ? `Connected to RomM ${test.version}` : 'RomM settings saved');
    onsaved?.();
  }

  function useSuggestion() {
    libraryRoot = problem.suggestion;
    handleSave();
  }

  const swap = $derived(romm.status?.swap);
  async function saveSwap() {
    swapRefusal = '';
    try {
      await romm.saveSettings({ swap_keep_rom_id: swapKeepRomId });
      await romm.loadStatus();
    } catch (e) {
      swapKeepRomId = s?.swap_keep_rom_id === true;
      swapRefusal = e?.message ?? 'Failed to save settings';
    }
  }

  // RomM's own converter (#385), when it is switched on. A warning and an
  // offer, never a block: nothing here stops either converter.
  const converter = $derived(romm.activeConverter);
  const rommTargets = $derived(
    Object.entries(converter?.platformFormats ?? {}).sort(([a], [b]) => a.localeCompare(b))
  );
  let importing = $state(false);

  async function handleImport() {
    importing = true;
    try {
      const { created, skipped } = await romm.importConverterTargets();
      const made = created === 1 ? '1 Rule' : `${created} Rules`;
      const left = skipped.length
        ? ` Skipped ${skipped.map((p) => `${p.name} (${p.target.toUpperCase()})`).join(', ')}: `
          + 'no mode here writes that format.'
        : '';
      if (created > 0) {
        toast.success(`Imported ${made}, off until you turn them on in Automation.${left}`);
      } else {
        toast.info(`No Rules to import: every Platform RomM converts already has one.${left}`);
      }
    } catch (e) {
      toast.error(e?.message ?? "Could not import RomM's targets");
    } finally {
      importing = false;
    }
  }
</script>

<div class="settings">
  {#if converter}
    <section class="card converter-banner" role="status" aria-live="polite">
      <header>
        <h2><TriangleAlert size={18} /> RomM's own converter is on</h2>
      </header>
      <p>
        RomM converts the Platforms listed in its config and then deletes the
        sources. It checks only that the conversion tool exited cleanly, and
        never reads the output back. Compressatorium checks every output before
        it deletes a source. Two converters on one library can convert the same
        game twice, so we suggest switching RomM's off
        (<code>ROM_CONVERTO_ENABLED=false</code>). Nothing here is blocked: the
        choice is yours.
      </p>
      {#if rommTargets.length}
        <p class="targets">
          RomM converts:
          {#each rommTargets as [slug, target], i (slug)}
            {#if i > 0}, {/if}<code>{slug}</code> → {target.toUpperCase()}
          {/each}
        </p>
      {/if}
      {#if converter.error}
        <p class="error">Could not read RomM's targets: {converter.error}</p>
      {/if}
      <div class="actions">
        <Button
          variant="secondary"
          onclick={handleImport}
          loading={importing}
          disabled={!rommTargets.length}
          icon={importIcon}
        >
          Import RomM's targets as Rules
        </Button>
        <span class="hint">Only for Platforms that have no Rule yet. New Rules start off.</span>
      </div>
    </section>
  {/if}

  {#if found && found.url !== url.trim()}
    <section class="card found" role="status" aria-live="polite">
      <p>
        Found RomM{found.version ? ` ${found.version}` : ''} at <code>{found.url}</code>{#if found.library_root},
          with its library at <code>{found.library_root}</code>{/if}.
      </p>
      <div class="actions">
        <Button variant="secondary" onclick={useFound}>Use it</Button>
      </div>
    </section>
  {/if}

  <form class="card" onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
    <header>
      <h2>Connection</h2>
      <p>
        Compressatorium reads RomM's catalog over the API, but the ROM files
        themselves through the filesystem — no disc image is ever copied over
        HTTP.
      </p>
    </header>

    <label class="field">
      <span class="label">
        RomM URL
        {#if envPinned.has('url')}<Badge tone="neutral" size="sm">from env</Badge>{/if}
      </span>
      <input
        type="url"
        bind:value={url}
        placeholder="http://romm:8080"
        autocomplete="off"
        spellcheck="false"
        aria-invalid={problem?.field === 'url'}
        aria-describedby={problem?.field === 'url' ? 'romm-problem' : undefined}
      />
      <span class="hint">The address you open RomM at.</span>
    </label>

    <label class="field">
      <span class="label">
        Pairing code or API token
        {#if s?.token_set}<Badge tone="success" size="sm">set</Badge>{/if}
      </span>
      <input
        type="password"
        bind:value={credential}
        placeholder={s?.token_set ? '•••••••• (leave blank to keep)' : 'ABCD-2345 or rmm_…'}
        autocomplete="new-password"
        spellcheck="false"
        disabled={clearToken}
        aria-invalid={problem?.field === 'token'}
        aria-describedby={problem?.field === 'token' ? 'romm-problem' : undefined}
      />
      <span class="hint">
        In RomM, open <strong>Client API Tokens</strong>, create a token with
        <code>platforms.read</code>, <code>roms.read</code>, <code>roms.write</code>
        and <code>tasks.run</code>, and press <strong>Pair</strong>. Type the code
        here and Save before it expires. Pairing gives that token a new secret.
        RomM before 4.8 has no Pair: paste the token instead.
      </span>
    </label>
    <!-- Outside the <label>: a label may own exactly one control. -->
    {#if s?.token_set}
      <Checkbox bind:checked={clearToken} label="Remove the saved token" />
    {/if}

    <label class="field">
      <span class="label">
        Library path (in this container)
        {#if envPinned.has('library_root')}<Badge tone="neutral" size="sm">from env</Badge>{/if}
      </span>
      <input
        type="text"
        bind:value={libraryRoot}
        placeholder="Found from RomM when left empty"
        autocomplete="off"
        spellcheck="false"
        aria-invalid={problem?.field === 'library_root'}
        aria-describedby={problem?.field === 'library_root' ? 'romm-problem' : undefined}
      />
      <span class="hint">
        Where RomM's library folder is mounted <em>here</em>, inside a
        Compressatorium volume. Leave it empty and Save finds it from RomM's
        platform folders.
      </span>
    </label>

    {#if busy || romm.testing}
      <div class="test"><Spinner /> Checking the connection…</div>
    {:else if test}
      <ul class="test-results">
        {@render row(test.reachable, 'RomM reachable', test.version ? `(v${test.version})` : '')}
        {@render row(
          test.authorized, 'Token accepted',
          test.platform_count != null ? `(${test.platform_count} platforms)` : '',
        )}
        {@render row(test.library_root_mounted, 'Library folder mounted here', '')}
      </ul>
    {/if}
    {#if problem}
      <p class="error" id="romm-problem" role="alert">{problem.error}</p>
      <div class="actions">
        {#if problem.suggestion && problem.suggestion !== libraryRoot.trim()}
          <Button variant="secondary" onclick={useSuggestion}>Use {problem.suggestion}</Button>
        {/if}
        <Button variant="secondary" onclick={() => handleSave()} icon={retryIcon}>Retry</Button>
        <Button variant="ghost" onclick={() => handleSave(true)}>Save anyway</Button>
      </div>
    {/if}

    <fieldset>
      <legend>Metadata</legend>
      <p class="hint">
        RomM identifies a CHD by the hash inside its header and an archive by its
        largest member, so CHD, ZIP and 7z keep their Redump/No-Intro match
        automatically. Other formats are matched on the file's own hash, which
        conversion changes — so their metadata is saved first and re-applied
        after RomM rescans, unless RomM re-identified the game by itself.
      </p>
      <Checkbox
        bind:checked={repinEnabled}
        label="Save metadata before converting to a format RomM can't hash"
      />
      <Checkbox
        bind:checked={hasheousWriteIds}
        disabled={!repinEnabled}
        label="Write Hasheous ids onto converted ROMs"
        description="When the app's Hasheous match knows a game RomM couldn't fully identify, write its provider ids (IGDB, TheGamesDB and others) onto the ROM in RomM after converting it, so RomM shows the cover and description. Only ids the ROM doesn't already have. Needs the snapshot above, which carries them."
      />
    </fieldset>

    <fieldset>
      <legend>Keep the ROM id</legend>
      <Checkbox
        bind:checked={swapKeepRomId}
        label="Keep the ROM id when converting"
        description="When a conversion replaces a file, rename RomM's record to the new file instead of letting RomM add a new game, so its saves, states, notes and collections stay put. For a single-file game converted to one file beside it, with the source deleted after its check. If RomM refuses the rename, the source is kept and the job says why. Needs a token with roms.read, roms.write and tasks.run, which RomM gives only to an admin's token: Save checks it with RomM first."
      />
      {#if swapRefusal}
        <p class="error" role="alert">{swapRefusal}</p>
      {:else if swap?.keepRomId && swap.error}
        <p class="error" role="alert">{swap.error}</p>
      {/if}
    </fieldset>

    <details class="advanced">
      <summary>Advanced</summary>
      <Checkbox
        bind:checked={repinOnLoad}
        label="Re-apply saved metadata when the RomM page opens"
      />
      <label class="field">
        <span>Give up on a saved snapshot after</span>
        <div class="days">
          <input type="number" min="1" max="365" bind:value={repinAbandonDays} />
          <span class="unit">days</span>
        </div>
        <span class="hint">
          A snapshot waits for RomM to rescan the converted file. Raise this if
          your queue or RomM's scan schedule runs longer.
        </span>
      </label>
    </details>

    <!-- Until the saved settings have loaded the buffers are empty, and
         submitting them would blank the URL and library root -- which the
         backend reads as a move to a different instance. -->
    <div class="actions">
      <Button
        type="submit"
        loading={busy || romm.settingsSaving}
        disabled={!loaded}
        icon={saveIcon}
      >
        Save
      </Button>
    </div>
  </form>
</div>

<!-- One check: passed, failed, or not checked (null) because an earlier one
     failed. Only what was checked is marked failed. -->
{#snippet row(ok, label, note)}
  <li class:ok={ok === true} class:skipped={ok == null}>
    {#if ok === true}<Check size={14} />{:else if ok === false}<XIcon size={14} />{:else}<Minus size={14} />{/if}
    {label}
    {#if ok == null}<span class="muted">(not checked)</span>{:else if note}<span class="muted">{note}</span>{/if}
  </li>
{/snippet}
{#snippet saveIcon()}<Save size={14} />{/snippet}
{#snippet retryIcon()}<RotateCw size={14} />{/snippet}
{#snippet importIcon()}<Import size={14} />{/snippet}

<style>
  .days {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .days input {
    width: 6rem;
  }

  .unit {
    color: var(--text-muted, #6b7280);
    font-size: 0.9rem;
  }

  .settings {
    display: grid;
    gap: var(--space-4);
    max-width: 720px;
  }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }
  header h2 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
  }
  header p {
    margin: var(--space-1) 0 0;
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .field { display: grid; gap: var(--space-1); }
  .label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: var(--weight-medium);
  }
  input {
    width: 100%;
    padding: var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-1);
    font: inherit;
  }
  input:disabled { opacity: 0.5; }
  .hint { color: var(--text-2); font-size: var(--text-sm); }
  .hint code {
    background: var(--surface-2);
    padding: 0 4px;
    border-radius: var(--radius-sm);
  }
  .actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
  .test { display: flex; align-items: center; gap: var(--space-2); }
  .test-results {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-1);
  }
  .test-results li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-2);
  }
  .test-results li.ok { color: var(--text-1); }
  .muted { color: var(--text-2); }
  .error { color: var(--danger-text, var(--text-1)); margin: 0; }
  fieldset {
    border: 0;
    border-top: 1px solid var(--border-subtle);
    margin: 0;
    padding: var(--space-3) 0 0;
    display: grid;
    gap: var(--space-3);
    min-width: 0;
  }
  legend {
    padding: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
  }
  fieldset > legend + * { margin-top: var(--space-1); }
  .advanced {
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-3);
  }
  /* A <details> lays its content out as a block, so space it by hand. */
  .advanced > :global(* + *) { margin-top: var(--space-3); }
  .advanced summary {
    cursor: pointer;
    font-weight: var(--weight-medium);
    color: var(--text-2);
  }
  input[aria-invalid='true'] { border-color: var(--error); }
  .test-results li.skipped { color: var(--text-3, var(--text-2)); }
  .found p { margin: 0; overflow-wrap: anywhere; }
  .found code, .hint code { overflow-wrap: anywhere; }
  .converter-banner {
    background: var(--warning-muted);
    border-color: var(--warning);
  }
  .converter-banner h2 {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .converter-banner :global(svg) { color: var(--warning-text); }
  .converter-banner p { margin: 0; }
  .converter-banner code {
    background: var(--surface-2);
    padding: 0 4px;
    border-radius: var(--radius-sm);
  }
  .converter-banner .actions { align-items: center; }
</style>
