<script>
  import { onMount, tick } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { startRouter } from '$lib/router/hashRouter.js';
  import Sidebar from '$lib/components/layout/Sidebar.svelte';
  import MobileDrawer from '$lib/components/layout/MobileDrawer.svelte';
  import TopBar from '$lib/components/layout/TopBar.svelte';
  import FilesView from '$lib/components/views/FilesView.svelte';
  import LibrariesView from '$lib/components/views/LibrariesView.svelte';
  import ActivityView from '$lib/components/views/ActivityView.svelte';
  import SettingsView from '$lib/components/views/SettingsView.svelte';
  import HelpView from '$lib/components/views/HelpView.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import { registry } from '$lib/tools/registry.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { terminalJobIds } from '$lib/stores/jobConstants.js';
  import { jobToasts } from '$lib/stores/jobToasts.svelte.js';
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { findings } from '$lib/stores/findings.svelte.js';
  import { defaults } from '$lib/stores/defaults.svelte.js';
  import { ModeWatcher, mode } from 'mode-watcher';
  import { Toaster, toast } from 'svelte-sonner';
  import { STORAGE_KEYS } from '$lib/util/localStorage.js';
  import BulkVerifyModal from '$lib/components/modals/BulkVerifyModal.svelte';
  import BulkDeleteModal from '$lib/components/modals/BulkDeleteModal.svelte';
  import PlaylistModal from '$lib/components/modals/PlaylistModal.svelte';
  import DatRenameModal from '$lib/components/modals/DatRenameModal.svelte';
  import DeleteModal from '$lib/components/modals/DeleteModal.svelte';
  import RenameModal from '$lib/components/modals/RenameModal.svelte';
  import PatchModal from '$lib/components/modals/PatchModal.svelte';
  import FileInfoModal from '$lib/components/modals/FileInfoModal.svelte';
  import CancelAllJobsModal from '$lib/components/modals/CancelAllJobsModal.svelte';
  import ClearDoneModal from '$lib/components/modals/ClearDoneModal.svelte';
  import MappingModal from '$lib/components/modals/MappingModal.svelte';

  let mainEl;
  // Why the tools have not loaded yet, once a fetch of them failed.
  let toolsError = $state('');

  $effect(() => {
    ui.focusBump;
    if (!mainEl) return;
    tick().then(() => {
      try {
        mainEl.focus({ preventScroll: false });
      } catch (_e) {
        // ignore, focus may fail on disconnected nodes mid-route swap
      }
    });
  });

  // Refresh the file listing when terminal job events arrive, newly
  // written outputs need to appear, delete-on-verify removals need to
  // disappear, sibling badges flip.
  //
  // Refresh interferes with whatever the user was doing if we don't
  // gate it carefully. Skip while any of the following hold; the
  // ids it refreshed for advance only once refresh fires, so the gates
  // clearing (modal closes, batch finishes, etc.) immediately re-runs
  // the effect and lets the deferred refresh through.
  //   - User toggled auto-refresh off explicitly.
  //   - Any modal that reads file-listing state is open.
  //   - A conversion submit is mid-flight.
  //   - A verify batch is running.
  //   - fileBrowser is already loading.
  //   - We're in search mode (refresh() short-circuits anyway).
  //
  // Keyed on which jobs finished, not how many, as the dashboard is: at the
  // history cap a newly finished job replaces an evicted one and the counts
  // stay flat. Clearing history only drops ids, so nothing refreshes then.
  let _refreshedFor = new Set();
  $effect(() => {
    const finished = terminalJobIds(jobs.jobs);
    if (finished.every((id) => _refreshedFor.has(id))) {
      _refreshedFor = new Set(finished);
      return;
    }
    if (!fileBrowser.autoRefresh) return;
    if (fileBrowser.loading) return;
    if (fileBrowser.searchMode) return;
    if (ui.anyEntryModalOpen) return;
    if (conversion.converting) return;
    if (verification.batchRun) return;
    _refreshedFor = new Set(finished);
    fileBrowser.refresh().catch(() => {});
  });

  // What needs attention can change whenever a run finishes (a check, a
  // duplicate scan, a watch folder's intake), so the sidebar badge and
  // Activity's opening tab reload it then, whether or not Needs attention is
  // on screen. Keyed on which jobs finished, as the refresh above is.
  const finishedRuns = $derived(terminalJobIds(jobs.jobs).join(','));
  $effect(() => {
    void finishedRuns;
    findings.load();
  });

  // Surface a live toast for every RUNNING job and resolve it on
  // completion. Iterating the list (and touching status/progress/message)
  // subscribes the effect at index granularity, so SSE progress frames,
  // which replace a job slot via index assignment in jobs._applyJob,
  // re-run the reconcile. jobToasts keys each toast by job id, so this is
  // idempotent across re-runs.
  $effect(() => {
    const list = jobs.jobs;
    const showExternalScan = jobs.showExternalScanJobs;
    for (const j of list) {
      void j?.status;
      void j?.progress;
      void j?.message;
    }
    jobToasts.reconcile(list, { showExternalScan });
  });

  function handleSkip(e) {
    e.preventDefault();
    mainEl?.focus({ preventScroll: false });
  }

  function onBoundaryError(err, _reset) {
    console.error('View boundary caught error:', err);
    toast.error('Something went wrong. Try again.', { duration: 6000 });
  }

  onMount(() => {
    ui.loadVersion();
    // The tools, their formats and modes come from the backend, and so does
    // which of them can run and why the others can't (#515). The views wait
    // for it; a failed fetch is retried, since nothing converts without it.
    // One request at a time: the next is only scheduled once this one failed,
    // so no older reply can land after a newer one. Nothing runs after unmount.
    let toolsRetry;
    let disposed = false;
    const loadTools = (delay = 1000) => {
      api.getTools()
        .then((t) => {
          if (disposed) return;
          if (!registry.load(t)) throw new Error('The server named no tools');
          toolsError = '';
          ui.applyToolAvailability(t?.available, t?.reasons);
          // Remembered per-tool compression settings, applied over the defaults.
          conversion.loadServerPrefs();
        })
        .catch(() => {
          if (disposed) return;
          toolsError = "Can't reach the server to load the tools. Trying again…";
          toolsRetry = setTimeout(() => loadTools(Math.min(delay * 2, 10000)), delay);
        });
    };
    loadTools();
    // The safety choices new conversions start with.
    defaults.load();
    // Rehydrate the verified set + DAT-library state so OK / DAT badges
    // survive reloads. Fire and forget, failure leaves the cache empty.
    verification.loadVerified();
    // Polls only while nothing can answer a lookup, and stops itself as soon
    // as something can -- so a provider enabled from another tab or by an API
    // client reaches this one without a reload, and a configured install never
    // makes the request at all.
    datMatching.watchMatchingAvailability();
    jobs.connect();
    const stopRouter = startRouter();
    return () => {
      disposed = true;
      clearTimeout(toolsRetry);
      datMatching.stopWatchingAvailability();
      stopRouter();
      jobs.dispose();
      jobToasts.dispose();
    };
  });
