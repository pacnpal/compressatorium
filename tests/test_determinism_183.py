"""Determinism guards for issue #183.

One test per ordering site the audit flagged: the registry extension-union
helpers, the recursive search walk, subprocess line segmentation, and the
cross-process FIFO concurrency ticket. Each asserts the output is a pure,
stable function of its input rather than of hash-seed / readdir / chunk-boundary
/ wall-clock order.
"""

from __future__ import annotations

import os

import pytest

from app.routes import files as files_routes
from app.services.concurrency_manager import ConcurrencyManager
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


# ---------------------------------------------------------------------------
# Site 4: FIFO ticket is unique, ordered, and fallback-safe
# ---------------------------------------------------------------------------


def _mk_cm(tmp_path, n=1):
    return ConcurrencyManager(max_concurrent=n, lock_dir=str(tmp_path / "locks"))


def test_tickets_are_monotonic_and_fifo_by_key(tmp_path):
    cm = _mk_cm(tmp_path)
    t1 = cm.reserve_ticket("jobA")
    t2 = cm.reserve_ticket("jobB")
    t3 = cm.reserve_ticket("jobC")
    assert t1 < t2 < t3
    assert cm._list_tickets() == ["jobA", "jobB", "jobC"]


def test_reserve_ticket_is_idempotent_per_key(tmp_path):
    cm = _mk_cm(tmp_path)
    assert cm.reserve_ticket("job") == cm.reserve_ticket("job")
    assert cm._list_tickets() == ["job"]


def test_ticket_files_are_unique_per_reservation(tmp_path):
    cm = _mk_cm(tmp_path)
    for key in ("a", "b", "c", "d"):
        cm.reserve_ticket(key)
    ticket_files = [
        f for f in os.listdir(cm.lock_dir)
        if f.startswith("queue_") and f.endswith(".ticket")
    ]
    assert len(ticket_files) == len(set(ticket_files)) == 4


def test_counter_fallback_is_strictly_increasing_and_in_range(tmp_path, monkeypatch):
    cm = _mk_cm(tmp_path)
    cm._next_ticket()  # advance the on-disk counter to 1
    cm._next_ticket()  # ... and 2, so the fallback must seed above it

    # Point the counter at a path under a missing dir so reads/writes raise
    # OSError and the in-process fallback takes over.
    monkeypatch.setattr(
        cm, "_ticket_counter_path", str(tmp_path / "missing" / "counter"),
    )
    fallbacks = [cm._next_ticket() for _ in range(3)]

    assert fallbacks == sorted(fallbacks)        # strictly increasing
    assert len(set(fallbacks)) == 3              # unique, never colliding
    assert all(t < 10_000 for t in fallbacks)    # small in-range ints, not wall-clock ms
    assert fallbacks[0] > 2                       # seeded above the on-disk high-water mark


def test_colliding_ticket_int_does_not_double_admit(tmp_path, monkeypatch):
    # Force every ticket to the SAME integer (simulating a counter collision);
    # admission must still pick exactly the first max_concurrent by (seq, key),
    # not let a duplicated int put two jobs in the same prefix slot.
    cm = ConcurrencyManager(max_concurrent=2, lock_dir=str(tmp_path / "locks"))
    monkeypatch.setattr(cm, "_next_ticket", lambda: 7)
    for key in ("a", "b", "c"):
        cm.reserve_ticket(key)

    keys = cm._list_tickets()
    assert keys == ["a", "b", "c"]   # ordered by reservation seq, then key
    assert keys[:2] == ["a", "b"]    # only the first two are admitted
