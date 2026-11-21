<script>
  /**
   * Sonarr, Radarr and ROMarr (#522): the webhook URL and header to copy,
   * path rewrites (how the *arr sees a folder -> how this app does), a
   * dry-run check of a path, and the paths received lately. A path that
   * lands nowhere comes with a suggested rewrite, one click to add.
   */
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Copy from '@lucide/svelte/icons/copy';
  import Plus from '@lucide/svelte/icons/plus';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import Search from '@lucide/svelte/icons/search';

  let setup = $state(null);
  let rewrites = $state([]);
  let from = $state('');
  let to = $state('');
  let saving = $state(false);
  let probe = $state('');
  let checking = $state(false);
  let result = $state(null);

  const url = `${window.location.origin}/api/hooks/arr`;

  // Moves when a save starts and when it lands: a refresh sent before either
  // is dropped, so it can't put the old rewrites back (CodeRabbit PR #526).
  let saves = 0;

  async function load() {
    const sent = saves;
    try {
      const fresh = await api.getArrHook();
      if (sent !== saves) return;
      setup = fresh;
      rewrites = setup.rewrites ?? [];
    } catch (e) {
      toast.error(e?.message ?? 'Could not read the webhook setup');
    }
  }

  $effect(() => {
    load();
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  });

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast.success('Copied');
    } catch {
      toast.error('Copy failed: select the text and copy it');
    }
  }

  async function save(next) {
    saving = true;
    saves += 1;
    try {
      rewrites = (await api.saveArrRewrites(next)).rewrites;
      saves += 1;
      return true;
    } catch (e) {
      toast.error(e?.message ?? 'Could not save the rewrites');
      return false;
    } finally {
      saving = false;
    }
  }

  async function add(event) {
    event.preventDefault();
    if (await save([...rewrites, { from: from.trim(), to: to.trim() }])) {
      from = '';
      to = '';
      if (probe.trim()) await check();
    }
  }

  async function adopt(suggestion) {
    if (await save([...rewrites, suggestion])) {
      toast.success(`Rewriting ${suggestion.from} to ${suggestion.to}`);
      if (probe.trim()) await check();
      await load();
    }
  }

  async function check(event) {
    event?.preventDefault();
    const path = probe.trim();
    if (!path) return;
    checking = true;
    try {
      result = await api.checkArrPath(path);
    } catch (e) {
      result = null;
      toast.error(e?.message ?? 'Could not check the path');
    } finally {
      checking = false;
    }
  }

  function when(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString();
  }
</script>