</script>

<!--
  mode-watcher manages the dark/light class on <html> + color-scheme. The
  matching FOUC-prevention inline script lives in index.html (runs
  synchronously before this component mounts), so we disable mode-watcher's
  own head-script injection to avoid double work. Storage key matches
  STORAGE_KEYS.THEME so existing users' preferences survive the swap.
-->
<ModeWatcher
  modeStorageKey={STORAGE_KEYS.THEME}
  defaultMode="system"
  disableHeadScriptInjection
/>

<!-- Toast surface. theme prop tracks the resolved mode so toasts match. -->
<Toaster
  theme={mode.current ?? 'system'}
  richColors
  closeButton
  position="bottom-right"
/>

<!-- Modal portal, each component self-renders based on its ui store target. -->
<BulkVerifyModal />
<BulkDeleteModal />
<PlaylistModal />
<DatRenameModal />
<DeleteModal />
<RenameModal />
<PatchModal />
<FileInfoModal />
<CancelAllJobsModal />
<ClearDoneModal />
<MappingModal />

<a class="skip-link" href="#main-content" onclick={handleSkip}>Skip to main content</a>

<div class="shell">
  <div class="sidebar-host"><Sidebar /></div>
  <MobileDrawer />
  <div class="main-col">
    <TopBar />
    <main id="main-content" bind:this={mainEl} class="main" tabindex="-1" aria-label="Main content">
      <svelte:boundary onerror={onBoundaryError}>
        {#if !ui.toolsKnown}
          <div class="tools-loading" role="status">
            <Spinner size="md" /> {toolsError || 'Loading the tools…'}
          </div>
        {:else if ui.activeView === 'files'}<FilesView />
        {:else if ui.activeView === 'libraries'}<LibrariesView />
        {:else if ui.activeView === 'activity'}<ActivityView />
        {:else if ui.activeView === 'settings'}<SettingsView />
        {:else if ui.activeView === 'help'}<HelpView />
        {/if}
        {#snippet failed(error, reset)}
          <div class="error-frame">
            <EmptyState title="This view crashed" description={error?.message ?? 'An unexpected error occurred while rendering.'} glyph="!">
              {#snippet actions()}
                <Button variant="primary" onclick={reset}>Try again</Button>
              {/snippet}
            </EmptyState>
          </div>
        {/snippet}
      </svelte:boundary>
    </main>
  </div>
</div>

<style>
  .shell {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    height: 100dvh;
    background: var(--surface-2);
  }
  .sidebar-host { display: none; }
  @media (min-width: 900px) { .sidebar-host { display: block; } }
  .main-col {
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--surface-2);
  }
  /* <main> takes focus on every route change so a screen reader starts there.
     It isn't a control (tabindex -1), so it draws no ring. */
  /* The page (not <main>) scrolls: the shell's row grows with its content. So
     <main> clips sideways without being a scroll box, or a sticky child (the
     Settings section list) would stick to a box that never scrolls. */
  .main { flex: 1; overflow-x: clip; outline: none; }
  .main:focus, .main:focus-visible { box-shadow: none; }

  .skip-link {
    position: fixed;
    top: var(--space-2);
    left: var(--space-2);
    z-index: calc(var(--z-notification) + 1);
    padding: var(--space-2) var(--space-4);
    background: var(--accent);
    color: var(--accent-contrast);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
    font-weight: var(--weight-semibold);
    text-decoration: none;
    transform: translateY(-150%);
    transition: transform var(--dur-base) var(--ease-out);
  }
  .skip-link:focus-visible {
    transform: translateY(0);
    box-shadow: var(--elev-3);
  }
  .error-frame { padding: var(--space-6); }
  .tools-loading {
    display: flex; align-items: center; gap: var(--space-2);
    padding: var(--space-6); color: var(--text-2); font-size: var(--text-sm);
  }
</style>
