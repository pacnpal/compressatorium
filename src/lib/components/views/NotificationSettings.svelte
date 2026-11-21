<script>
  /**
   * Notification destinations: a generic webhook, a Discord webhook, or an Apprise URL.
   * The kinds, their labels and placeholders come from the server (`kinds`).
   *
   * A saved URL is a secret (it often holds a token), so the server never sends
   * it back. The list shows a label and a harmless hint; "Edit" can change the
   * label, and typing a new URL replaces the stored one. Leaving the URL box
   * empty keeps it.
   *
   * Each destination picks the messages it gets (#523); a generic webhook can
   * also carry a signing secret, which, like the URL, is never sent back.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';

  let destinations = $state([]);
  let events = $state([]);
  let kinds = $state([]);
  let loaded = $state(false);

  // The one open form: a new destination (editingId === null) or an edit.
  let formOpen = $state(false);
  let editingId = $state(null);
  let kind = $state('webhook');
  let url = $state('');
  let label = $state('');
  // The event keys the form's destination gets, and its signing secret.
  let chosen = $state([]);
  let secret = $state('');
  let stopSigning = $state(false);
  let editingSigned = $state(false);
  let busy = $state(false);
  // id (or 'draft') -> { ok, error } from the last Test.
  let results = $state({});

  const kindOptions = $derived(
    kinds.map((k) => ({
      value: k.value,
      label: k.available ? k.label : `${k.label} (not installed)`,
    })),
  );
  const selectedKind = $derived(kinds.find((k) => k.value === kind));
  const kindLabel = (value) => kinds.find((k) => k.value === value)?.label ?? value;
  const defaultEvents = () => events.filter((e) => e.default).map((e) => e.key);

  function toggleEvent(key, on) {
    chosen = on ? [...chosen, key] : chosen.filter((k) => k !== key);
  }

  async function load() {
    try {
      const r = await api.getNotifications();
      destinations = r.destinations ?? [];
      events = r.events ?? [];
      kinds = r.kinds ?? [];
    } catch (e) {
      toast.error(e?.message ?? 'Failed to load notifications');
    } finally {
      loaded = true;
    }
  }

  onMount(load);

  function openNew() {
    editingId = null;
    kind = 'webhook';
    url = '';
    label = '';
    chosen = defaultEvents();
    secret = '';
    stopSigning = false;
    editingSigned = false;
    formOpen = true;
  }

  function openEdit(d) {
    editingId = d.id;
    kind = d.kind;
    url = '';
    label = d.label === d.hint ? '' : d.label;
    chosen = [...(d.events ?? defaultEvents())];
    secret = '';
    stopSigning = false;
    editingSigned = d.signed === true;
    formOpen = true;
  }

  function closeForm() {
    formOpen = false;
    url = '';
    secret = '';
  }

  async function save() {
    busy = true;
    try {
      if (editingId) {
        const body = { label, events: chosen };
        if (url.trim()) body.url = url.trim();
        if (stopSigning) body.secret = '';
        else if (secret.trim()) body.secret = secret.trim();
        await api.editNotificationDestination(editingId, body);
      } else {
        const body = { kind, url: url.trim(), label, events: chosen };
        if (selectedKind?.signs && secret.trim()) body.secret = secret.trim();
        await api.addNotificationDestination(body);
      }
      toast.success('Destination saved');
      closeForm();
      await load();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save destination');
    } finally {
      busy = false;
    }
  }

  async function remove(d) {
    try {
      await api.deleteNotificationDestination(d.id);
      delete results[d.id];
      await load();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to remove destination');
    }
  }

  async function runTest(key, request) {
    busy = true;
    try {
      results[key] = await request();
    } catch (e) {
      results[key] = { ok: false, error: e?.message ?? 'Test failed' };
    } finally {
      busy = false;
    }
  }

  const testSaved = (d) => runTest(d.id, () => api.testNotificationDestination(d.id));
  const testDraft = () =>
    runTest('draft', () =>
      api.testNotificationDraft({
        kind,
        url: url.trim(),
        ...(selectedKind?.signs && secret.trim() ? { secret: secret.trim() } : {}),
      }),
    );
</script>

{#snippet outcome(r)}
  {#if r}
    <span class="outcome" class:ok={r.ok} role="status">
      {r.ok ? 'Test message sent' : r.error}
    </span>
  {/if}
{/snippet}

<section class="card" aria-labelledby="notif-title">
  <header>
    <h2 id="notif-title">Notifications</h2>
    <p>
      Hear about finished sweeps and jobs on your phone or in your own tooling. A
      sweep sends one message with its counts and space saved, with its failures
      listed inside it. Pick which messages each destination gets.
    </p>
  </header>

  {#if !loaded}
    <p class="muted">Loading…</p>
  {:else}
    {#if destinations.length === 0 && !formOpen}
      <p class="muted">No destinations yet, so nothing is being sent.</p>
    {/if}

    {#if destinations.length}
      <ul class="dests">
        {#each destinations as d (d.id)}
          <li>
            <div class="dest-main">
              <span class="dest-label">{d.label}</span>
              <Badge tone="neutral" size="sm">{kindLabel(d.kind)}</Badge>
              {#if d.signed}<Badge tone="neutral" size="sm">Signed</Badge>{/if}
              {#if d.hint && d.hint !== d.label}<span class="muted">{d.hint}</span>{/if}
            </div>
            <span class="muted">
              Gets {d.events?.length ?? 0} of {events.length} messages
            </span>
            <div class="dest-actions">
              <Button size="sm" variant="secondary" disabled={busy} onclick={() => testSaved(d)}>Test</Button>
              <Button size="sm" variant="ghost" onclick={() => openEdit(d)}>Edit</Button>
              <Button size="sm" variant="ghost" onclick={() => remove(d)}>Remove</Button>
            </div>
            {@render outcome(results[d.id])}
          </li>
        {/each}
      </ul>
    {/if}

    {#if formOpen}
      <form class="form" onsubmit={(e) => { e.preventDefault(); save(); }}>
        {#if !editingId}
          <div class="field">
            <span class="label">Type</span>
            <Select bind:value={kind} options={kindOptions} ariaLabel="Destination type" />
          </div>
        {/if}
        {#if selectedKind && !selectedKind.available}
          <p class="error">
            {selectedKind.label} is not installed in this build. Use a webhook, or run
            the Docker image, which includes it.
          </p>
        {/if}
        <label class="field">
          <span class="label">{editingId ? 'New URL (leave empty to keep the saved one)' : 'URL'}</span>
          <input
            type="url"
            bind:value={url}
            placeholder={selectedKind?.placeholder ?? ''}
            autocomplete="off"
            spellcheck="false"
          />
          <span class="hint">
            Saved URLs are never shown again; they can carry a token.
          </span>
        </label>
        <label class="field">
          <span class="label">Label (optional)</span>
          <input type="text" bind:value={label} maxlength="60" placeholder="My phone" />
        </label>
        {#if selectedKind?.signs}
          <label class="field">
            <span class="label">
              {editingSigned
                ? 'New signing secret (leave empty to keep the saved one)'
                : 'Signing secret (optional)'}
            </span>
            <input
              type="password"
              bind:value={secret}
              maxlength="256"
              autocomplete="new-password"
              disabled={stopSigning}
            />
            <span class="hint">
              Each message then carries <code>X-Compressatorium-Signature: sha256=…</code>,
              the HMAC-SHA256 of the body with this secret, so your endpoint can check it
              came from here.
            </span>
          </label>
          {#if editingSigned}
            <Checkbox bind:checked={stopSigning} label="Stop signing messages" />
          {/if}
        {/if}
        {#if events.length}
          <fieldset class="field events">
            <legend class="label">Messages to send</legend>
            {#each events as ev (ev.key)}
              <Checkbox
                checked={chosen.includes(ev.key)}
                label={ev.title}
                onchange={(on) => toggleEvent(ev.key, on)}
              />
            {/each}
          </fieldset>
        {/if}
        <div class="actions">
          <Button type="submit" disabled={busy || (!editingId && !url.trim()) || !chosen.length}>
            Save
          </Button>
          <Button variant="secondary" disabled={busy || !url.trim()} onclick={testDraft}>Test</Button>
          <Button variant="ghost" onclick={closeForm}>Cancel</Button>
          {@render outcome(results.draft)}
        </div>
      </form>
    {:else}
      <div class="actions">
        <Button variant="secondary" onclick={openNew}>Add destination</Button>
      </div>
    {/if}
  {/if}
</section>

<style>
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
    max-width: 720px;
  }
  header h2 { margin: 0; font-size: var(--text-lg); font-weight: var(--weight-semibold); }
  header p { margin: var(--space-1) 0 0; color: var(--text-2); font-size: var(--text-sm); }
  .dests { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-2); }
  .dests li {
    display: grid;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    background: var(--surface-2);
    border-radius: var(--radius-md);
  }
  .dest-main { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; min-width: 0; }
  .dest-label { font-weight: var(--weight-medium); overflow-wrap: anywhere; }
  .dest-actions { display: flex; gap: var(--space-1); flex-wrap: wrap; }
  .form { display: grid; gap: var(--space-3); }
  .field { display: grid; gap: var(--space-1); }
  .label { font-weight: var(--weight-medium); }
  input {
    width: 100%;
    padding: var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-1);
    font: inherit;
  }
  .hint, .muted { color: var(--text-2); font-size: var(--text-sm); }
  .actions { display: flex; gap: var(--space-2); flex-wrap: wrap; align-items: center; }
  .outcome { color: var(--danger-text, var(--text-1)); font-size: var(--text-sm); overflow-wrap: anywhere; }
  .outcome.ok { color: var(--success, var(--text-1)); }
  .error { color: var(--danger-text, var(--text-1)); margin: 0; font-size: var(--text-sm); }
  .events { border: 0; margin: 0; padding: 0; gap: var(--space-2); }
  .events legend { padding: 0; margin-bottom: var(--space-1); }
</style>
