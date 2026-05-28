<script>
  // Compression configuration. Three "styles" supported, picked off the
  // tool descriptor:
  //   - 'multi'             chdman: comma-separated codec list (chips).
  //   - 'single-with-level' Dolphin RVZ/WIA: one codec + numeric level.
  //   - 'none'              z3ds: fixed compression; renders nothing.
  // The CompressionPicker has zero tool-specific branches — adding a new
  // tool means declaring its codec list + style in registry.js and
  // (optionally) a level range; this file does not need editing.

  import { conversion } from '$lib/stores/conversion.svelte.js';
  import Check from '@lucide/svelte/icons/check';

  const tool = $derived(conversion.currentTool);
  const spec = $derived(conversion.currentSpec);
  const codecs = $derived(tool?.compressionCodecs ?? []);
  const style = $derived(tool?.compressionStyle ?? 'none');
  const selection = $derived(conversion.compressionSelection);
  const level = $derived(conversion.dolphinCompressionLevel);
  const levelRange = $derived(
    tool?.compressionLevelRange ?? { min: 1, max: 22, default: 19 },
  );
  const supports = $derived(
    !!(spec?.supportsCompression || spec?.supportsCompressionLevel),
  );
  const singleCodec = $derived(
    selection.find((c) => c && c !== 'none') ?? 'none',
  );

  function isSelected(value) {
    return selection.includes(value);
  }
</script>

{#if supports && codecs.length > 0}
  <fieldset class="picker">
    <legend class="legend">Compression</legend>

    {#if style === 'multi'}
      <div class="chips" role="group" aria-label="Codec selection">
        <button
          type="button"
          class="chip"
          class:active={isSelected('none')}
          onclick={() => conversion.toggleCodec('none')}
        >
          {#if isSelected('none')}<Check size={12} aria-hidden="true" />{/if}
          None
        </button>
        {#each codecs as codec (codec.value)}
          {#if codec.value !== 'none'}
            <button
              type="button"
              class="chip"
              class:active={isSelected(codec.value)}
              title={codec.hint ?? ''}
              onclick={() => conversion.toggleCodec(codec.value)}
            >
              {#if isSelected(codec.value)}<Check size={12} aria-hidden="true" />{/if}
              {codec.label}
            </button>
          {/if}
        {/each}
      </div>
      <p class="hint">Select one or more codecs. chdman tries them in order.</p>

    {:else if style === 'single-with-level'}
      <label class="single-codec">
        <span class="sublabel">Codec</span>
        <select
          value={singleCodec}
          onchange={(e) => conversion.setSingleCodec(e.currentTarget.value)}
          aria-label="Codec"
        >
          {#each codecs as codec (codec.value)}
            <option value={codec.value} title={codec.hint ?? ''}>{codec.label}</option>
          {/each}
        </select>
      </label>

      {#if singleCodec && singleCodec !== 'none'}
        <label class="level">
          <span class="sublabel">Level (compression effort)</span>
          <div class="level-row">
            <input
              type="range"
              min={levelRange.min}
              max={levelRange.max}
              step="1"
              value={level}
              oninput={(e) => conversion.setDolphinLevel(e.currentTarget.value)}
              aria-label="Compression level"
            />
            <input
              class="level-input"
              type="number"
              min={levelRange.min}
              max={levelRange.max}
              step="1"
              value={level}
              oninput={(e) => conversion.setDolphinLevel(e.currentTarget.value)}
              aria-label="Compression level value"
            />
          </div>
          <p class="hint">
            {levelRange.min}–{levelRange.max}; {levelRange.default} matches MAME Redump.
          </p>
        </label>
      {/if}
    {/if}
  </fieldset>
{/if}

<style>
  .picker {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--space-3);
    background: var(--surface-2);
    margin: 0;
    min-width: 0;
  }
  .legend {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
    padding: 0 var(--space-1);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    border: 1px solid var(--border-subtle);
    background: var(--surface-1);
    color: var(--text-2);
    border-radius: var(--radius-full);
    padding: 4px 10px;
    font-size: var(--text-xs);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-out),
      color var(--dur-fast) var(--ease-out),
      border-color var(--dur-fast) var(--ease-out);
  }
  .chip:hover {
    background: var(--surface-3);
    color: var(--text-1);
  }
  .chip.active {
    background: var(--accent-muted);
    color: var(--accent);
    border-color: var(--accent);
  }
  .chip:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .single-codec,
  .level {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .sublabel {
    font-size: var(--text-xs);
    color: var(--text-2);
  }
  select,
  input[type='number'] {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: 6px 8px;
  }
  select:focus-visible,
  input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .level-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .level-row input[type='range'] {
    flex: 1;
    accent-color: var(--accent);
  }
  .level-input {
    width: 60px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .hint {
    margin: 0;
    color: var(--text-3);
    font-size: var(--text-xs);
  }
</style>
