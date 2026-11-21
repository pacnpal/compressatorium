<script>
  /**
   * The game list app a Library feeds (#386, #387, #523): when a conversion
   * replaces a file, its entry in the app's gamelist.xml is pointed at the
   * new file first, so artwork, favourites and play counts stay.
   *
   * Stored as the gamelist switch and the ES-DE gamelists folder: ES-DE is
   * the switch with a folder (it reads only its own gamelists), Batocera and
   * Knulli the switch without one (theirs sit in each console folder).
   * ES-DE's folder is found in the Drives and offered with one click, or
   * picked from them; it is never typed.
   *
   * @typedef {Object} Props
   * @property {object} source the Library's rules source
   */
  import { onMount, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import FolderPickerModal from '$lib/components/modals/FolderPickerModal.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Select from '$lib/components/ui/Select.svelte';

  /** @type {Props} */
  let { source } = $props();

  const APPS = [
    { value: 'none', label: 'None' },
    { value: 'esde', label: 'ES-DE' },
    { value: 'batocera', label: 'Batocera or Knulli' },
  ];

  // ES-DE picked, its folder not chosen yet: nothing is on until it is.
  let pendingEsde = $state(false);
  const app = $derived(
    pendingEsde ? 'esde' : !source.esFrontend ? 'none' : source.gamelistsFolder ? 'esde' : 'batocera',
  );
  // ES-DE gamelists folders found in the Drives; null until looked for.
  let found = $state(null);
  let searching = $state(false);
  let saving = $state(false);
  let picking = $state(false);
  let pickError = $state('');
  // Bumped when a choice doesn't stick, so the select shows the saved one again.
  let reset = $state(0);
  // False once the tab is gone: a search answering later writes nothing.
  let alive = true;
  onMount(() => () => {
    alive = false;
  });

  async function findFolders() {
    searching = true;
    try {
      const answer = (await api.findGamelistsFolders()).folders ?? [];
      if (alive) found = answer;
    } catch (e) {
      if (!alive) return;
      found = [];
      toast.error(e?.message ?? "Couldn't look in the Drives");
    } finally {
      if (alive) searching = false;
    }
  }

  // A Library set to Batocera may really be ES-DE's, whose gamelists live
  // elsewhere: look once, and offer what is found.
  $effect(() => {
    if (source.esFrontend && !source.gamelistsFolder && found === null) untrack(findFolders);
  });

  async function choose(value) {
    try {
      if (value === 'esde') {
        if (source.gamelistsFolder) {
          pendingEsde = false;
          await source.setEsFrontend(true);
          return;
        }
        pendingEsde = true;
        if (found === null) findFolders();
        return;
      }
      pendingEsde = false;
      if (value === 'none') {
        await source.setEsFrontend(false);
        toast.success('Game lists are left alone');
        return;
      }
      if (source.gamelistsFolder) await source.setGamelistsFolder('');
      await source.setEsFrontend(true);
      toast.success('Conversions now update the game list in each console folder');
    } catch (e) {
      reset += 1;
      toast.error(e?.message ?? "Couldn't save");
    }
  }

  /** Save *path* as ES-DE's gamelists folder and turn the updates on. */
  async function useFolder(path) {
    saving = true;
    pickError = '';
    try {
      await source.setGamelistsFolder(path);
      await source.setEsFrontend(true);
      pendingEsde = false;
      picking = false;
      toast.success("Conversions now update ES-DE's game lists");
    } catch (e) {
      if (picking) pickError = e?.message ?? "Couldn't use that folder";
      else toast.error(e?.message ?? "Couldn't use that folder");
    } finally {
      saving = false;
    }
  }
</script>

<div class="frontend">
  {#key reset}
    <Select label="Game list app" value={app} options={APPS} onchange={choose} />
  {/key}
  <p class="hint">
    Keeps its game lists pointing at the new files.
    {#if app !== 'none'}Close ES-DE first: it rewrites its game lists when it quits.{/if}
  </p>

  {#if app === 'esde' || (app === 'batocera' && found?.length)}
    <div class="found" role="status">
      {#if app === 'batocera'}
        <span class="hint">
          ES-DE's game lists were found in your Drives. ES-DE reads only those, so if
          this Library is ES-DE's, use its folder:
        </span>
      {:else if searching}
        <span class="hint">Looking for ES-DE's game lists in your Drives…</span>
      {:else if found && !found.length && !source.gamelistsFolder}
        <span class="hint">No ES-DE game lists were found in your Drives.</span>
      {/if}
      {#each found ?? [] as folder (folder)}
        <span class="row">
          <code>{folder}</code>
          {#if folder === source.gamelistsFolder}
            <Badge tone="success" size="sm">In use</Badge>
          {:else}
            <Button size="sm" onclick={() => useFolder(folder)} loading={saving}>Use this folder</Button>
          {/if}
        </span>
      {/each}
      {#if app === 'esde'}
        <span class="row">
          {#if source.gamelistsFolder && !(found ?? []).includes(source.gamelistsFolder)}
            <code>{source.gamelistsFolder}</code>
            <Badge tone="success" size="sm">In use</Badge>
          {/if}
          <Button size="sm" variant="ghost" onclick={() => { pickError = ''; picking = true; }}>
            Pick another folder…
          </Button>
        </span>
        {#if pendingEsde && !searching}
          <span class="hint">Choose ES-DE's gamelists folder to turn this on.</span>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<FolderPickerModal
  open={picking}
  start={source.gamelistsFolder}
  title="Pick ES-DE's gamelists folder"
  confirmLabel="Use this folder"
  onClose={() => (picking = false)}
  onpick={useFolder}
  error={pickError}
/>

<style>
  .frontend { display: flex; flex-direction: column; gap: var(--space-2); max-width: 40rem; }
  .hint { margin: 0; color: var(--text-2); font-size: var(--text-sm); }
  .found { display: grid; gap: var(--space-2); }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); min-width: 0; }
  .row code { overflow-wrap: anywhere; min-width: 0; font-size: var(--text-xs); }
</style>
