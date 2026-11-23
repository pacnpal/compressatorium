"""Files' three-panel layout only when the file table fits (#562).

At a 1280px viewport the 240px sidebar left the file list about 350px, so
the table scrolled sideways and only Name showed. The three panels now come
from the width the view itself has (a container query), and only once that
width holds the Volumes rail, the Convert panel and the whole table at their
default widths. The Size column's default holds the longest size it shows.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _frozen(name: str) -> dict[str, int]:
    body = re.search(
        rf"const {name} = Object\.freeze\(\{{([^}}]*)\}}\)", _read("stores/layout.svelte.js"),
    ).group(1)
    return {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", body)}


def _table_min_width() -> int:
    """FileList's own table min-width, evaluated at the default columns."""
    cols = _frozen("DEFAULT_COLUMNS")
    expr = re.search(
        r"const tableMinWidth = \$derived\(([^;]*)\);", _read("components/panels/FileList.svelte"),
    ).group(1)
    expr = expr.replace("(nameSet ?? 240)", "240")
    for key, value in cols.items():
        expr = expr.replace(f"cols.{key}", str(value))
    assert re.fullmatch(r"[\d\s+]+", expr), expr
    return sum(int(n) for n in expr.split("+"))


def _container_breakpoint(rel: str) -> int:
    """The widest layout's breakpoint: where every side panel sits beside the list."""
    css = _read(rel)
    found = re.findall(r"@container\s*\(min-width:\s*(\d+)px\)", css)
    assert found, f"{rel} has a container breakpoint for its side panels"
    return max(int(n) for n in found)


def _right_min() -> int:
    return int(re.search(
        r"right:\s*\{\s*min:\s*(\d+)", _read("stores/layout.svelte.js"),
    ).group(1))


def test_the_size_column_holds_the_longest_size():
    # "1023.99 MB" is 73px in the table's 13px font, plus 12px padding a side.
    assert _frozen("DEFAULT_COLUMNS")["size"] >= 73 + 24


def test_files_goes_three_panel_only_when_the_whole_table_fits():
    view = _read("components/views/FilesView.svelte")
    assert "container-type: inline-size" in view
    assert not re.search(r"@media \(min-width: 1280px\)", view)
    panels = _frozen("DEFAULT_PANELS")
    # The list's padding (16px a side), its border and the table frame's, two
    # 9px splitters and four 8px gaps between the five grid tracks. Convert
    # shrinks to its narrowest before the table gives way (#589).
    needed = panels["left"] + _right_min() + _table_min_width() + 36 + 18 + 32
    assert _container_breakpoint("components/views/FilesView.svelte") >= needed


def test_romm_browse_puts_convert_beside_the_list_only_when_the_table_fits():
    browse = _read("components/library/RommBrowse.svelte")
    assert "container-type: inline-size" in browse
    assert not re.search(r"@media \(min-width: 1280px\)", browse)
    needed = _frozen("DEFAULT_PANELS")["right"] + _table_min_width() + 36 + 9 + 16
    assert _container_breakpoint("components/library/RommBrowse.svelte") >= needed


def test_the_main_column_never_sizes_to_its_content():
    """Under 900px the sidebar is hidden and the main column fell into the
    shell's first, auto-sized track. A size container contributes no width,
    so Files collapsed to 159px on a phone."""
    app = (_SRC.parent / "App.svelte").read_text(encoding="utf-8")
    css = app.split("<style>", 1)[1]
    main_col = css[css.index(".main-col {"):]
    assert "grid-column: 2;" in main_col[:main_col.index("}")]
