<script>
  /**
   * The Findings view (#304): everything that needs a person's decision.
   *
   * Each folder Library's walk records a Platform conflict (the header and
   * the folder or mapping disagree) or an Unsorted Image (nobody could tell
   * its Platform). Each row links to what fixes it: open the folder in the
   * file browser, or set the folder's Platform. Dismiss hides a row until its
   * cause changes. The app never moves or deletes an Image over a Finding.
   */
  import { untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { findings } from '$lib/stores/findings.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import FolderOpen from '@lucide/svelte/icons/folder-open';
  import FolderPen from '@lucide/svelte/icons/folder-pen';
  import EyeOff from '@lucide/svelte/icons/eye-off';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';

  const KIND_LABEL = { platform_conflict: 'Platform conflict', unsorted: 'Unsorted' };

  // Lists on open, and again when "Show dismissed" flips.
  $effect(() => {
    void findings.showDismissed;
    untrack(() => findings.load());
  });

  function when(stamp) {
    if (!stamp) return '';
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? stamp : date.toLocaleString();
  }

  async function check() {
    try {
      const result = await findings.check();
      const waiting = (result?.libraries ?? []).filter((lib) => lib.status !== 'ok');
      if (waiting.length) {
        toast.warning(`${waiting.length} Library could not be checked right now`, {
          description: waiting.map((lib) => `${lib.id}: ${lib.status}`).join(', '),
        });
      }
    } catch (e) {
      toast.error(e?.message ?? 'Could not check the Libraries');
    }
  }

  async function dismiss(row) {
    try {
      await findings.dismiss(row.id);
      toast.success(`Dismissed until its cause changes: ${row.image_key}`);
    } catch (e) {
      toast.error(e?.message ?? 'Could not dismiss the Finding');
    }
  }

</script>

<div class="findings-view">
  <header class="head">
    <div class="title">
      <h1>Findings</h1>
      <p class="muted">
        Images that need your decision. Nothing here is moved or deleted by the app:
        open the folder to move a file yourself, or tell the app which Platform a folder holds.
      </p>
    </div>
    <div class="head-actions">
      <Checkbox
        bind:checked={findings.showDismissed}
        label={`Show dismissed (${findings.dismissed})`}
      />
      <Button variant="secondary" onclick={check} loading={findings.checking} icon={refreshIcon}>
        Check now
      </Button>
    </div>
  </header>

  {#if findings.error}
    <div class="notice error" role="alert">Could not list the Findings: {findings.error}</div>
  {/if}

  {#if findings.loading && findings.list.length === 0}
    <p class="muted"><Spinner /> Loading…</p>
  {:else if findings.list.length === 0 && !findings.error}
    <EmptyState
      title="No Findings"
      description="Every Image in your folder Libraries has a Platform. Findings appear after a Library is listed or swept, or when you press Check now."
    />
  {:else}
    <div class="table" role="table" aria-label="Findings">
      <div class="row header" role="row">
        <span role="columnheader">Library</span>
        <span role="columnheader">Image</span>
        <span role="columnheader">Kind</span>
        <span role="columnheader">First seen</span>
        <span role="columnheader">Last seen</span>
        <span role="columnheader" class="sr-only">Actions</span>
      </div>
      {#each findings.list as row (row.id)}
        <div class="row" class:dismissed={row.dismissed} role="row">
          <span role="cell" class="library">{row.library_name}</span>
          <span role="cell" class="image">
            <code>{row.image_key}</code>
            <span class="muted reason">{row.reason}</span>
          </span>
          <span role="cell">
            <Badge tone={row.kind === 'platform_conflict' ? 'warning' : 'neutral'}>
              {KIND_LABEL[row.kind] ?? row.kind}
            </Badge>
            {#if row.dismissed}<span class="muted small">dismissed</span>{/if}
          </span>
          <span role="cell" class="muted small"><span class="cell-label">First seen </span>{when(row.first_seen)}</span>
          <span role="cell" class="muted small"><span class="cell-label">Last seen </span>{when(row.last_seen)}</span>
          <span role="cell" class="actions">
            <Button variant="ghost" size="sm" icon={openIcon} onclick={() => fileBrowser.openFolder(row.folder)}>
              Open folder
            </Button>
            <Button variant="ghost" size="sm" icon={mapIcon} onclick={() => findings.openMapping(row.folder)}>
              Set Platform
            </Button>
            {#if !row.dismissed}
              <Button variant="ghost" size="sm" icon={dismissIcon} onclick={() => dismiss(row)}>
                Dismiss
              </Button>
            {/if}
          </span>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#snippet refreshIcon()}<RefreshCw size={14} />{/snippet}
{#snippet openIcon()}<FolderOpen size={14} />{/snippet}
{#snippet mapIcon()}<FolderPen size={14} />{/snippet}
{#snippet dismissIcon()}<EyeOff size={14} />{/snippet}

<style>
  .findings-view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4, 16px);
    padding: var(--space-6, 24px);
    max-width: 1200px;
    margin: 0 auto;
    width: 100%;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--space-4, 16px);
  }
  .title h1 { margin: 0 0 4px; font-size: var(--text-xl, 1.25rem); }
  .title p { margin: 0; max-width: 70ch; }
  .head-actions { display: flex; align-items: center; gap: var(--space-3, 12px); flex-wrap: wrap; }
  .muted { color: var(--text-3); }
  .small { font-size: var(--text-sm, 0.85rem); }
  .notice.error {
    padding: 8px 12px;
    border: 1px solid var(--danger, #d33);
    border-radius: var(--radius-sm);
    color: var(--danger, #d33);
  }
  .table {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--radius-md, 8px);
    background: var(--surface-1);
    overflow: hidden;
  }
  .row {
    display: grid;
    grid-template-columns: minmax(90px, 0.8fr) minmax(200px, 2.4fr) minmax(120px, 0.9fr) 1fr 1fr auto;
    gap: var(--space-3, 12px);
    align-items: center;
    padding: 10px 14px;
    border-top: 1px solid var(--border-subtle, var(--border));
  }
  .row.header {
    border-top: none;
    background: var(--surface-2);
    font-size: var(--text-xs, 0.75rem);
    font-weight: var(--weight-semibold);
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .row.dismissed { opacity: 0.6; }
  .image { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .image code { overflow-wrap: anywhere; font-size: var(--text-sm, 0.85rem); }
  .reason { font-size: var(--text-sm, 0.85rem); }
  .library { overflow-wrap: anywhere; }
  .actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; }
  .cell-label { display: none; }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  @media (max-width: 900px) {
    .findings-view { padding: var(--space-4, 16px); }
    .row { grid-template-columns: 1fr; gap: 4px; }
    .row.header { display: none; }
    .actions { justify-content: flex-start; }
    .cell-label { display: inline; }
  }
</style>
