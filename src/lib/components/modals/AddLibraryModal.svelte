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
  // Bumped when the dialog closes: an answer for an earlier opening keeps its
  // Library (registered all the same) but no longer closes or navigates.
  let session = 0;

  async function pick(path) {
    error = '';
    const mine = ++session;
    try {
      const library = await libraries.register(path);
      if (mine !== session) return;
      onClose();
      if (library?.id) ui.navigate('libraries', { id: library.id, tab: 'automation' });
    } catch (e) {
      if (mine !== session) return;
      error = e?.status === 409 ? 'This folder is already a Library.' : (e?.message ?? "Couldn't add it.");
    }
  }

  function close() {
    session++;
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
