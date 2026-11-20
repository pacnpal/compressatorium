<script>
  /**
   * Add a Library: pick its folder on a Drive (and, if you like, name it),
   * then open its Automation tab.
   * The backend's own words show under the picker when it refuses (the folder
   * is already a Library, it isn't on a Drive).
   *
   * @typedef {Object} Props
   * @property {boolean} open
   * @property {() => void} onClose
   * @property {string|null} [start]
   */
  import { libraries } from '$lib/stores/libraries.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import FolderPickerModal from './FolderPickerModal.svelte';

  /** @type {Props} */
  let { open, onClose, start = null } = $props();

  let error = $state('');
  let name = $state('');

  async function pick(path) {
    error = '';
    try {
      const library = await libraries.register(path, name.trim() || null);
      name = '';
      onClose();
      if (library?.id) ui.navigate('libraries', { id: library.id, tab: 'automation' });
    } catch (e) {
      error = e?.status === 409 ? 'This folder is already a Library.' : (e?.message ?? "Couldn't add it.");
    }
  }

  function close() {
    error = '';
    name = '';
    onClose();
  }
</script>

<FolderPickerModal
  {open}
  {start}
  title="Add a Library"
  confirmLabel="Make this a Library"
  onClose={close}
  onpick={pick}
  {error}
>
  {#snippet extra()}
    <label class="name">
      <span>Name (optional)</span>
      <input type="text" bind:value={name} placeholder="The folder's name" />
    </label>
  {/snippet}
</FolderPickerModal>

<style>
  .name { display: flex; flex-direction: column; gap: var(--space-1); }
  .name span { font-size: var(--text-xs); color: var(--text-2); }
  .name input {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font: inherit;
    padding: var(--space-2) var(--space-3);
  }
</style>
