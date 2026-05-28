<script>
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { formatSize } from '$lib/api/format.js';
  import Badge from '$lib/components/ui/Badge.svelte';
  import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import XIcon from '@lucide/svelte/icons/x';
  import EyeOff from '@lucide/svelte/icons/eye-off';
  import CircleCheck from '@lucide/svelte/icons/circle-check';
  import CircleX from '@lucide/svelte/icons/circle-x';
  import CircleSlash from '@lucide/svelte/icons/circle-slash';
  import Loader from '@lucide/svelte/icons/loader-circle';
  import Clock from '@lucide/svelte/icons/clock';

  /** @type {{ job: any }} */
  let { job } = $props();

  const status = $derived(job?.status ?? 'unknown');
  const mode = $derived(job?.mode ?? '');
  const spec = $derived(registry.specFor(mode));
  const tool = $derived(registry.toolForMode(mode));
  const isActive = $derived(status === 'queued' || status === 'processing');
  const isTerminal = $derived(
    status === 'completed' || status === 'failed' || status === 'cancelled',
  );

  // Backend emits `progress` as a number between 0 and 100. Keep it null
  // on queued/unstarted jobs so ProgressBar renders an empty track (not
  // a misleading 0% complete).
  const percent = $derived(
    isActive && typeof job?.progress === 'number' ? job.progress : null,
  );

  const filename = $derived(
    job?.file_path?.split(/[/\\]/).pop() ?? job?.file_path ?? '(unknown)',
  );

  function statusIcon(s) {
    switch (s) {
      case 'completed': return CircleCheck;
      case 'failed':    return CircleX;
      case 'cancelled': return CircleSlash;
      case 'processing': return Loader;
      case 'queued':    return Clock;
      default:          return Clock;
    }
  }

  function statusTone(s) {
    switch (s) {
      case 'completed': return 'success';
      case 'failed':    return 'error';
      case 'cancelled': return 'neutral';
      case 'processing': return 'converting';
      case 'queued':    return 'info';
      default:          return 'neutral';
    }
  }

  const StatusIcon = $derived(statusIcon(status));

  async function handleCancel() {
    try {
      await jobs.cancel(job.id);
    } catch (_e) {
      // jobs.cancel surfaces nothing to toast — the store applies
      // optimistic state; the SSE delivers the terminal frame.
    }
  }
</script>

<li class="job-row" class:terminal={isTerminal}>
  <div class="header">
    <span class="status" title={status}>
      <StatusIcon size={14} class={status === 'processing' ? 'spin' : ''} aria-hidden="true" />
    </span>
    <div class="meta">
      <div class="filename" title={job?.file_path}>{filename}</div>
      <div class="sub">
        {tool?.label ?? ''} ·
        {spec?.label ?? mode}
        {#if job?.compression}· {job.compression}{/if}
        {#if job?.output_size}· {formatSize(job.output_size)}{/if}
      </div>
    </div>
    <div class="actions">
      <Badge tone={statusTone(status)} size="sm">{status}</Badge>
      {#if isActive}
        <IconButton label="Cancel job" size="sm" onclick={handleCancel}>
          <XIcon size={12} />
        </IconButton>
      {:else if isTerminal}
        <IconButton
          label="Dismiss"
          size="sm"
          onclick={() => jobs.hideLocally(job.id)}
        >
          <EyeOff size={12} />
        </IconButton>
      {/if}
    </div>
  </div>

  {#if isActive}
    <ProgressBar value={percent} size="sm" tone="accent" />
    {#if job?.message}
      <div class="message">{job.message}</div>
    {/if}
  {:else if status === 'failed' && job?.error}
    <div class="error-message">{job.error}</div>
  {:else if status === 'completed' && job?.output_path}
    <div class="message muted" title={job.output_path}>→ {job.output_path}</div>
  {/if}
</li>

<style>
  .job-row {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-1);
    min-width: 0;
  }
  .terminal { background: var(--surface-2); }

  .header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }
  .status {
    display: inline-flex;
    align-items: center;
    color: var(--text-2);
    flex-shrink: 0;
  }
  .meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .filename {
    color: var(--text-1);
    font-size: var(--text-sm);
    font-weight: var(--weight-medium);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .sub {
    color: var(--text-3);
    font-size: var(--text-xs);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .actions {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    flex-shrink: 0;
  }
  .message {
    color: var(--text-2);
    font-size: var(--text-xs);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .message.muted { color: var(--text-3); font-family: var(--font-mono); }
  .error-message {
    color: var(--error);
    font-size: var(--text-xs);
    background: var(--error-muted);
    border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2);
  }

  :global(.job-row .spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
