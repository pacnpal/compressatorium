<script>
  import NotificationSettings from '$lib/components/views/NotificationSettings.svelte';
</script>

<section class="view" aria-labelledby="settings-title">
  <header class="header">
    <h1 id="settings-title">Settings</h1>
    <p class="hint">Where Compressatorium sends messages. Nothing is sent until you add a destination.</p>
  </header>
  <NotificationSettings />
</section>

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
    padding: var(--space-5);
    max-width: var(--container-max);
    margin: 0 auto;
    width: 100%;
    min-width: 0;
  }
  .header h1 {
    margin: 0;
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .hint {
    color: var(--text-2);
    margin-top: var(--space-1);
  }
</style>
