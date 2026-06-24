"""Tests for the unified ``post_convert`` disc-ID embed seam (#181).

``ChdmanTool.post_convert`` is now the single home for conversion-time disc-ID
tagging. A direct ``createcd`` / ``createdvd`` job reaches it through
``job_manager._process_job`` (``registry.for_mode(mode).post_convert(...)``) and
a ``cso_to_chd`` chain reaches it through ``ChainTool``'s final step, replacing
the two copy-pasted embed blocks that used to live in those call sites.

These tests exercise the hook directly (the real chdman CLI / disc parsing can't
run here, so ``extract_from_source`` / ``read_embedded_game_id`` / ``embed_in_chd``
are monkeypatched). The chain's *routing* into the hook is asserted separately in
``test_chain_service.py::test_chain_tags_final_chd_via_post_convert``.
"""
from __future__ import annotations

import asyncio

import pytest

import app.services.tools.chdman as chdman_mod
from app.services.tools import registry


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def chd_out(tmp_path):
    """A stand-in output CHD on disk (presence is all post_convert checks)."""
    out = tmp_path / "Game.chd"
    out.write_bytes(b"MComprHD")
    return out


@pytest.fixture
def patch_embed(monkeypatch):
    """Install recording stubs for the three disc_id helpers post_convert uses.

    Returns ``(set_source, set_existing, embedded)`` where ``embedded`` is a
    list each successful ``embed_in_chd`` appends ``(path, game_id, title)`` to.
    """
    state: dict = {"source": None, "existing": None}
    embedded: list[tuple] = []

    def _extract(path):  # sync: called via run_in_threadpool
        return state["source"]

    async def _read_existing(path, chdman_path):
        return state["existing"]

    async def _embed(path, game_id, title, chdman_path):
        embedded.append((path, game_id, title))
        return True

    monkeypatch.setattr(chdman_mod, "extract_from_source", _extract)
    monkeypatch.setattr(chdman_mod, "read_embedded_game_id", _read_existing)
    monkeypatch.setattr(chdman_mod, "embed_in_chd", _embed)

    def set_source(info):
        state["source"] = info

    def set_existing(game_id):
        state["existing"] = game_id

    return set_source, set_existing, embedded


@pytest.mark.parametrize("mode", ["createcd", "createdvd"])
def test_post_convert_embeds_disc_id_for_disc_modes(
    mode, tmp_path, chd_out, patch_embed,
):
    set_source, _set_existing, embedded = patch_embed
    set_source({"game_id": "SLUS-20312", "title": "Demo Disc"})

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), mode))

    assert embedded == [(str(chd_out), "SLUS-20312", "Demo Disc")]


def test_post_convert_falls_back_to_serial_as_title(tmp_path, chd_out, patch_embed):
    set_source, _set_existing, embedded = patch_embed
    set_source({"game_id": "SLUS-20312"})  # no title key

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    # NAME falls back to the serial when no human title is available.
    assert embedded == [(str(chd_out), "SLUS-20312", "SLUS-20312")]


@pytest.mark.parametrize(
    "mode", ["createraw", "createhd", "createld", "extractcd", "extractdvd", "copy"],
)
def test_post_convert_noop_for_non_disc_modes(
    mode, tmp_path, chd_out, patch_embed, monkeypatch,
):
    set_source, _set_existing, embedded = patch_embed
    set_source({"game_id": "SLUS-20312"})

    extracted: list[str] = []
    real_extract = chdman_mod.extract_from_source
    monkeypatch.setattr(
        chdman_mod, "extract_from_source",
        lambda p: extracted.append(p) or real_extract(p),
    )

    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), mode))

    # Non-CD/DVD modes carry no disc serial: the source is never even read.
    assert extracted == []
    assert embedded == []


def test_post_convert_noop_when_source_has_no_disc_id(tmp_path, chd_out, patch_embed):
    set_source, _set_existing, embedded = patch_embed
    set_source(None)  # extract_from_source found nothing

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    assert embedded == []


def test_post_convert_idempotent_skips_matching_tag(tmp_path, chd_out, patch_embed):
    set_source, set_existing, embedded = patch_embed
    set_source({"game_id": "SLUS-20312", "title": "Demo"})
    set_existing("SLUS-20312")  # CHD already carries this exact serial

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    # Idempotent: no duplicate GAME tag appended.
    assert embedded == []


def test_post_convert_reembeds_when_tag_differs(
    tmp_path, chd_out, patch_embed, monkeypatch,
):
    set_source, set_existing, _embedded = patch_embed
    set_source({"game_id": "SLUS-20312", "title": "Demo"})
    set_existing("SLES-00000")  # stale / wrong serial already on the CHD

    order: list[tuple] = []

    async def _clear(path, chdman_path):
        order.append(("clear", path))

    async def _embed(path, game_id, title, chdman_path):
        order.append(("embed", path, game_id, title))
        return True

    monkeypatch.setattr(chdman_mod, "clear_embedded_disc_id", _clear)
    monkeypatch.setattr(chdman_mod, "embed_in_chd", _embed)

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    # chdman addmeta appends, so the stale GAME/NAME is stripped *before* the new
    # pair is written — otherwise the old serial persists and the embed never
    # converges (it would just accumulate duplicate GAME tags).
    assert order == [
        ("clear", str(chd_out)),
        ("embed", str(chd_out), "SLUS-20312", "Demo"),
    ]


def test_post_convert_fresh_chd_does_not_clear(
    tmp_path, chd_out, patch_embed, monkeypatch,
):
    set_source, set_existing, embedded = patch_embed
    set_source({"game_id": "SLUS-20312"})
    set_existing(None)  # freshly created CHD: no prior tag

    cleared: list[str] = []

    async def _clear(path, chdman_path):
        cleared.append(path)

    monkeypatch.setattr(chdman_mod, "clear_embedded_disc_id", _clear)

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    # The common path (no existing tag) pays no delmeta cost.
    assert cleared == []
    assert embedded == [(str(chd_out), "SLUS-20312", "SLUS-20312")]


def test_post_convert_missing_output_is_noop(tmp_path, patch_embed):
    set_source, _set_existing, embedded = patch_embed
    set_source({"game_id": "SLUS-20312"})

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    missing = tmp_path / "missing.chd"  # never created
    _run(registry.get("chdman").post_convert(str(src), str(missing), "createdvd"))

    assert embedded == []


def test_post_convert_swallows_embed_errors(tmp_path, chd_out, monkeypatch):
    """Tagging is best-effort: a failure in any helper never fails the job."""
    monkeypatch.setattr(
        chdman_mod, "extract_from_source",
        lambda p: {"game_id": "SLUS-20312"},
    )

    async def _boom(path, chdman_path):
        raise RuntimeError("chdman dumpmeta blew up")

    monkeypatch.setattr(chdman_mod, "read_embedded_game_id", _boom)

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    # Must not raise.
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))


def test_chain_tool_post_convert_is_noop(tmp_path):
    """ChainTool inherits the BaseTool no-op: when job_manager calls
    post_convert for a chain mode, it does nothing (the chain already tagged its
    output via the final step during the conversion itself)."""
    out = tmp_path / "Game.chd"
    out.write_bytes(b"MComprHD")
    src = tmp_path / "Game.cso"
    src.write_bytes(b"x")
    # No exception, returns None.
    assert _run(
        registry.for_mode("cso_to_chd").post_convert(
            str(src), str(out), "cso_to_chd",
        )
    ) is None
