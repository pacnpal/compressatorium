"""Each chdman create mode offers and accepts only the codecs it can use (#471).

chdman parses any codec it knows (``parse_compression`` in MAME's
src/tools/chdman.cpp), but a CD codec (cdzl, cdzs, cdlz, cdfl) refuses a hunk
that is not whole CD frames (``chd_cd_compressor``: ``hunkbytes %
cdrom_file::FRAME_SIZE`` is a CODEC_ERROR), and only createcd writes CD frames:
createdvd (``-hs 2048``), createhd and createraw (``-us 512``) write 4096-byte
or 2048-byte hunks. avhu reads the A/V metadata only createld writes. A name
chdman does not know (``lzma2``) is "Invalid compressor" for every mode. So
each create mode declares its codecs on its ``ModeSpec``, and the shared
``compression_problem`` refuses the rest before a job exists.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.tools import registry
from services.tools.base import compression_problem

_GENERAL = {"none", "zlib", "zstd", "lzma", "huff", "flac"}
_CD = {"cdzl", "cdzs", "cdlz", "cdfl"}
_EXPECTED = {
    "createraw": _GENERAL,
    "createhd": _GENERAL,
    "createdvd": _GENERAL,
    "createcd": _GENERAL | _CD,
    "createld": _GENERAL | {"avhu"},
}


@pytest.mark.parametrize("mode", sorted(_EXPECTED))
def test_each_create_mode_declares_the_codecs_chdman_takes_for_it(mode) -> None:
    assert registry.spec(mode).codecs == frozenset(_EXPECTED[mode])


def test_every_implicit_codec_is_one_the_mode_takes() -> None:
    """What the tool uses when nothing is chosen is a codec the mode declares,
    for every mode of every tool that names its codecs."""
    for spec in registry.mode_specs():
        if spec.codecs and spec.implicit_codecs:
            assert set(spec.implicit_codecs) <= spec.codecs, spec.mode


def test_the_tool_default_codec_is_one_every_create_mode_takes() -> None:
    default = registry.get("chdman").default_compression
    for mode in _EXPECTED:
        assert compression_problem(registry.spec(mode), default) is None, mode


@pytest.mark.parametrize(("mode", "compression"), [
    ("createdvd", "cdzl"),
    ("createdvd", "zlib,cdlz"),
    ("createhd", "cdfl"),
    ("createraw", "cdzs"),
    ("createdvd", "avhu"),
    ("createcd", "avhu"),
    ("createcd", "lzma2"),
])
def test_a_codec_the_mode_cannot_use_is_refused(mode, compression) -> None:
    problem = compression_problem(registry.spec(mode), compression)
    assert problem is not None
    assert registry.spec(mode).label in problem


@pytest.mark.parametrize(("mode", "compression"), [
    ("createdvd", "zlib"),
    ("createdvd", "lzma,zlib,huff,flac"),
    ("createhd", "zstd"),
    ("createraw", "none"),
    ("createcd", "cdlz,cdzl,cdfl"),
    ("createcd", "cdzs,zlib"),
    ("createld", "avhu"),
    ("createld", "zlib"),
    ("copy", "cdzl"),
])
def test_a_codec_the_mode_takes_is_accepted(mode, compression) -> None:
    assert compression_problem(registry.spec(mode), compression) is None


@pytest.fixture(name="queue")
def _queue(tmp_path, monkeypatch):
    """Routes confined to ``tmp_path``; ``job_manager.create_job`` records."""
    from app.routes import convert as convert_routes

    for name, value in (
        ("chd_volumes", str(tmp_path)), ("data_mount_root", str(tmp_path)),
        ("max_queue_depth", 0),
    ):
        monkeypatch.setattr(convert_routes.settings, name, value)
    created: list[dict] = []

    async def record(file_path, mode, **kwargs):
        created.append({"mode": mode.value, "compression": kwargs.get("compression")})
        return SimpleNamespace(id="job")

    monkeypatch.setattr(convert_routes.job_manager, "create_job", record)
    return {"tmp": tmp_path, "created": created}


def _iso(tmp_path, name="Disc.iso") -> str:
    path = tmp_path / name
    path.write_bytes(b"\0" * 4096)
    return str(path)


async def _post(queue, mode: str, compression: str):
    from app.models import ConversionMode, JobCreateRequest
    from app.routes import convert as convert_routes

    return await convert_routes.create_job(JobCreateRequest(
        file_path=_iso(queue["tmp"]), mode=ConversionMode(mode),
        compression=compression, output_dir=str(queue["tmp"] / "out"),
    ))


@pytest.mark.asyncio
async def test_post_jobs_refuses_createdvd_with_a_cd_codec_before_a_job_exists(queue) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        await _post(queue, "createdvd", "cdzl")

    assert refused.value.status_code == 400
    assert "Create DVD can't use cdzl" in refused.value.detail
    assert queue["created"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "compression"), [
    ("createdvd", "zlib"),
    ("createcd", "cdlz,cdzl,cdfl"),
    ("createld", "avhu"),
])
async def test_post_jobs_queues_a_codec_the_mode_takes(queue, mode, compression) -> None:
    await _post(queue, mode, compression)

    assert queue["created"] == [{"mode": mode, "compression": compression}]


def test_an_auto_target_with_a_saved_cd_codec_falls_back_to_the_tool_default() -> None:
    """A saved chdman preference of CD codecs, on a PS2 .iso that Auto sends
    to createdvd, runs on chdman's own codecs instead of failing in chdman."""
    from app.routes.convert import checked_compression

    assert checked_compression("createdvd", "cdlz,cdzl,cdfl") is None
    assert checked_compression("createcd", "cdlz,cdzl,cdfl") == "cdlz,cdzl,cdfl"
