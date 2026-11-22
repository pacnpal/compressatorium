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
  // Which opening of the dialog an async update belongs to: a Platform-list
  // failure from an earlier opening must not show on this one.
  let opening = 0;

  // What the folder is set to now, read by the store for this opening
  // (`findings.loadCurrentMapping`): `loaded` with a slug or null (none set),
  // or `error`, which is never shown as "none set".
  const current = $derived(findings.mappingState === 'loaded' ? findings.mappingCurrent : null);

  const options = $derived([
    { value: '', label: 'Choose a console…' },
    ...findings.platforms.map((p) => ({ value: p.slug, label: p.name })),
  ]);

  // A fresh choice each time the dialog opens on a folder. Untracked: the
  // Platform list loading must not reset what was picked.
  $effect(() => {
    const mine = ++opening;
    if (!target) return;
    untrack(() => {
      platform = '';
      loadError = null;
      findings.loadPlatforms().catch((e) => {
        if (mine === opening) loadError = e?.message ?? "Couldn't list the consoles";
      });
    });
  });

  // Preselect what the folder is set to, once this opening's read answers.
  // Tracks only the read: choosing another Platform must not snap back.
  $effect(() => {
    const slug = current;
    untrack(() => {
      if (slug && !platform) platform = slug;
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
          : `${target.name} no longer has a console set`,
        // A walk was running: the Findings follow once it finishes.
        saved?.walk?.status && saved.walk.status !== 'ok'
          ? { description: saved.walk.message ?? 'Needs attention updates after the next listing.' }
          : undefined,
      );
      findings.closeMapping();
    } catch (e) {
      toast.error(e?.message ?? "Couldn't set the console");
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  {open}
  onClose={close}
  title="Set console"
  description={target
    ? `Games in ${target.name} count as this console, unless a folder inside it is named after or set to another console, or a game's own data says otherwise. A game whose data disagrees shows up in Needs attention.`
    : ''}
  size="sm"
>
  {#snippet titleIcon()}<FolderPen size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <code class="path">{target?.path}</code>
    {#if loadError}
      <p class="error" role="alert">{loadError}</p>
    {:else}
      <Select label="Console" bind:value={platform} {options} />
    {/if}
    {#if findings.mappingState === 'error'}
      <div class="read-error" role="alert">
        <p class="error">
          Couldn't read this folder's console: {findings.mappingError}
        </p>
        <Button variant="secondary" size="sm" onclick={() => findings.loadCurrentMapping()}>
          Retry
        </Button>
      </div>
    {/if}
  {/snippet}
  {#snippet footer()}
    <Button variant="ghost" onclick={() => save(null)} disabled={busy || !current}>Clear console</Button>
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
  .read-error {
    display: flex;
    align-items: center;
    gap: var(--space-2, 8px);
    margin-top: var(--space-3, 12px);
  }
</style>
