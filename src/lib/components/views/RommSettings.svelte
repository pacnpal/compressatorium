<script>
  /**
   * RomM connection settings, editable in-app.
   *
   * Environment variables remain first-run defaults, but nothing here requires
   * a redeploy to change. The connection test is deliberately granular —
   * reachable / authorised / mounted are three independent things to get right,
   * and "it doesn't work" is useless when it could be any of them.
   */
  import { toast } from 'svelte-sonner';
  import { romm } from '$lib/stores/romm.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Check from '@lucide/svelte/icons/check';
  import XIcon from '@lucide/svelte/icons/x';
  import Plug from '@lucide/svelte/icons/plug';
  import Save from '@lucide/svelte/icons/save';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import Import from '@lucide/svelte/icons/import';

  let { onsaved } = $props();

  const s = $derived(romm.settings);

  // Local edit buffer so a half-typed URL never becomes live config.
  let url = $state('');
  let libraryRoot = $state('');
  let token = $state('');
  let clearToken = $state(false);
  let repinEnabled = $state(true);
  let repinOnLoad = $state(true);
  let repinAbandonDays = $state(7);
  let swapKeepRomId = $state(false);
  // Why the last attempt to turn the switch on was refused, kept on screen
  // (a toast is gone before anyone has read two permission names).
  let swapRefusal = $state('');
  let loaded = $state(false);

  $effect(() => {
    if (s && !loaded) {
      url = s.url ?? '';
      libraryRoot = s.library_root ?? '';
      repinEnabled = s.repin_enabled !== false;
      repinOnLoad = s.repin_on_load !== false;
      repinAbandonDays = s.repin_abandon_days ?? 7;
      swapKeepRomId = s.swap_keep_rom_id === true;
      loaded = true;
    }
  });

  const test = $derived(romm.testResult);
  const envPinned = $derived(new Set(s?.env_defaults ?? []));

  function patch() {
    const out = {
      url: url.trim(),
      library_root: libraryRoot.trim(),
      ...metadataPatch(),
    };
    if (clearToken) out.clear_token = true;
    else if (token.trim()) out.token = token.trim();
    return out;
  }

  /**
   * The Metadata card's fields, and only those.
   *
   * Saving here must never submit the connection buffers. They hold whatever
   * is half-typed in the other card, and a URL or library root that differs
   * from the saved one is an *identity change* on the backend: it clears the
   * conversion history and retires every pending metadata snapshot. Toggling
   * "re-apply on load" is not a reason to lose those.
   */
  function metadataPatch() {
    return {
      repin_enabled: repinEnabled,
      repin_on_load: repinOnLoad,
      repin_abandon_days: Number(repinAbandonDays) || undefined,
    };
  }

  async function save(body, message) {
    try {
      await romm.saveSettings(body);
      // Reset the credential buffers only when the request actually carried
      // them. They belong to the Connection card, and the Metadata card's Save
      // deliberately submits neither -- so clearing them here discarded a
      // replacement token the operator had typed and not yet saved. RomM shows
      // an API token once, at creation, so that is frequently a token nobody
      // can get back: the operator has to generate another one and update
      // anything else using it. The body is the authority on what was sent,
      // rather than a flag each call site has to remember to pass.
      if ('token' in body || 'clear_token' in body) {
        token = '';
        clearToken = false;
      }
      toast.success(message);
      onsaved?.();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save settings');
    }
  }

  const handleSave = () => save(patch(), 'RomM settings saved');
  const handleSaveMetadata = () =>
    save(metadataPatch(), 'Metadata settings saved');

  // "Keep the ROM id when converting" (#390). Turning it on makes the backend
  // check the token; a refusal names the missing permissions.
  const swap = $derived(romm.status?.swap);
  async function handleSaveSwap() {
    swapRefusal = '';
    try {
      await romm.saveSettings({ swap_keep_rom_id: swapKeepRomId });
      toast.success(swapKeepRomId ? 'Keeping ROM ids is on' : 'Keeping ROM ids is off');
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

  async function handleTest() {
    try {
      const result = await romm.testConnection(patch());
      if (result?.authorized && result?.library_root_mounted) {
        toast.success(`Connected to RomM ${result.version ?? ''}`.trim());
      } else if (result?.error) {
        toast.error(result.error);
      }
    } catch (e) {
      toast.error(e?.message ?? 'Connection test failed');
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

  <section class="card">
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
      />
      <span class="hint">The base URL of your RomM instance.</span>
    </label>

    <label class="field">
      <span class="label">
        API token
        {#if s?.token_set}<Badge tone="success" size="sm">set</Badge>{/if}
      </span>
      <input
        type="password"
        bind:value={token}
        placeholder={s?.token_set ? '•••••••• (leave blank to keep)' : 'rmm_…'}
        autocomplete="new-password"
        spellcheck="false"
        disabled={clearToken}
      />
      <span class="hint">
        In RomM: <strong>Administration → Client API Tokens</strong>. Needs
        <code>platforms.read</code> and <code>roms.read</code>; add
        <code>roms.write</code> to let Compressatorium restore metadata after
        converting. The token is never shown again once saved.
      </span>
    </label>
    <!-- Outside the <label>: a label may own exactly one control, and with the
         checkbox inside it a click on the checkbox row could focus the password
         input instead, while assistive tech read one ambiguous name for both. -->
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
        placeholder="/data/library"
        autocomplete="off"
        spellcheck="false"
      />
      <span class="hint">
        Where RomM's library folder is mounted <em>here</em> — not the path RomM
        sees. It must also be inside a configured Compressatorium volume.
      </span>
    </label>

    <div class="actions">
      <Button variant="secondary" onclick={handleTest} loading={romm.testing} icon={plugIcon}>
        Test connection
      </Button>
      <!-- Until the saved settings have loaded the buffers are empty, and
           submitting them would blank the URL and library root -- which the
           backend reads as a move to a different instance. -->
      <Button
        onclick={handleSave}
        loading={romm.settingsSaving}
        disabled={!loaded}
        icon={saveIcon}
      >
        Save
      </Button>
    </div>

    {#if romm.testing}
      <div class="test"><Spinner /> Testing…</div>
    {:else if test}
      <ul class="test-results">
        <li class:ok={test.reachable}>
          {#if test.reachable}<Check size={14} />{:else}<XIcon size={14} />{/if}
          RomM reachable
          {#if test.version}<span class="muted">(v{test.version})</span>{/if}
        </li>
        <li class:ok={test.authorized}>
          {#if test.authorized}<Check size={14} />{:else}<XIcon size={14} />{/if}
          Token accepted
          {#if test.platform_count !== null && test.platform_count !== undefined}
            <span class="muted">({test.platform_count} platforms)</span>
          {/if}
        </li>
        <li class:ok={test.library_root_mounted}>
          {#if test.library_root_mounted}<Check size={14} />{:else}<XIcon size={14} />{/if}
          Library folder mounted here
        </li>
      </ul>
      {#if test.error}<p class="error">{test.error}</p>{/if}
    {/if}
  </section>

  <section class="card">
    <header>
      <h2>Metadata</h2>
      <p>
        RomM identifies a CHD by the hash inside its header and an archive by its
        largest member, so CHD, ZIP and 7z keep their Redump/No-Intro match
        automatically. Other formats are matched on the file's own hash, which
        conversion changes — so their metadata is saved first. Where RomM
        re-identifies the format on its own, the saved metadata is confirmed
        against RomM's record and only re-applied if RomM didn't match it; the
        rest are re-applied after RomM rescans.
      </p>
    </header>
    <Checkbox
      bind:checked={repinEnabled}
      label="Save metadata before converting to a format RomM can't hash"
    />
    <Checkbox
      bind:checked={repinOnLoad}
      label="Re-apply metadata automatically when this page loads"
    />
    <label class="field">
      <span>Give up on a saved snapshot after</span>
      <div class="days">
        <input type="number" min="1" max="365" bind:value={repinAbandonDays} />
        <span class="unit">days</span>
      </div>
      <span class="hint">
        A snapshot waits for RomM to rescan the converted file. Raise this if
        your conversion queue or your RomM scan schedule runs longer than that
        — retiring a snapshot early means that ROM's metadata is gone for good.
      </span>
    </label>
    <div class="actions">
      <Button
        onclick={handleSaveMetadata}
        loading={romm.settingsSaving}
        disabled={!loaded}
        icon={saveIcon}
      >
        Save
      </Button>
    </div>
  </section>

  <section class="card">
    <header>
      <h2>Keep the ROM id</h2>
      <p>
        When a conversion replaces a file, rename RomM's record to the new file
        instead of letting RomM add a new game, so the saves, states, notes and
        collections on it stay put. Off by default. It applies when the source
        is deleted after its check, to a single-file game converted to one file
        beside it; anything else converts as before. If RomM refuses the rename,
        the source is kept and the job says why.
      </p>
    </header>
    <Checkbox
      bind:checked={swapKeepRomId}
      label="Keep the ROM id when converting"
    />
    <span class="hint">
      Needs a token with <code>roms.read</code>, <code>roms.write</code> and
      <code>tasks.run</code>. RomM only gives <code>roms.write</code> and
      <code>tasks.run</code> to a token created by an <strong>admin user</strong>
      (or a user in a group that can write ROMs and run tasks), so create the
      token under <strong>Administration → Client API Tokens</strong> as one.
      Turning this on checks the token with RomM first.
    </span>
    {#if swapRefusal}
      <p class="error" role="alert">{swapRefusal}</p>
    {:else if swap?.keepRomId && swap.error}
      <p class="error" role="alert">{swap.error}</p>
    {/if}
    <div class="actions">
      <Button
        onclick={handleSaveSwap}
        loading={romm.settingsSaving}
        disabled={!loaded}
        icon={saveIcon}
      >
        Save
      </Button>
    </div>
  </section>
</div>

{#snippet plugIcon()}<Plug size={14} />{/snippet}
{#snippet saveIcon()}<Save size={14} />{/snippet}
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
  .converter-banner {
    background: var(--warning-muted);
    border-color: var(--warning);
  }
  .converter-banner h2 {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .converter-banner :global(svg) { color: var(--warning); }
  .converter-banner p { margin: 0; }
  .converter-banner code {
    background: var(--surface-2);
    padding: 0 4px;
    border-radius: var(--radius-sm);
  }
  .converter-banner .actions { align-items: center; }
</style>
