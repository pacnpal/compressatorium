<script>
  /**
   * Set a folder's Platform (#304): "this folder is PSP".
   *
   * Opened from a Finding or from a folder's row menu in the file browser.
   * The folder must be inside a folder Library; the backend says so when it
   * is not. The mapping ranks below the Image's header and above the
   * folder's name, and the Library is listed again so its Findings follow.
   * No file is moved or renamed.
   */
  import { untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { findings } from '$lib/stores/findings.svelte.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import FolderPen from '@lucide/svelte/icons/folder-pen';

  const target = $derived(findings.mappingTarget);
  const open = $derived(!!target);

  let platform = $state('');
  let busy = $state(false);
  let loadError = $state(null);

  const options = $derived([
    { value: '', label: 'Choose a Platform…' },
    ...findings.platforms.map((p) => ({ value: p.slug, label: p.name })),
  ]);

  // A fresh choice each time the dialog opens on a folder. Untracked: the
  // Platform list loading must not reset what was picked.
  $effect(() => {
    if (!target) return;
    untrack(() => {
      platform = '';
      loadError = null;
      findings.loadPlatforms().catch((e) => {
        loadError = e?.message ?? 'Could not list the Platforms';
      });
    });
  });

  function close() {
    if (busy) return;
    findings.closeMapping();
  }

  async function save(slug) {
    if (!target) return;
    busy = true;
    try {
      const saved = await findings.setMapping(target.path, slug);
      toast.success(
        slug
          ? `${target.name} is now ${saved?.platform_name ?? slug}`
          : `${target.name} no longer has a Platform set`,
        // A walk was running: the Findings follow once it finishes.
        saved?.walk?.status && saved.walk.status !== 'ok'
          ? { description: saved.walk.message ?? 'Findings refresh after the next listing.' }
          : undefined,
      );
      findings.closeMapping();
    } catch (e) {
      toast.error(e?.message ?? 'Could not set the Platform');
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  {open}
  onClose={close}
  title="Set the folder's Platform"
  description={target
    ? `Every Image under ${target.name} counts as this Platform unless its header says otherwise. A header that disagrees shows up as a Finding.`
    : ''}
  size="sm"
>
  {#snippet titleIcon()}<FolderPen size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <code class="path">{target?.path}</code>
    {#if loadError}
      <p class="error" role="alert">{loadError}</p>
    {:else}
      <Select label="Platform" bind:value={platform} {options} />
    {/if}
  {/snippet}
  {#snippet footer()}
    <Button variant="ghost" onclick={() => save(null)} disabled={busy}>Clear mapping</Button>
    <Button variant="secondary" onclick={close} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={() => save(platform)} disabled={busy || !platform} loading={busy}>
      Save
    </Button>
  {/snippet}
</BaseModal>

<style>
  .path {
    display: block;
    margin-bottom: var(--space-3, 12px);
    font-size: var(--text-sm, 0.85rem);
    overflow-wrap: anywhere;
  }
  .error { color: var(--danger, #d33); margin: 0; }
</style>
