<script>
  /**
   * The one-time Hasheous question: asked until the user answers either way,
   * since answering saves the choice (`PUT /api/dat/hasheous` sets the
   * override) and the question never comes back.
   *
   * @typedef {Object} Props
   * @property {boolean} busy
   * @property {(yes: boolean) => void} onanswer
   */
  import Button from '$lib/components/ui/Button.svelte';

  /** @type {Props} */
  let { busy, onanswer } = $props();
</script>

<div class="ask" role="group" aria-labelledby="hasheous-ask">
  <div class="text">
    <strong id="hasheous-ask">Identify games online?</strong>
    <span>
      Hasheous looks up each game's checksum to name it and tell good dumps from
      bad. Only checksums are sent, and your own game lists are checked first.
      You can change this later.
    </span>
  </div>
  <div class="buttons">
    <Button variant="secondary" size="sm" disabled={busy} onclick={() => onanswer(false)}>
      No, stay offline
    </Button>
    <Button size="sm" loading={busy} disabled={busy} onclick={() => onanswer(true)}>
      Yes, look them up
    </Button>
  </div>
</div>

<style>
  .ask {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: var(--space-3);
    padding: var(--space-3);
    /* A tone, not a second border inside the card (#597). */
    background: var(--accent-muted);
    border-radius: var(--radius-md);
  }
  .text { display: flex; flex-direction: column; gap: 2px; flex: 1 1 280px; min-width: 0; }
  .text strong { color: var(--text-1); }
  .text span { color: var(--text-2); font-size: var(--text-sm); }
  .buttons { display: flex; flex-wrap: wrap; gap: var(--space-2); }
</style>
