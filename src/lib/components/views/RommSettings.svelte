<script>
  /**
   * RomM's Connection tab: where RomM is, its token, where its games are
   * here, and (under More) what happens to RomM's details when a game is
   * converted. One Save sends the whole tab, then tests the connection.
   *
   * The address and games folder are sent only when they changed: the backend
   * reads a different one as a move to another RomM, which clears the
   * conversion history and retires every pending metadata snapshot. Keeping
   * the same RomM entry is saved on its own after the rest, because RomM
   * checks the token for it and a refusal must not undo the other fields.
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

  let { onsaved } = $props();

  const s = $derived(romm.settings);

  // Local edit buffer so a half-typed address never becomes live config.
  let url = $state('');
  let libraryRoot = $state('');
  let token = $state('');
  let clearToken = $state(false);
  let repinEnabled = $state(true);
  let repinOnLoad = $state(true);
  let repinAbandonDays = $state(7);
  let swapKeepRomId = $state(false);
  let hasheousWriteIds = $state(false);
  // Why turning "keep the same entry" on was refused, kept on screen.
  let swapRefusal = $state('');
  let loaded = $state(false);
  let saving = $state(false);

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
    }
  });

  const test = $derived(romm.testResult);
  const envPinned = $derived(new Set(s?.env_defaults ?? []));
  const swap = $derived(romm.status?.swap);

  /** The connection fields, the address and games folder only when changed. */
  function connectionPatch() {
    const out = {};
    if (url.trim() !== (s?.url ?? '')) out.url = url.trim();
    if (libraryRoot.trim() !== (s?.library_root ?? '')) out.library_root = libraryRoot.trim();
    if (clearToken) out.clear_token = true;
    else if (token.trim()) out.token = token.trim();
    return out;
  }

  /** What the test checks: the typed connection, whatever was saved. */
  function testPatch() {
    const out = { url: url.trim(), library_root: libraryRoot.trim() };
    if (clearToken) out.clear_token = true;
    else if (token.trim()) out.token = token.trim();
    return out;
  }

  function morePatch() {
    return {
      repin_enabled: repinEnabled,
      repin_on_load: repinOnLoad,
      repin_abandon_days: Number(repinAbandonDays) || undefined,
      hasheous_write_ids: hasheousWriteIds,
    };
  }

  async function handleSave() {
    saving = true;
    swapRefusal = '';
    try {
      const body = { ...connectionPatch(), ...morePatch() };
      await romm.saveSettings(body);
      // Reset the credential buffers only once they were sent, so a saved
      // token doesn't sit in the field to be sent again.
      if ('token' in body || 'clear_token' in body) {
        token = '';
        clearToken = false;
      }
      if (swapKeepRomId !== (s?.swap_keep_rom_id === true)) {
        try {
          await romm.saveSettings({ swap_keep_rom_id: swapKeepRomId });
        } catch (e) {
          swapKeepRomId = s?.swap_keep_rom_id === true;
          swapRefusal = e?.message ?? "Couldn't keep the same entry";
        }
      }
      onsaved?.();
    } catch (e) {
      toast.error(e?.message ?? "Couldn't save");
      return;
    } finally {
      saving = false;
    }
    try {
      const result = await romm.testConnection(testPatch());
      if (result?.authorized && result?.library_root_mounted) {
        toast.success(`Saved. Connected to RomM ${result.version ?? ''}`.trim());
      } else {
        toast.warning(`Saved, but RomM isn't answering yet: ${result?.error ?? 'see the checks below'}`);
      }
    } catch (e) {
      toast.warning(`Saved, but RomM isn't answering yet: ${e?.message ?? ''}`);
    }
    await romm.loadStatus();
  }

  async function handleTest() {
    try {
      await romm.testConnection(testPatch());
    } catch (e) {
      toast.error(e?.message ?? "Couldn't test the connection");
    }
  }
</script>

