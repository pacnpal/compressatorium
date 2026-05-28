<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import IconButton from './IconButton.svelte';
  import X from '@lucide/svelte/icons/x';

  const note = $derived(ui.notification);
</script>

{#if note}
  <output class="toast tone-{note.kind}" aria-live="polite">
    <span class="msg">{note.message}</span>
    <IconButton label="Dismiss" size="sm" onclick={() => ui.dismissNotification()}>
      <X size={14} />
    </IconButton>
  </output>
{/if}

<style>
  .toast {
    position: fixed;
    bottom: var(--space-5);
    right: var(--space-5);
    z-index: var(--z-notification);
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-width: 240px;
    max-width: 420px;
    padding: var(--space-3) var(--space-4);
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    box-shadow: var(--elev-2);
    font-size: var(--text-sm);
  }
  .tone-success { border-color: var(--success); background: var(--success-muted); }
  .tone-warning { border-color: var(--warning); background: var(--warning-muted); }
  .tone-error { border-color: var(--error); background: var(--error-muted); }
  .tone-info { border-color: var(--info); background: var(--info-muted); }
  .msg { flex: 1; color: inherit; }
</style>
