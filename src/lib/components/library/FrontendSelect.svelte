<script>
  /**
   * The game list app a Library feeds (#386, #387): when a conversion
   * replaces a file, its entry in the app's gamelist.xml is pointed at the new
   * file first, so artwork, favourites and play counts stay.
   *
   * Stored as the gamelist switch and the ES-DE gamelists folder: ES-DE is
   * the switch with a folder (it reads only its own gamelists), Batocera and
   * Knulli the switch without one (theirs sit in each console folder). The
   * folder is picked from the Drives, never typed.
   *
   * @typedef {Object} Props
   * @property {object} source the Library's rules source
   */
  import { toast } from 'svelte-sonner';
  import FolderPickerModal from '$lib/components/modals/FolderPickerModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Select from '$lib/components/ui/Select.svelte';

  /** @type {Props} */
  let { source } = $props();

  const APPS = [
    { value: 'none', label: 'None' },
    { value: 'esde', label: 'ES-DE' },
    { value: 'batocera', label: 'Batocera or Knulli' },
  ];

  const app = $derived(
    !source.esFrontend ? 'none' : source.gamelistsFolder ? 'esde' : 'batocera',
  );
  let picking = $state(false);
  let pickError = $state('');
  // Bumped when a choice doesn't stick, so the select shows the saved one again.
  let reset = $state(0);

  async function choose(value) {
    try {
      if (value === 'esde') {
        pickError = '';
        picking = true;
        return;
      }
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

  async function useFolder(path) {
    pickError = '';
    try {
      await source.setGamelistsFolder(path);
      await source.setEsFrontend(true);
      picking = false;
      toast.success("Conversions now update ES-DE's game lists");
    } catch (e) {
      pickError = e?.message ?? "Couldn't use that folder";
    }
  }

  function cancel() {
    picking = false;
    reset += 1;
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
  {#if app === 'esde'}
    <p class="folder">
      <span class="muted">ES-DE's game lists:</span>
      <code>{source.gamelistsFolder}</code>
      <Button size="sm" variant="ghost" onclick={() => (picking = true)}>Change</Button>
    </p>
  {/if}
</div>

<FolderPickerModal
  open={picking}
  start={source.gamelistsFolder}
  title="Pick ES-DE's gamelists folder"
  confirmLabel="Use this folder"
  onClose={cancel}
  onpick={useFolder}
  error={pickError}
/>

<style>
  .frontend { display: flex; flex-direction: column; gap: var(--space-2); max-width: 32rem; }
  .hint { margin: 0; color: var(--text-2); font-size: var(--text-sm); }
  .folder { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin: 0; }
  .folder code { overflow-wrap: anywhere; font-size: var(--text-xs); }
  .muted { color: var(--text-2); font-size: var(--text-sm); }
</style>
