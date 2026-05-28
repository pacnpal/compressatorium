<script>
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import ModeSelect from './ModeSelect.svelte';
  import CompressionPicker from './CompressionPicker.svelte';
  import DuplicateModal from '$lib/components/modals/DuplicateModal.svelte';
  import Settings2 from '@lucide/svelte/icons/settings-2';
  import Play from '@lucide/svelte/icons/play';
  import Loader from '@lucide/svelte/icons/loader-circle';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  const selectedFiles = $derived(Array.from(fileBrowser.selectedFiles.values()));
  const selectedPaths = $derived(selectedFiles.map((f) => f.path));
  const selectedCount = $derived(selectedPaths.length);
  const tool = $derived(conversion.currentTool);
  const supportsDeleteOnVerify = $derived(conversion.supportsDeleteOnVerify);
  const converting = $derived(conversion.converting);

  // Delete-on-verify preflight: each source must already be verified or
  // the backend rejects the submission. Surfacing the unverified count in
  // the UI prevents users from being surprised by a 400 at submit time.
  const unverifiedSources = $derived.by(() => {
    if (!conversion.deleteOnVerify) return [];
    if (!supportsDeleteOnVerify) return [];
    return selectedPaths.filter((p) => !verification.isVerified(p));
  });
  const blockedByDeleteOnVerify = $derived(unverifiedSources.length > 0);

  const submitDisabled = $derived(
    selectedCount === 0 || converting || blockedByDeleteOnVerify,
  );

  // Duplicate preflight: open the modal when the backend reports any
  // output collision; await the user's pick (skip / overwrite / null
  // for cancel) and only then submit. Skips the modal entirely when
  // there are no collisions.
  let duplicatePrompt = $state(null); // resolver fn while modal is open
  const duplicateModalOpen = $derived(!!duplicatePrompt);

  function resolveDuplicate(action) {
    if (duplicatePrompt) {
      const resolver = duplicatePrompt;
      duplicatePrompt = null;
      resolver(action);
    }
  }

  async function awaitDuplicateChoice() {
    return new Promise((resolve) => {
      duplicatePrompt = resolve;
    });
  }

  async function handleSubmit() {
    if (submitDisabled) return;
    try {
      const check = await conversion.checkDuplicates(selectedPaths);
      const conflicts = Array.isArray(check) ? check.filter((d) => d?.exists) : [];
      let duplicateAction = 'skip';
      if (conflicts.length > 0) {
        const choice = await awaitDuplicateChoice();
        if (!choice) return;   // user cancelled
        duplicateAction = choice;
      }
      await conversion.submit(selectedPaths, { duplicateAction });
      conversion.clearDuplicateCheck();
      fileBrowser.clearSelection();
    } catch (_e) {
      // toast already raised in conversion.submit / checkDuplicates
    }
  }

  function setOutputDir(value) {
    conversion.outputDir = value;
  }
</script>

<section class="panel" aria-label="Conversion configuration">
  <h2 class="panel-title">
    <Settings2 size={14} aria-hidden="true" />
    Convert with {tool?.label ?? '…'}
  </h2>

  {#if !tool}
    <EmptyState
      title="No tool selected"
      description="Pick a tool from the sidebar to configure conversions."
      glyph="◈"
    />
  {:else}
    <div class="fields">
      <ModeSelect />
      <CompressionPicker />

      <label class="field">
        <span class="label">Output directory</span>
        <input
          type="text"
          value={conversion.outputDir}
          placeholder="Leave blank to write next to source"
          oninput={(e) => setOutputDir(e.currentTarget.value)}
        />
        <span class="hint">
          Optional. Absolute paths or paths inside a mounted volume.
        </span>
      </label>

      {#if supportsDeleteOnVerify}
        <Checkbox
          bind:checked={conversion.deleteOnVerify}
          label="Delete sources after successful verification"
          description="Each source is verified before deletion. Skipped if verify fails."
        />
      {/if}
    </div>

    {#if blockedByDeleteOnVerify}
      <div class="warning" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          <strong>{unverifiedSources.length} unverified file{unverifiedSources.length === 1 ? '' : 's'} selected.</strong>
          Delete-on-verify requires every source to be verified first.
        </div>
      </div>
    {/if}

    <div class="actions">
      <span class="status" aria-live="polite">
        {#if selectedCount === 0}
          Select files to enable conversion.
        {:else}
          {selectedCount} file{selectedCount === 1 ? '' : 's'} ready.
        {/if}
      </span>
      <Button variant="primary" disabled={submitDisabled} onclick={handleSubmit}>
        {#snippet icon()}
          {#if converting}<Loader size={14} class="spin" />{:else}<Play size={14} />{/if}
        {/snippet}
        {converting ? 'Queuing…' : 'Start conversion'}
      </Button>
    </div>
  {/if}
</section>

<DuplicateModal open={duplicateModalOpen} onResolve={resolveDuplicate} />

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }
  .panel-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }
  .label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  .field input {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
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
  .hint {
    color: var(--text-3);
    font-size: var(--text-xs);
  }

  .warning {
    display: flex;
    gap: var(--space-2);
    background: var(--warning-muted);
    color: var(--warning);
    border-radius: var(--radius-md);
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
    align-items: flex-start;
  }
  .warning strong { font-weight: var(--weight-semibold); }

  .actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }
  .status {
    color: var(--text-2);
    font-size: var(--text-sm);
  }

  :global(.panel .spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
