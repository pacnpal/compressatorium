<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import Sun from '@lucide/svelte/icons/sun';
  import Moon from '@lucide/svelte/icons/moon';
  import Monitor from '@lucide/svelte/icons/monitor';
  import IconButton from '$lib/components/ui/IconButton.svelte';

  const theme = $derived(ui.theme);
  const next = $derived(theme === 'light' ? 'dark' : theme === 'dark' ? 'system' : 'light');
  const label = $derived(`Theme: ${theme} — switch to ${next}`);
  const Icon = $derived(theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor);
</script>

<IconButton {label} title={label} size="md" onclick={() => ui.cycleTheme()}>
  <Icon size={16} />
</IconButton>
