<script>
  /**
   * Set a folder's Platform (#304): "this folder is PSP".
   *
   * Opened from a Finding or from a folder's row menu in the file browser.
   * The folder must be inside a folder Library; the backend says so when it
   * is not. Walking up from an Image, the nearest folder with a mapping or
   * a Platform name decides (on the same folder the mapping wins), and the
   * Image's header beats both. The Library is listed again so its Findings
   * follow.
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
  // What the folder is set to now (null: nothing set), preselected.
  let current = $state(null);

  const options = $derived([
    { value: '', label: 'Choose a Platform…' },
    ...findings.platforms.map((p) => ({ value: p.slug, label: p.name })),
  ]);

  // A fresh choice each time the dialog opens on a folder. Untracked: the
  // Platform list loading must not reset what was picked.
  $effect(() => {
    if (!target) return;
    const path = target.path;
    untrack(() => {
      platform = '';
      current = null;
      loadError = null;
      findings.loadPlatforms().catch((e) => {
        loadError = e?.message ?? 'Could not list the Platforms';
      });
      findings.currentMapping(path).then((slug) => {
        if (findings.mappingTarget?.path !== path) return;
        current = slug;
        if (slug && !platform) platform = slug;
      }).catch(() => {});
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
    ? `Images under ${target.name} count as this Platform, unless a nearer folder is named after or set to another Platform, or the Image's own header says otherwise. A header that disagrees shows up as a Finding.`
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
    <Button variant="ghost" onclick={() => save(null)} disabled={busy || !current}>Clear mapping</Button>
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
