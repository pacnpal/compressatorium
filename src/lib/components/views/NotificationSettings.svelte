<script>
  /**
   * Notification destinations: a generic webhook, a Discord webhook, or an Apprise URL.
   * The kinds, their labels and placeholders come from the server (`kinds`).
   *
   * A saved URL is a secret (it often holds a token), so the server never sends
   * it back. The list shows a label and a harmless hint; "Edit" can change the
   * label, and typing a new URL replaces the stored one. Leaving the URL box
   * empty keeps it.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Select from '$lib/components/ui/Select.svelte';

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
    formOpen = true;
  }

  function openEdit(d) {
    editingId = d.id;
    kind = d.kind;
    url = '';
    label = d.label === d.hint ? '' : d.label;
    formOpen = true;
  }

  function closeForm() {
    formOpen = false;
    url = '';
  }

  async function save() {
    busy = true;
    try {
      if (editingId) {
        const body = { label };
        if (url.trim()) body.url = url.trim();
        await api.editNotificationDestination(editingId, body);
      } else {
        await api.addNotificationDestination({ kind, url: url.trim(), label });
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
    runTest('draft', () => api.testNotificationDraft({ kind, url: url.trim() }));
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
      Hear about finished sweeps and failed jobs on your phone or in your own tooling.
      A sweep sends one message with its counts and space saved, with its failures
      listed inside it. A job you start by hand sends one message only if it fails.
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
              {#if d.hint && d.hint !== d.label}<span class="muted">{d.hint}</span>{/if}
            </div>
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
        <div class="actions">
          <Button type="submit" disabled={busy || (!editingId && !url.trim())}>Save</Button>
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

    {#if events.length}
      <div class="events">
        <span class="label">Messages sent</span>
        <ul>
          {#each events as ev (ev.key)}<li>{ev.title}</li>{/each}
        </ul>
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
  .events ul { margin: var(--space-1) 0 0; padding-left: var(--space-4); color: var(--text-2); font-size: var(--text-sm); }
</style>
