<script>
  /**
   * The safety choices for a conversion: check each new file, delete the
   * original once it checks out, and the quick check. Controlled: every change
   * goes out through `onchange({ checkAfter, deleteOriginal, quickCheck })`.
   * `specs` are the mode specs the choice covers (one for a console's setting,
   * many for a plan); what each offers decides which controls show.
   *
   * @typedef {Object} Props
   * @property {object[]} specs
   * @property {boolean} checkAfter
   * @property {boolean} deleteOriginal
   * @property {boolean} quickCheck
   * @property {(state: { checkAfter: boolean, deleteOriginal: boolean, quickCheck: boolean }) => void} onchange
   * @property {string} [idPrefix]
   */
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import { safetyControls, applySafety } from '$lib/util/safetyOptions.js';

  /** @type {Props} */
  let { specs, checkAfter, deleteOriginal, quickCheck, onchange, idPrefix = 'safety' } = $props();

  const controls = $derived(safetyControls(specs, { deleteOriginal }));

  function change(patch) {
    onchange?.(applySafety({ checkAfter, deleteOriginal, quickCheck }, patch));
  }
</script>

<div class="safety" id={idPrefix}>
  {#if controls.showCheck}
    <span title={controls.checkLocked ? 'Deleting always checks first' : undefined}>
      <Checkbox
        checked={checkAfter || controls.checkLocked}
        disabled={controls.checkLocked}
        label="Check each new file"
        description="Reads it back to make sure it's good. Keeps the original."
        onchange={(v) => change({ checkAfter: v })}
      />
    </span>
  {/if}
  {#if controls.showDelete}
    <Checkbox
      checked={deleteOriginal}
      label="Delete the original after the new file checks out"
      description="Only after it reads back as an exact copy. If it doesn't, the original stays."
      onchange={(v) => change({ deleteOriginal: v })}
    />
    {#if controls.kept}
      <p class="kept">
        Kept for {controls.kept.games} {controls.kept.games === 1 ? 'game' : 'games'}: {controls.kept.reason}.
      </p>
    {/if}
  {/if}
  {#if controls.showQuick}
    <details class="more">
      <summary>More</summary>
      <Checkbox
        checked={quickCheck}
        label="Quick check only (faster, less thorough)"
        description="Checks the new file's structure instead of comparing it to the original."
        onchange={(v) => change({ quickCheck: v })}
      />
    </details>
  {/if}
</div>

<style>
  .safety {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }
  .kept {
    margin: 0;
    color: var(--text-2);
    font-size: var(--text-xs);
  }
  .more summary {
    cursor: pointer;
    color: var(--text-2);
    font-size: var(--text-sm);
    margin-bottom: var(--space-2);
  }
</style>