<section class="arr" aria-label="Sonarr, Radarr and ROMarr">
  <div class="text">
    <p class="muted">
      Add a <strong>Webhook</strong> connection in the app, turned on for imports. Each file it
      imports into a watch folder is taken in at once, with no wait; one it imports straight
      into a Library is converted under its console's settings. Scripts can POST
      <code>{'{"path": "…"}'}</code> to <code>/api/scan</code> the same way.
    </p>
  </div>

  <dl class="copy">
    <div>
      <dt>URL</dt>
      <dd><code>{url}</code></dd>
      <Button variant="ghost" size="sm" icon={copyIcon} ariaLabel="Copy the webhook URL"
        onclick={() => copy(url)}>Copy</Button>
    </div>
    {#if setup?.confirm_header}
      <div>
        <dt>Header</dt>
        <dd><code>{setup.confirm_header.name}: {setup.confirm_header.value}</code></dd>
        <Button variant="ghost" size="sm" icon={copyIcon} ariaLabel="Copy the header value"
          onclick={() => copy(setup.confirm_header.value)}>Copy value</Button>
      </div>
    {/if}
    {#if setup?.auth}
      <div>
        <dt>Login</dt>
        <dd>Username <code>{setup.username}</code>, password: this app's API token</dd>
      </div>
    {/if}
  </dl>

  <form class="row" onsubmit={check}>
    <label class="field grow">
      <span class="label">Test a path, as the *arr sees it</span>
      <input type="text" bind:value={probe} placeholder="/downloads/roms/ps2/Game.iso"
        autocomplete="off" spellcheck="false" />
    </label>
    <Button type="submit" variant="secondary" icon={searchIcon} loading={checking}
      disabled={!probe.trim()}>Test</Button>
  </form>
  {#if result}
    <div class="result" class:ok={!result.problem} role="status">
      {#if result.problem}
        <p>{result.problem}.</p>
      {:else}
        {#if result.inbox_id != null}
          <p>Works: it lands at <code>{result.local}</code>, in a watch folder, and would be taken in at once.</p>
        {:else}
          <p>Works: it lands at <code>{result.local}</code>, in a Library, and its console's settings convert it.</p>
        {/if}
      {/if}
      {#if result.suggestion}
        {@render suggest(result.suggestion)}
      {/if}
    </div>
  {/if}

  <div class="rewrites">
    <h3>Path rewrites</h3>
    <p class="muted small">
      When the *arr mounts a folder under another name (it sees <code>/downloads</code>, this
      app sees <code>/data/downloads</code>). The longest match wins; the result must still be
      on one of your Drives.
    </p>
    {#if rewrites.length}
      <ul>
        {#each rewrites as rule, i (rule.from)}
          <li>
            <code>{rule.from}</code><span aria-hidden="true">→</span><code>{rule.to}</code>
            <Button variant="ghost" size="sm" icon={trashIcon} loading={saving}
              ariaLabel={`Remove the rewrite of ${rule.from}`}
              onclick={() => save(rewrites.filter((_, j) => j !== i))}>Remove</Button>
          </li>
        {/each}
      </ul>
    {/if}
    <form class="row" onsubmit={add}>
      <label class="field grow">
        <span class="label">The *arr sees</span>
        <input type="text" bind:value={from} placeholder="/downloads" autocomplete="off"
          spellcheck="false" />
      </label>
      <label class="field grow">
        <span class="label">This app sees</span>
        <input type="text" bind:value={to} placeholder="/data/downloads" autocomplete="off"
          spellcheck="false" />
      </label>
      <Button type="submit" variant="secondary" icon={plusIcon} loading={saving}
        disabled={!from.trim() || !to.trim()}>Add</Button>
    </form>
  </div>

  {#if setup?.recent?.length}
    <div class="recent">
      <h3>Received lately</h3>
      <ul>
        {#each setup.recent.slice(0, 5) as item, i (i)}
          <li>
            <span class="muted small">{when(item.at)} · {item.source}</span>
            <code>{item.path}</code>
            <span class="small" class:bad={item.problem}>
              {item.problem ? item.problem : 'Taken in'}
            </span>
            {#if item.suggestion}{@render suggest(item.suggestion)}{/if}
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</section>

{#snippet suggest(s)}
  <p class="suggest">
    <span>Found on a Drive: rewrite <code>{s.from}</code> to <code>{s.to}</code></span>
    <Button variant="secondary" size="sm" icon={plusIcon} loading={saving}
      onclick={() => adopt(s)}>Add this rewrite</Button>
  </p>
{/snippet}
{#snippet copyIcon()}<Copy size={14} />{/snippet}
{#snippet plusIcon()}<Plus size={14} />{/snippet}
{#snippet trashIcon()}<Trash2 size={14} />{/snippet}
{#snippet searchIcon()}<Search size={14} />{/snippet}

<style>
  .arr {
    display: flex;
    flex-direction: column;
    gap: var(--space-3, 12px);
    min-width: 0;
  }
  h2 { margin: 0 0 4px; font-size: var(--text-md, 1rem); }
  h3 { margin: 0 0 4px; font-size: var(--text-sm, 0.85rem); }
  .text p, .rewrites p { margin: 0; }
  .rewrites, .recent { display: flex; flex-direction: column; gap: var(--space-2, 8px); }
  code { overflow-wrap: anywhere; }
  .copy { margin: 0; display: flex; flex-direction: column; gap: var(--space-1, 4px); }
  .copy div { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2, 8px); }
  .copy dt { color: var(--text-3); width: 56px; font-size: var(--text-sm); }
  .copy dd { margin: 0; flex: 1 1 200px; min-width: 0; font-size: var(--text-sm); }
  .row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--space-2, 8px); }
  .field { display: flex; flex-direction: column; gap: var(--space-1, 4px); min-width: 0; }
  .grow { flex: 1 1 200px; }
  .label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  .field input {
    background: var(--surface-1, var(--surface-2));
    border: 1px solid var(--border-subtle, var(--border));
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
  }
  .field input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .result {
    padding: var(--space-2, 8px) var(--space-3, 12px);
    border-left: 3px solid var(--warning, var(--danger, #d33));
    background: var(--surface-1, var(--surface-2));
    font-size: var(--text-sm);
  }
  .result.ok { border-left-color: var(--success, var(--accent)); }
  .result p { margin: 0; }
  .suggest { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2, 8px);
    margin: var(--space-1, 4px) 0 0; font-size: var(--text-sm); }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column;
    gap: var(--space-1, 4px); }
  li { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1, 4px)
    var(--space-2, 8px); min-width: 0; padding: var(--space-1, 4px) 0;
    border-top: 1px solid var(--border-subtle, var(--border)); }
  .muted { color: var(--text-3); }
  .small { font-size: var(--text-xs, 0.75rem); }
  .bad { color: var(--danger, #d33); }
</style>
