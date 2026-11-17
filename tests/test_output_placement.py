"""The shared final move of a product onto its output path (#422 review).

``services.output_placement.OutputPlacement`` is what extract-xiso's and
rom-converto's services use to put a product finished in a private work folder
in place: never over an existing file, and safe to give up on. These pin the
helper itself; ``test_xiso_service.py`` and ``test_rom_converto.py`` pin the
two services on it.
"""
from __future__ import annotations

import asyncio
import os
import threading

import pytest

from app.services.output_placement import OutputPlacement


def _product(tmp_path, data=b"PRODUCT"):
    work = tmp_path / ".work"
    work.mkdir()
    produced = work / "Game.out"
    produced.write_bytes(data)
    return produced


def test_place_moves_the_product_and_names_it(tmp_path):
    produced = _product(tmp_path)
    out = tmp_path / "Game.out"
    placement = OutputPlacement(str(out), label="ZAR")

    placement.place(str(produced))

    assert out.read_bytes() == b"PRODUCT" and not produced.exists()
    # Completed: the placed file is this job's, so abandoning hands it back.
    assert placement.abandon() is not None


def test_place_refuses_an_existing_file_with_the_formats_label(tmp_path):
    produced = _product(tmp_path)
    out = tmp_path / "Game.out"
    out.write_bytes(b"PRECIOUS")
    placement = OutputPlacement(str(out), label="XISO")

    with pytest.raises(RuntimeError, match="while the XISO was being written; not overwriting it"):
        placement.place(str(produced))

    assert out.read_bytes() == b"PRECIOUS" and produced.exists()
    assert placement.abandon() is None  # nothing at the output is this job's


@pytest.mark.asyncio
async def test_abandon_after_placement_sweeps_the_product(tmp_path):
    produced = _product(tmp_path)
    out = tmp_path / "Game.out"
    placement = OutputPlacement(str(out), label="ZAR")
    placement.place(str(produced))

    await placement.abandon_and_sweep()

    assert not out.exists()


@pytest.mark.asyncio
async def test_abandon_after_placement_never_sweeps_a_replacement(tmp_path):
    produced = _product(tmp_path)
    out = tmp_path / "Game.out"
    placement = OutputPlacement(str(out), label="ZAR")
    placement.place(str(produced))
    out.unlink()
    out.write_bytes(b"SOMEONE ELSE'S")

    await placement.abandon_and_sweep()

    assert out.read_bytes() == b"SOMEONE ELSE'S"


@pytest.mark.asyncio
async def test_a_placement_that_lands_after_the_job_gave_up_removes_itself(
    tmp_path, monkeypatch,
):
    """The cancellation path: the move is stuck when the job gives up, so the
    sweep leaves the destination alone, and the move, when it lands, removes
    its own product."""
    produced = _product(tmp_path)
    out = tmp_path / "Game.out"
    placement = OutputPlacement(str(out), label="ZAR")
    started, release = threading.Event(), threading.Event()
    real_link = os.link

    def slow_link(src, dst):
        started.set()
        release.wait(10)
        real_link(src, dst)

    monkeypatch.setattr(os, "link", slow_link)
    worker = asyncio.create_task(asyncio.to_thread(placement.place, str(produced)))
    assert await asyncio.to_thread(started.wait, 5)

    await placement.abandon_and_sweep()  # the job gives up mid-move
    assert not out.exists()
    release.set()
    await worker

    assert not out.exists()
