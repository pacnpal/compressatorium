"""Determinism guards for issue #183.

One test per ordering site the audit flagged: the registry extension-union
helpers, the recursive search walk and subprocess line segmentation. Each asserts the output is a pure,
stable function of its input rather than of hash-seed / readdir / chunk-boundary
/ wall-clock order.
"""

from __future__ import annotations

import pytest

from app.routes import files as files_routes
from app.services.subprocess_runner import _split_stream_lines
from app.services.tools import registry

# ---------------------------------------------------------------------------
# Site 1: registry union helpers return sorted, stable sequences
# ---------------------------------------------------------------------------

UNION_HELPERS = [
    "convertible_extensions",
    "archive_input_extensions",
    "verify_extensions",
    "output_extensions",
    "scannable_extensions",
]


@pytest.mark.parametrize("helper", UNION_HELPERS)
def test_registry_union_helpers_return_sorted_tuples(helper):
    result = getattr(registry, helper)()
    assert isinstance(result, tuple), f"{helper} must return a tuple"
    assert list(result) == sorted(result), f"{helper} must be sorted"
    assert len(result) == len(set(result)), f"{helper} must be deduplicated"


@pytest.mark.parametrize("helper", UNION_HELPERS)
def test_registry_union_helpers_are_byte_stable(helper):
    fn = getattr(registry, helper)
    assert fn() == fn()  # identical ordered sequence run-to-run


# ---------------------------------------------------------------------------
# Site 2: recursive search returns sorted order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_files_returns_sorted_order(tmp_path, monkeypatch):
    # Convertible (.cue -> chdman) files created in non-alphabetical order.
    for name in ["zebra.cue", "alpha.cue", "mango.cue", "beta.cue", "delta.cue"]:
        (tmp_path / name).write_bytes(b"cue")
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    result = await files_routes.search_files(path=str(tmp_path))
    names = [f["name"] for f in result["files"]]

    assert names == sorted(names)
    assert names == ["alpha.cue", "beta.cue", "delta.cue", "mango.cue", "zebra.cue"]


# ---------------------------------------------------------------------------
# Site 5: line segmentation is a pure function of the byte stream
# ---------------------------------------------------------------------------


def test_split_stream_lines_is_chunk_boundary_independent():
    # CRLF, a bare CR (progress redraw), an LF, and a partial trailing line.
    stream = "alpha\r\nbeta\rgamma\ndelta"
    whole_lines, whole_remainder = _split_stream_lines(stream)
    assert whole_lines == ["alpha", "beta", "gamma"]
    assert whole_remainder == "delta"

    # Feeding the same bytes split at *every* boundary yields identical lines.
    for cut in range(len(stream) + 1):
        buf = ""
        emitted: list[str] = []
        for piece in (stream[:cut], stream[cut:]):
            buf += piece
            lines, buf = _split_stream_lines(buf)
            emitted.extend(lines)
        assert emitted == whole_lines, f"cut={cut}"
        assert buf == whole_remainder, f"cut={cut}"


def test_split_stream_lines_strips_and_drops_blanks():
    lines, remainder = _split_stream_lines("  a  \n\n b \n")
    assert lines == ["a", "b"]
    assert remainder == ""
