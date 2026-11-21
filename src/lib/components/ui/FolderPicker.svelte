<script>
  /**
   * Pick a folder on a Drive by walking to it: the Drives, then the folders
   * inside the chosen one. Never a text box for a path, so the answer is
   * always a folder Compressatorium can reach. A Drive's own top folder can be
   * picked.
   *
   * @typedef {Object} Props
   * @property {string|null} [start] a folder to open on, when it is on a Drive
   * @property {(path: string) => void} onpick
   * @property {() => void} [onCancel]
   * @property {string} [confirmLabel]
   * @property {string[]} [disabledPaths] folders that can't be picked (already used)
   */
  import { onMount } from 'svelte';
  import { api } from '$lib/api/endpoints.js';
  import { crumbsFor, isWithin, parentPath, trimPath } from '$lib/util/folderPath.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Folder from '@lucide/svelte/icons/folder';
  import HardDrive from '@lucide/svelte/icons/hard-drive';
  import CornerLeftUp from '@lucide/svelte/icons/corner-left-up';

  /** @type {Props} */
  let {
    start = null, onpick, onCancel, confirmLabel = 'Use this folder', disabledPaths = [],
  } = $props();

  const SETUP_GUIDE = 'https://github.com/pacnpal/compressatorium/blob/latest/docs/DOCKER-COMPOSE.md';

  let drives = $state(null);   // null while loading
  let drive = $state(null);    // the Drive being walked, null on the Drive list
  let path = $state(null);     // the folder shown, null on the Drive list
  let folders = $state([]);
  let status = $state('loading');
  let error = $state('');
  // Only the latest listing writes: a slow folder never replaces a newer one.
  let request = 0;

  // The breadcrumb from the Drive's top folder down to `path`.
  const crumbs = $derived(drive && path ? crumbsFor(drive, path) : []);
  const blocked = $derived(!!path && disabledPaths.map(trimPath).includes(path));

  async function loadDrives() {
    status = 'loading';
    error = '';
    try {
      const list = await api.getVolumes();
      drives = Array.isArray(list) ? list.filter((v) => v?.path) : [];
      const home = start ? drives.find((v) => isWithin(start, v.path)) : null;
      if (home) await open(home, trimPath(start));
      else if (drives.length === 1) await open(drives[0], trimPath(drives[0].path));
      else status = 'ready';
    } catch (e) {
      drives = [];
      status = 'error';
      error = e?.message ?? '';
    }
  }

  async function open(to, folder) {
    const mine = ++request;
    drive = to;
    path = folder;
    status = 'loading';
    error = '';
    try {
      const data = await api.listFiles(folder, false);
      if (mine !== request) return;
      folders = (data?.entries ?? []).filter((e) => e?.type === 'directory');
      status = 'ready';
    } catch (e) {
      if (mine !== request) return;
      folders = [];
      status = 'error';
      error = e?.message ?? '';
    }
  }

  function toDrives() {
    request++;
    drive = null;
    path = null;
    folders = [];
    status = 'ready';
  }

  function up() {
    if (!drive || path === trimPath(drive.path)) toDrives();
    else open(drive, parentPath(path));
  }

  function retry() {
    if (drive && path) open(drive, path);
    else loadDrives();
  }

  onMount(loadDrives);
</script>

<div class="folder-picker">
  <nav class="crumbs" aria-label="Where you are">
    <button type="button" class="crumb" onclick={toDrives} disabled={!drive}>Drives</button>
    {#each crumbs as c (c.path)}
      <span class="sep" aria-hidden="true">›</span>
      <button type="button" class="crumb" onclick={() => open(drive, c.path)}>{c.label}</button>
    {/each}
  </nav>

  <div class="list">
    {#if status === 'loading'}
      <div class="state"><Spinner size="sm" label="Opening" /> Opening…</div>
    {:else if status === 'error'}
      <div class="state error">
        <span>Couldn't open this folder. {error}</span>
        <Button size="sm" variant="secondary" onclick={retry}>Try again</Button>
      </div>
    {:else if !drive}
      {#if drives?.length}
        {#each drives as d (d.path)}
          <button type="button" class="row" onclick={() => open(d, trimPath(d.path))}>
            <HardDrive size={16} aria-hidden="true" />
            <span class="name">{d.name || d.path}</span>
            <span class="path">{d.path}</span>
          </button>
        {/each}
      {:else}
        <div class="state">
          <span>
            No drives yet. Add your game folders to the container
            (see the <a href={SETUP_GUIDE} target="_blank" rel="noreferrer">setup guide</a>).
          </span>
        </div>
      {/if}
    {:else}
      <button type="button" class="row" onclick={up}>
        <CornerLeftUp size={16} aria-hidden="true" />
        <span class="name">..</span>
      </button>
      {#each folders as f (f.path)}
        <button type="button" class="row" onclick={() => open(drive, trimPath(f.path))}>
          <Folder size={16} aria-hidden="true" />
          <span class="name">{f.name}</span>
        </button>
      {:else}
        <div class="state">No folders in here.</div>
      {/each}
    {/if}
  </div>

  <div class="actions">
    {#if blocked}<span class="note">This folder is already used.</span>{/if}
    {#if onCancel}<Button variant="ghost" onclick={onCancel}>Cancel</Button>{/if}
    <Button
      variant="primary"
      disabled={!path || status !== 'ready' || blocked}
      onclick={() => onpick?.(path)}
    >{confirmLabel}</Button>
  </div>
</div>

<style>
  .folder-picker {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }
  .crumbs {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-sm);
    min-width: 0;
  }
  .crumb {
    background: none;
    border: none;
    padding: 2px 4px;
    border-radius: var(--radius-sm);
    color: var(--accent);
    cursor: pointer;
    font-size: inherit;
    overflow-wrap: anywhere;
    text-align: left;
  }
  .crumb:disabled { color: var(--text-2); cursor: default; }
  .crumb:last-child { color: var(--text-1); font-weight: var(--weight-medium); }
  .crumb:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .sep { color: var(--text-3); }
  .list {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    max-height: min(360px, 50dvh);
    overflow-y: auto;
    min-height: 120px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    background: none;
    border: none;
    border-bottom: 1px solid var(--border-subtle);
    color: var(--text-1);
    font-size: var(--text-sm);
    text-align: left;
    cursor: pointer;
    min-width: 0;
  }
  .row:last-child { border-bottom: none; }
  .row:hover { background: var(--surface-3); }
  .row:focus-visible { outline: none; box-shadow: inset var(--focus-ring); }
  .name { overflow-wrap: anywhere; }
  .path {
    margin-left: auto;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }
  .state {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    padding: var(--space-4);
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .state.error { color: var(--text-1); }
  .actions {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .note { color: var(--text-2); font-size: var(--text-sm); margin-right: auto; }
</style>
