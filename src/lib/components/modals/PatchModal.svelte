<script>
  // Patch bench (#428): an IPS or BPS file, from a Volume or uploaded, applied
  // to one cartridge Image. The job writes "Name (Patched).7z" beside it and
  // never changes the original; the backend refuses a disc Image.
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { toast } from 'svelte-sonner';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import FileDiff from '@lucide/svelte/icons/file-diff';

  const target = $derived(ui.patchTarget);

  let patchPath = $state('');
  /** @type {FileList | undefined} */
  let files = $state();
  let busy = $state(false);

  const upload = $derived(files?.[0] ?? null);
  const ready = $derived(!!upload || !!patchPath.trim());

  // Every way out clears the form, so the next Image starts empty.
  function dismiss() {
    patchPath = '';
    files = undefined;
    ui.patchTarget = null;
  }

  function close() {
    if (!busy) dismiss();
  }

  async function handleSubmit() {
    if (!target?.path || !ready) return;
    busy = true;
    try {
      const path = upload ? (await api.uploadPatch(upload)).path : patchPath.trim();
      await api.startPatch(target.path, path);
      const name = target.name;
      dismiss();
      toast.success(`Patching ${name}`, {
        description: 'The patched copy is written beside it as a .7z.',
      });
      jobs.refresh();
    } catch (e) {
      toast.error(e?.message ?? 'Could not start the patch');
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  open={!!target}
  onClose={close}
  title="Patch"
  description={target ? `Apply an IPS or BPS patch to ${target.name}.` : ''}
  size="sm"
>
  {#snippet titleIcon()}<FileDiff size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <div class="pt-body">
      <label class="pt-label">
        <span class="pt-text">Patch file on a Volume</span>
        <input
          type="text"
          class="pt-input"
          bind:value={patchPath}
          disabled={!!upload}
          placeholder="/roms/patches/Translation.bps"
          autocomplete="off"
          spellcheck="false"
        />
      </label>
      <label class="pt-label">
        <span class="pt-text">Or upload one</span>
        <input type="file" accept=".ips,.bps" bind:files />
      </label>
      <p class="pt-note">
        The original is never changed. A BPS patch for another ROM is refused; an IPS
        patch carries no checksum, so it only warns when the Image isn't Recognized.
      </p>
    </div>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={close} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={handleSubmit} disabled={busy || !ready} loading={busy}>
      Patch
    </Button>
  {/snippet}
</BaseModal>

<style>
  .pt-body { display: flex; flex-direction: column; gap: var(--space-3); }
  .pt-label { display: flex; flex-direction: column; gap: var(--space-1); }
  .pt-text {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  .pt-input {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
    font-family: var(--font-mono);
  }
  .pt-input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .pt-note { margin: 0; font-size: var(--text-xs); color: var(--text-2); }
</style>
