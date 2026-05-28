<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import IconButton from '$lib/components/ui/IconButton.svelte';

  const theme = $derived(ui.theme);
  const next = $derived(
    theme === 'light' ? 'dark' : theme === 'dark' ? 'system' : 'light',
  );
  const label = $derived(`Theme: ${theme} — switch to ${next}`);
  const glyph = $derived(theme === 'light' ? '☀' : theme === 'dark' ? '☾' : '◐');
</script>

<IconButton {label} title={label} size="md" onclick={() => ui.cycleTheme()}>
  <span class="glyph">{glyph}</span>
</IconButton>

<style>
  .glyph {
    font-size: 16px;
    line-height: 1;
  }
</style>
