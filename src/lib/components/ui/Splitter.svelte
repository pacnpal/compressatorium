<script>
  /**
   * A draggable vertical separator. Generic enough to drive both the
   * workspace panel split and individual table-column widths.
   *
   * It owns no width itself. During a drag it reports the cumulative
   * pointer delta (px, positive = moved right) via `onmove`; the parent
   * snapshots the starting width on `onstart` and applies the delta in
   * whichever direction makes sense for that edge. Arrow keys nudge via
   * `onstep`; double-click asks the parent to reset via `onreset`.
   *
   * @typedef {Object} Props
   * @property {string} label - accessible name for the separator
   * @property {number} [value] - current width, for aria-valuenow
   * @property {number} [min] - for aria-valuemin
   * @property {number} [max] - for aria-valuemax
   * @property {number} [step] - keyboard nudge in px (default 8)
   * @property {() => void} [onstart]
   * @property {(delta: number) => void} [onmove]
   * @property {() => void} [onend]
   * @property {(delta: number) => void} [onstep]
   * @property {() => void} [onreset]
   * @property {'panel'|'column'} [variant]
   */

  /** @type {Props} */
  let {
    label,
    value,
    min,
    max,
    step = 8,
    onstart,
    onmove,
    onend,
    onstep,
    onreset,
    variant = 'panel',
  } = $props();

  let dragging = $state(false);
  let startX = 0;

  function pointerdown(e) {
    // Left button / primary pointer only.
    if (e.button != null && e.button !== 0) return;
    e.preventDefault();
    startX = e.clientX;
    dragging = true;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    onstart?.();
  }

  function pointermove(e) {
    if (!dragging) return;
    onmove?.(e.clientX - startX);
  }

  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    // releasePointerCapture throws (NotFoundError) if capture was never
    // established or was already lost; we only care that the drag ends.
    try {
      e.currentTarget.releasePointerCapture?.(e.pointerId);
    } catch {
      // ignore — capture already gone
    }
    onend?.();
  }

  function keydown(e) {
    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      onstep?.(-step);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      onstep?.(step);
    } else if (e.key === 'Home' || e.key === 'Enter') {
      // Enter/Home both reset to default — a discoverable keyboard path.
      e.preventDefault();
      onreset?.();
    }
  }
</script>

<!--
  A focusable resizable separator is the WAI-ARIA "window splitter"
  pattern (role=separator + tabindex + aria-valuenow). The a11y linter
  treats `separator` as non-interactive and so flags the tabindex and
  the pointer/key handlers; for this widget they're correct and required.
-->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
  class="splitter {variant}"
  class:dragging
  role="separator"
  aria-orientation="vertical"
  aria-label={label}
  aria-valuenow={value}
  aria-valuemin={min}
  aria-valuemax={max}
  tabindex="0"
  onpointerdown={pointerdown}
  onpointermove={pointermove}
  onpointerup={endDrag}
  onpointercancel={endDrag}
  ondblclick={() => onreset?.()}
  onkeydown={keydown}
></div>

<style>
  .splitter {
    /* The visible bar is thin; the grab/hit area is wider via padding so
       it's easy to land on without a precise mouse. */
    align-self: stretch;
    box-sizing: border-box;
    background-clip: content-box;
    cursor: col-resize;
    touch-action: none;
    user-select: none;
    background-color: transparent;
    transition: background-color var(--dur-fast) var(--ease-out);
  }
  .splitter.panel {
    width: 9px;
    padding-inline: 3px; /* 3px visible bar centered in a 9px hit area */
  }
  .splitter.column {
    /* Sits on the right edge of a table header cell. */
    width: 11px;
    padding-inline: 5px;
  }
  .splitter:hover,
  .splitter:focus-visible,
  .splitter.dragging {
    background-color: var(--accent, var(--text-2));
  }
  .splitter:focus-visible {
    outline: 2px solid var(--accent, var(--text-1));
    outline-offset: 1px;
  }
</style>
