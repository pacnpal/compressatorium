<script>
  // Three-state toggle driven by mode-watcher. mode.current resolves to
  // "light" or "dark" (never "system"); userPrefersMode.current tells us
  // whether the user explicitly picked light/dark or is following the OS.
  import { userPrefersMode, setMode } from 'mode-watcher';
  import Sun from '@lucide/svelte/icons/sun';
  import Moon from '@lucide/svelte/icons/moon';
  import Monitor from '@lucide/svelte/icons/monitor';
  import IconButton from '$lib/components/ui/IconButton.svelte';

  const userPref = $derived(userPrefersMode.current);
  const next = $derived(
    userPref === 'light' ? 'dark' : userPref === 'dark' ? 'system' : 'light',
  );
  const label = $derived(`Theme: ${userPref}, switch to ${next}`);

  // Pick the icon by the user's pick when explicit, otherwise show the
  // monitor icon to telegraph "following system".
  const Icon = $derived(
    userPref === 'system'
      ? Monitor
      : userPref === 'light'
        ? Sun
        : Moon,
  );

  function cycle() {
    setMode(next);
  }
</script>

<IconButton {label} title={label} size="md" onclick={cycle}>
  <Icon size={16} />
</IconButton>
