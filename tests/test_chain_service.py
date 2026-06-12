"""Orchestration tests for ``ChainTool`` (the cso_to_chd pipeline seam).

The real maxcso/chdman CLIs can't run here, so the component steps are mocked:
``registry.for_mode("cso_decompress")`` and ``registry.for_mode("createdvd")``
resolve to the cso/chdman plugins, whose ``convert`` is monkeypatched. These
tests assert the chain's own behavior — step delegation, weighted progress,
intermediate placement/cleanup, compression routing, cancel propagation, and the
disk-headroom preflight — independent of the underlying tools.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import app.services.tools.chain as chain_mod
from app.services.disk import InsufficientDiskSpace
from app.services.tools import registry


class _Cancelled(Exception):
    """Stand-in for a sub-tool's cancellation signal."""


def _drain(agen) -> list[dict]:
    async def _run() -> list[dict]:
        return [u async for u in agen]

    return asyncio.run(_run())


@pytest.fixture
def chain_env(monkeypatch, tmp_path):
    """Patch the chain's temp dir + disc-ID embed, and install fake steps."""
    work = tmp_path / "chainwork"

    def _scratch(prefix=""):
        work.mkdir(parents=True, exist_ok=True)
        return str(work)

    monkeypatch.setattr(chain_mod, "create_scratch_dir", _scratch)
    # No real disc parsing/tagging: keep the embed a no-op.
    monkeypatch.setattr(chain_mod, "extract_from_source", lambda p: None)

    calls: list[dict] = []

    def install(cso_progress=(0, 100), chd_progress=(0, 100), cso_cancel=False):
        def make(tool_id, progress, cancel_raises):
            def _convert(input_path, output_path, mode, *,
                         compression=None, cancel_event=None):
                async def _gen():
                    calls.append({
                        "tool": tool_id, "mode": mode, "in": input_path,
                        "out": output_path, "compression": compression,
                    })
                    if cancel_raises and cancel_event is not None \
                            and cancel_event.is_set():
                        raise _Cancelled()
                    for p in progress:
                        yield {"progress": p, "message": f"{mode}:{p}"}
                    Path(output_path).write_bytes(b"0" * 32)

                return _gen()

            return _convert

        monkeypatch.setattr(
            registry.get("cso"), "convert", make("cso", cso_progress, cso_cancel),
        )
        monkeypatch.setattr(
            registry.get("chdman"), "convert", make("chdman", chd_progress, False),
        )

    return calls, work, install


def test_chain_orchestration_progress_and_intermediate(chain_env, tmp_path):
    calls, work, install = chain_env
    install(cso_progress=(0, 50, 100), chd_progress=(0, 50, 100))
    src = tmp_path / "Game.cso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Game.chd"

    updates = _drain(
        registry.for_mode("cso_to_chd").convert(
            str(src), str(out), "cso_to_chd", compression="zstd",
        )
    )

    # Step delegation + ordering.
    assert [c["mode"] for c in calls] == ["cso_decompress", "createdvd"]
    # Step 1 reads the source; step 2 reads the intermediate .iso in the work dir.
    assert calls[0]["in"] == str(src)
    assert calls[0]["out"].endswith(".iso")
    assert os.path.dirname(calls[0]["out"]) == str(work)
    assert calls[1]["in"] == calls[0]["out"]
    assert calls[1]["out"] == str(out)
    # cso_to_chd advertises no compression, so a client-supplied preset is
    # dropped (not smuggled to chdman as a codec): neither step receives it.
    assert calls[0]["compression"] is None
    assert calls[1]["compression"] is None

    # Aggregate progress is monotonic and ends at 100.
    progs = [u["progress"] for u in updates]
    assert progs == sorted(progs)
    assert progs[-1] == 100
    # The 0.20-weighted cso step tops out at ~20% of the bar before chd starts.
    assert max(u["progress"] for u in updates if u["message"].startswith("[1/2]")) == 20
    assert any(u["message"].startswith("[2/2]") for u in updates)

    # Final output produced; intermediate + work dir cleaned up.
    assert out.exists()
    assert not work.exists()


def test_chain_propagates_cancel_and_cleans_up(chain_env, tmp_path):
    _calls, work, install = chain_env
    install(cso_cancel=True)
    src = tmp_path / "Game.cso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Game.chd"
    event = asyncio.Event()
    event.set()

    with pytest.raises(_Cancelled):
        _drain(
            registry.for_mode("cso_to_chd").convert(
                str(src), str(out), "cso_to_chd", cancel_event=event,
            )
        )

    # Temp work dir removed even though the job aborted; no partial final.
    assert not work.exists()
    assert not out.exists()


def test_uncompressed_iso_size_reads_container_headers(tmp_path):
    import struct

    from app.services.maxcso import uncompressed_iso_size

    cso = tmp_path / "g.cso"
    cso.write_bytes(b"CISO" + struct.pack("<I", 24) + struct.pack("<Q", 5_000_000)
                    + struct.pack("<I", 2048))
    assert uncompressed_iso_size(str(cso)) == 5_000_000

    zso = tmp_path / "g.zso"
    zso.write_bytes(b"ZISO" + struct.pack("<I", 24) + struct.pack("<Q", 7_000_000)
                    + struct.pack("<I", 2048))
    assert uncompressed_iso_size(str(zso)) == 7_000_000

    dax = tmp_path / "g.dax"
    dax.write_bytes(b"DAX\x00" + struct.pack("<I", 3_000_000) + b"\x00" * 8)
    assert uncompressed_iso_size(str(dax)) == 3_000_000

    # Unknown header -> None (caller falls back to the ratio estimate).
    other = tmp_path / "g.bin"
    other.write_bytes(b"\x00" * 16)
    assert uncompressed_iso_size(str(other)) is None


def test_chain_headroom_blocks_before_any_step(chain_env, tmp_path, monkeypatch):
    calls, work, install = chain_env
    install()

    def _boom(targets, *, margin_bytes=0):
        raise InsufficientDiskSpace("not enough space")

    monkeypatch.setattr(chain_mod, "ensure_headroom", _boom)

    src = tmp_path / "Game.cso"
    src.write_bytes(b"x" * 1000)
    out = tmp_path / "Game.chd"

    with pytest.raises(InsufficientDiskSpace):
        _drain(
            registry.for_mode("cso_to_chd").convert(str(src), str(out), "cso_to_chd")
        )

    # Preflight runs before step 1: no steps executed, work dir cleaned up.
    assert calls == []
    assert not work.exists()
