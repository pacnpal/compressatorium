<script>
  // The Patch dialog (#428): apply an IPS or BPS patch to one cartridge Image.
  // The patch is a file in a Volume (a path) or an upload. The backend writes
  // "Name (Patched).7z" beside the Image and never changes the Image; the job
  // shows in the Jobs panel and its Ledger row names the patch file.
  import { ui } from '$lib/stores/ui.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { toast } from 'svelte-sonner';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Bandage from '@lucide/svelte/icons/bandage';
  import { isPatchFile, patchedName, patchRequest } from '$lib/util/patchDialog.js';

  const open = $derived(!!ui.patchTarget);
  const target = $derived(ui.patchTarget);
  const outputName = $derived(target?.path ? patchedName(target.path) : null);

  let source = $state('volume');
  let patchPath = $state('');
  let file = $state(null);
  let busy = $state(false);

  // The folder the Image is in, where a patch most often sits beside it.
  function folderOf(path) {
    const disk = path.split('::', 1)[0];
    return disk.slice(0, disk.lastIndexOf('/') + 1);
  }

  // A fresh form whenever the dialog opens on a new row.
  $effect(() => {
    if (target?.path) {
      source = 'volume';
      patchPath = folderOf(target.path);
      file = null;
    }
  });

  const ready = $derived(
    !busy && (source === 'upload' ? !!file && isPatchFile(file.name) : isPatchFile(patchPath.trim())),
  );

  function close() {
    if (busy) return;
    ui.patchTarget = null;
  }

  function pickFile(event) {
    file = event.currentTarget.files?.[0] ?? null;
  }

  async function handleSubmit() {
    if (!target?.path || !ready) return;
    busy = true;
    try {
      let uploadId;
      if (source === 'upload') {
        uploadId = (await api.uploadPatch(file)).upload_id;
      }
      await api.startPatch(patchRequest(target.path, { source, patchPath, uploadId }));
      toast.success(`Patching ${target.name}`, {
        description: outputName
          ? `Writing ${outputName} beside it`
          : 'Writing the patched ROM beside it',
      });
      ui.patchTarget = null;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to start the patch');
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  {open}
  onClose={close}
  title="Patch ROM"
  description={target ? `Apply an IPS or BPS patch to ${target.name}.` : ''}
  size="md"
>
  {#snippet titleIcon()}<Bandage size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <fieldset class="pt-source">
      <legend class="pt-text">Patch file</legend>
      <label class="pt-choice">
        <input type="radio" name="patch-source" value="volume" bind:group={source} />
        <span>In a Volume</span>
      </label>
      <label class="pt-choice">
        <input type="radio" name="patch-source" value="upload" bind:group={source} />
        <span>Upload from this device</span>
      </label>
    </fieldset>

    {#if source === 'volume'}
      <label class="pt-label">
        <span class="pt-text">Path to the .ips or .bps file</span>
        <input
          type="text"
          class="pt-input"
          bind:value={patchPath}
          autocomplete="off"
          spellcheck="false"
          aria-label="Path to the patch file"
        />
      </label>
    {:else}
      <label class="pt-label">
        <span class="pt-text">Patch to upload</span>
        <input
          type="file"
          class="pt-file"
          accept=".ips,.bps"
          onchange={pickFile}
          aria-label="Patch file to upload"
        />
      </label>
    {/if}

    <p class="pt-note">
      {#if outputName}
        Writes <strong>{outputName}</strong> beside it,
      {:else}
        Writes the ROM inside it as <strong>Name (Patched).7z</strong> beside it,
      {/if}
      with a number added if that name is taken. The original isn't changed.
    </p>
    <p class="pt-note">
      A BPS patch is checked against the ROM before it's applied and against its
      result after. An IPS patch carries no checksums, so you'll get a warning when
      the ROM isn't Recognized.
    </p>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={close} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={handleSubmit} disabled={!ready} loading={busy}>
      Patch
    </Button>
  {/snippet}
</BaseModal>

<style>
  .pt-source {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-4);
    border: none;
    margin: 0 0 var(--space-3);
    padding: 0;
  }
  .pt-source legend { margin-bottom: var(--space-2); }
  .pt-choice {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-1);
    font-size: var(--text-sm);
  }
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
  .pt-file { color: var(--text-1); font-size: var(--text-sm); }
  .pt-note {
    margin: var(--space-3) 0 0;
    color: var(--text-2);
    font-size: var(--text-sm);
    line-height: 1.5;
  }
  .pt-note strong { color: var(--text-1); font-weight: var(--weight-semibold); overflow-wrap: anywhere; }
</style>
