"""Convert sits with the selection and on screen at laptop widths (#589).

The selection bar offered Delete but not Convert, and between 900px and the
three-panel layout the Convert panel stacked under the list, off screen. The
three panels needed a view about 1344px wide, so a 1440x900 laptop with the
240px sidebar never got them.

Now the selection bar has a primary "Convert N games" that runs the Convert
panel's own action; while games are selected in the stacked layout the panel
sticks to the bottom of the screen and a row just ticked is scrolled clear of
it; and at laptop widths Drives become a strip above the list with Convert
beside it, the Convert panel shrinking before the table does.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"

# The sidebar the shell shows at desktop widths, and the view's own padding
# (--space-5 a side) inside it.
_SIDEBAR = 240
_VIEW_PADDING = 48
# Room for a classic (non-overlay) scrollbar.
_SCROLLBAR = 16


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _css(rel: str) -> str:
    return _read(rel).split("<style>", 1)[1]


def _frozen(name: str) -> dict[str, int]:
    body = re.search(
        rf"const {name} = Object\.freeze\(\{{([^}}]*)\}}\)", _read("stores/layout.svelte.js"),
    ).group(1)
    return {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", body)}


def _right_min() -> int:
    limits = _read("stores/layout.svelte.js")
    return int(re.search(r"right:\s*\{\s*min:\s*(\d+)", limits).group(1))


def _table_min_width() -> int:
    cols = _frozen("DEFAULT_COLUMNS")
    expr = re.search(
        r"const tableMinWidth = \$derived\(([^;]*)\);", _read("components/panels/FileList.svelte"),
    ).group(1)
    expr = expr.replace("(nameSet ?? 240)", "240")
    for key, value in cols.items():
        expr = expr.replace(f"cols.{key}", str(value))
    assert re.fullmatch(r"[\d\s+]+", expr), expr
    return sum(int(n) for n in expr.split("+"))


def _list_room() -> int:
    """The table at its default widths plus the list's padding and the two frames."""
    return _table_min_width() + 36


def _tiers() -> list[int]:
    found = re.findall(
        r"@container\s*\(min-width:\s*(\d+)px\)", _css("components/views/FilesView.svelte"),
    )
    return sorted({int(n) for n in found})


# ── The selection bar ──────────────────────────────────────────────────────


def test_the_selection_bar_offers_convert_as_its_primary_action():
    fl = _read("components/panels/FileList.svelte")
    bar = fl[fl.index('class="selection-bar"'):fl.index("{#if canSelectAllPages}")]
    assert "const convertAction = $derived(conversion.convertAction);" in fl
    # Only while a Convert panel is on screen to run it.
    assert "{#if convertAction}" in bar
    assert 'variant="primary"' in bar
    # The same words the panel's own button uses: "Convert 3 games".
    assert "{convertLabel}" in bar
    assert re.search(r"const convertLabel = \$derived\.by\(.*?`Convert \$\{n\}", fl, re.S)
    # It runs the panel's action rather than a second copy of the submit flow.
    assert "convertAction.run" in bar or "action.run" in bar


def test_the_convert_panel_publishes_its_action_and_withdraws_it():
    panel = _read("components/panels/ConvertPanel.svelte")
    assert re.search(r"conversion\.publishConvert\(\{[^}]*count:\s*convertCount", panel, re.S)
    assert re.search(r"publishConvert\(\{[^}]*disabled", panel, re.S)
    assert re.search(r"publishConvert\(\{[^}]*run:\s*convert", panel, re.S)
    destroy = panel[panel.index("onDestroy(() => {"):]
    destroy = destroy[:destroy.index("});")]
    assert "publishConvert(null)" in destroy


def test_the_store_holds_the_action_without_proxying_it():
    store = _read("stores/conversion.svelte.js")
    assert re.search(r"convertAction\s*=\s*\$state\.raw\(null\)", store)
    assert re.search(r"publishConvert\(action\)\s*\{", store)


# ── The stacked layout keeps Convert on screen ─────────────────────────────


def test_the_stacked_layout_sticks_convert_to_the_bottom_of_the_screen():
    css = _css("components/views/FilesView.svelte")
    # A grid item can only stick inside its own grid area, so the stacked
    # layout is a flex column and the sheet sticks across the whole of it.
    stacked = css[css.index(".grid {"):]
    stacked = stacked[:stacked.index("}")]
    assert "display: flex;" in stacked
    assert "flex-direction: column;" in stacked
    sheet = css[css.index(".right.sheet {"):]
    sheet = sheet[:sheet.index("}")]
    assert "position: sticky;" in sheet
    assert "bottom: 0;" in sheet
    # Not fixed: a fixed sheet left the list under it and needed padding to
    # make room.
    assert "position: fixed" not in css
    assert "padding-bottom: 55dvh" not in css


def test_the_sheet_only_sticks_below_the_side_by_side_layout():
    css = _css("components/views/FilesView.svelte")
    tiers = _tiers()
    m = re.search(r"@container\s*\(max-width:\s*(\d+)px\)\s*\{\s*\.right\.sheet", css)
    assert m, "the sticky sheet is limited to the stacked layout"
    assert int(m.group(1)) == tiers[0] - 1


def test_a_ticked_row_is_scrolled_clear_of_the_sheet():
    view = _read("components/views/FilesView.svelte")
    # The sheet's height is measured, and rows keep that much room below them
    # when scrolled into view, by focus or by the script.
    assert "bind:offsetHeight={sheetHeight}" in view
    assert "--sheet-h" in view
    assert re.search(r"scroll-margin-bottom:\s*calc\(var\(--sheet-h", view)
    assert "scrollIntoView({ block: 'nearest' })" in view


# ── Laptop widths ──────────────────────────────────────────────────────────


def test_a_1440_laptop_puts_convert_beside_the_list():
    tiers = _tiers()
    assert len(tiers) == 2, tiers
    side_by_side = tiers[0]
    # The list holds the whole table, a 9px splitter and two 8px gaps, and
    # Convert at its narrowest.
    assert side_by_side >= _list_room() + 9 + 16 + _right_min()
    assert side_by_side <= 1440 - _SIDEBAR - _VIEW_PADDING - _SCROLLBAR


def test_three_panels_once_drives_fit_beside_them():
    three = _tiers()[1]
    needed = _frozen("DEFAULT_PANELS")["left"] + _right_min() + _list_room() + 18 + 32
    assert three >= needed
    # Lower than before (#562 needed 1344): Convert gives way first.
    assert three < 1344


def test_convert_shrinks_before_the_table_does():
    css = _css("components/views/FilesView.svelte")
    assert f"--list-room: {_list_room()}px" in css
    clamps = re.findall(r"clamp\(\s*(\d+)px,\s*calc\(100cqw.*?\),\s*var\(--ws-right", css, re.S)
    assert len(clamps) == 2, "both side-by-side layouts clamp the Convert panel"
    assert all(int(n) == _right_min() for n in clamps)


def test_drives_become_a_strip_above_the_list_when_given_the_width():
    vl = _css("components/panels/VolumeList.svelte")
    assert re.search(r"container:\s*drives\s*/\s*inline-size", vl)
    strip = re.search(r"@container drives \(min-width:\s*\d+px\)\s*\{(.*?)\n  \}", vl, re.S)
    assert strip and "flex-direction: row" in strip.group(1)
    css = _css("components/views/FilesView.svelte")
    assert re.search(r"\.side\s*\{\s*grid-column:\s*1\s*/\s*-1;", css)
