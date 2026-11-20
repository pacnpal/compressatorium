<script>
  /**
   * Add a Library: pick its folder on a Drive, then open its Automation tab.
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

  async function pick(path) {
    error = '';
    try {
      const library = await libraries.register(path);
      onClose();
      if (library?.id) ui.navigate('libraries', { id: library.id, tab: 'automation' });
    } catch (e) {
      error = e?.status === 409 ? 'This folder is already a Library.' : (e?.message ?? "Couldn't add it.");
    }
  }

  function close() {
    error = '';
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
/>