<div class="settings">
  <section class="card">
    <h2>Connection</h2>
    <label class="field">
      <span class="label">
        RomM address
        {#if envPinned.has('url')}<Badge tone="neutral" size="sm">from setup</Badge>{/if}
      </span>
      <input
        type="url" bind:value={url} placeholder="http://romm:8080"
        autocomplete="off" spellcheck="false"
      />
    </label>
    <label class="field">
      <span class="label">
        API token
        {#if s?.token_set}<Badge tone="success" size="sm">set</Badge>{/if}
      </span>
      <input
        type="password" bind:value={token}
        placeholder={s?.token_set ? '•••••••• (leave blank to keep)' : 'rmm_…'}
        autocomplete="new-password" spellcheck="false" disabled={clearToken}
      />
      <span class="hint">In RomM: Administration → Client API Tokens.</span>
    </label>
    <!-- Outside the <label>: a label owns one control. -->
    {#if s?.token_set}
      <Checkbox bind:checked={clearToken} label="Remove the saved token" />
    {/if}
    <label class="field">
      <span class="label">
        Where RomM's games are, as Compressatorium sees them
        {#if envPinned.has('library_root')}<Badge tone="neutral" size="sm">from setup</Badge>{/if}
      </span>
      <input
        type="text" bind:value={libraryRoot} placeholder="/data/library"
        autocomplete="off" spellcheck="false"
      />
    </label>
    <div class="actions">
      <Button variant="secondary" onclick={handleTest} loading={romm.testing} icon={plugIcon}>
        Test connection
      </Button>
    </div>
    {#if romm.testing}
      <div class="test"><Spinner /> Testing…</div>
    {:else if test}
      <ul class="test-results">
        <li class:ok={test.reachable}>
          {#if test.reachable}<Check size={14} />{:else}<XIcon size={14} />{/if}
          RomM answers
          {#if test.version}<span class="muted">(v{test.version})</span>{/if}
        </li>
        <li class:ok={test.authorized}>
          {#if test.authorized}<Check size={14} />{:else if test.reachable}<XIcon size={14} />{/if}
          The token works
          {#if !test.reachable}<span class="muted">not checked</span>{/if}
        </li>
        <li class:ok={test.library_root_mounted}>
          {#if test.library_root_mounted}<Check size={14} />{:else}<XIcon size={14} />{/if}
          The games folder is here
        </li>
      </ul>
      {#if test.error}<p class="error">{test.error}</p>{/if}
    {/if}
  </section>

  <details class="card more">
    <summary>More</summary>
    <Checkbox
      bind:checked={repinEnabled}
      label="Keep RomM's details (cover, notes, play history) when converting"
    />
    <Checkbox
      bind:checked={swapKeepRomId}
      label="Keep the same RomM entry when the file changes"
    />
    {#if swapRefusal}
      <p class="error" role="alert">{swapRefusal}</p>
    {:else if swap?.keepRomId && swap.missing?.length}
      <p class="error" role="alert">
        Your token can't rename RomM's entries. Make a token with {swap.missing.join(', ')}.
      </p>
    {/if}
    <Checkbox
      bind:checked={hasheousWriteIds}
      disabled={!repinEnabled}
      label="Add online IDs to converted games"
    />
    <Checkbox
      bind:checked={repinOnLoad}
      label="Put RomM's details back when this page opens"
    />
    <label class="field">
      <span>Stop waiting for RomM's scan after</span>
      <span class="days">
        <input type="number" min="1" max="365" bind:value={repinAbandonDays} />
        <span class="muted">days</span>
      </span>
    </label>
  </details>

  <div class="actions">
    <!-- Until the saved settings load the buffers are empty. -->
    <Button onclick={handleSave} loading={saving || romm.settingsSaving} disabled={!loaded}>
      Save
    </Button>
  </div>
</div>

{#snippet plugIcon()}<Plug size={14} />{/snippet}

<style>
  .settings { display: grid; gap: var(--space-4); max-width: 720px; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }
  .card h2 { margin: 0; font-size: var(--text-lg); font-weight: var(--weight-semibold); }
  .more summary { cursor: pointer; font-weight: var(--weight-semibold); }
  .more[open] summary { margin-bottom: var(--space-2); }
  .field { display: grid; gap: var(--space-1); }
  .label { display: flex; align-items: center; gap: var(--space-2); font-weight: var(--weight-medium); }
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
  .days { display: flex; align-items: center; gap: var(--space-2); }
  .days input { width: 6rem; }
  .hint { color: var(--text-2); font-size: var(--text-sm); }
  .actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
  .test { display: flex; align-items: center; gap: var(--space-2); }
  .test-results { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-1); }
  .test-results li { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); }
  .test-results li.ok { color: var(--text-1); }
  .muted { color: var(--text-2); }
  .error { color: var(--error); margin: 0; font-size: var(--text-sm); }
</style>
