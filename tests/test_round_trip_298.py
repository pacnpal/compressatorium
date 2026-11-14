"""Round-trip check, first tool: Dolphin (#298).

A Dolphin job with delete-source on deletes its Source only when the content
the output decodes to equals the content the Source decodes to. dolphin-tool is
faked at the ``SubprocessRunner.run_capture`` seam, so these tests need no real
binary; the Source ISO side is a real SHA1 of a real temp file.
"""
from __future__ import annotations

import hashlib

import pytest
from services.tools import registry
from services.tools.base import ContentDigest, ContentDigestUnavailable

DOLPHIN_MODES = ("dolphin_rvz", "dolphin_wia", "dolphin_gcz", "dolphin_iso")


def _dolphin():
    return registry.get("dolphin")


def _fake_capture(monkeypatch, *, rc=0, stdout=b"", calls=None):
    """Replace dolphin-tool's one-shot capture; record each command."""
    service = _dolphin()._service

    async def fake(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        return rc, stdout, b""

    monkeypatch.setattr(service._runner, "run_capture", fake)


@pytest.mark.asyncio
async def test_iso_digest_is_the_file_sha1_without_spawning(tmp_path, monkeypatch):
    calls: list = []
    _fake_capture(monkeypatch, calls=calls)
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"disc bytes" * 1000)

    digest = await _dolphin().content_digest(str(iso))

    assert digest == ContentDigest(
        kind="gc_wii_disc_sha1",
        value=hashlib.sha1(iso.read_bytes()).hexdigest(),
    )
    assert calls == []


@pytest.mark.asyncio
async def test_container_digest_comes_from_dolphin_tool_verify(tmp_path, monkeypatch):
    calls: list = []
    sha1 = "ab" * 20
    _fake_capture(monkeypatch, stdout=f"SHA-1: {sha1.upper()}\n".encode(), calls=calls)
    rvz = tmp_path / "Game.rvz"
    rvz.write_bytes(b"x")

    digest = await _dolphin().content_digest(str(rvz))

    assert digest == ContentDigest(kind="gc_wii_disc_sha1", value=sha1)
    assert len(calls) == 1
    assert calls[0][-4:] == ["-i", str(rvz), "--algorithm", "sha1"]


@pytest.mark.asyncio
async def test_failed_dolphin_tool_verify_is_unavailable(tmp_path, monkeypatch):
    _fake_capture(monkeypatch, rc=1)
    rvz = tmp_path / "Game.rvz"
    rvz.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _dolphin().content_digest(str(rvz))


@pytest.mark.asyncio
async def test_a_file_dolphin_does_not_read_has_no_digest(tmp_path):
    other = tmp_path / "Game.chd"
    other.write_bytes(b"x")
    assert await _dolphin().content_digest(str(other)) is None


def test_every_dolphin_mode_declares_round_trip():
    for mode in DOLPHIN_MODES:
        assert registry.mode_supports_round_trip(mode), mode


def test_modes_without_a_content_digest_do_not_declare_round_trip():
    for spec in registry.mode_specs():
        if spec.tool_id != "dolphin":
            assert not registry.mode_supports_round_trip(spec.mode), spec.mode


def test_digests_of_different_kinds_are_never_equal():
    a = ContentDigest(kind="gc_wii_disc_sha1", value="ab" * 20)
    b = ContentDigest(kind="cd_track_sha1", value="ab" * 20)
    with pytest.raises(ValueError):
        a.same_content(b)
    assert a.same_content(ContentDigest(kind="gc_wii_disc_sha1", value="ab" * 20))
    assert not a.same_content(ContentDigest(kind="gc_wii_disc_sha1", value="cd" * 20))
