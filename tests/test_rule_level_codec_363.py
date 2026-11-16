"""A compression level only for a codec that takes one (#363).

The codec a job runs with is its own or, when blank, the mode's default
(``ModeSpec.default_compression``). Dolphin's ``none`` takes no level, and WIA's
default is ``none``, so a WIA Rule on "Tool default" with level 15 used to be
stored and sent as ``":15"``. One rule, ``ModeSpec.takes_no_level``, decides it:
the job route refuses such a level with 400 (as #347 refuses an out-of-range
one), and the Rule sanitizer drops it from what it stores and runs.
"""
from __future__ import annotations

import pytest
from services.tools import registry

from .test_default_compression_345 import _env
from .test_platform_detect import _gamecube

pytestmark = pytest.mark.usefixtures("fresh_detect_cache")
# #345's "env" fixture (a Volume, an empty queue, nothing saved), shared here.
ENV_FIXTURE = _env


@pytest.mark.parametrize(("mode", "codec", "refuses"), [
    ("dolphin_wia", None, True),      # WIA's default is none
    ("dolphin_wia", "none", True),
    ("dolphin_rvz", "none", True),
    ("dolphin_wia", "lzma2", False),
    ("dolphin_rvz", None, False),     # RVZ's default is zstd
    ("dolphin_rvz", "lzma2", False),
    ("nsz_compress", None, False),    # no per-codec levels: the tool's range
])
def test_which_codec_takes_no_level(mode, codec, refuses) -> None:
    assert registry.spec(mode).takes_no_level(codec) is refuses


def test_a_wia_rule_on_tool_default_stores_and_runs_no_level() -> None:
    from services.rules.rules import normalize_rule
    from services.rules.sweep import _compression_arg

    rule = normalize_rule({
        "mode": "dolphin_wia", "compression": None, "compression_level": 15,
    })

    assert rule["compression_level"] is None
    assert _compression_arg(rule) is None


@pytest.mark.parametrize(("mode", "compression", "level"), [
    ("dolphin_wia", "none", 15),
    ("dolphin_rvz", None, 15),
    ("dolphin_rvz", "lzma2", 7),
])
def test_a_rule_keeps_a_level_its_codec_takes(mode, compression, level) -> None:
    from services.rules.rules import normalize_rule

    rule = normalize_rule({"mode": mode, "compression": compression, "compression_level": level})

    assert rule["compression_level"] == (None if compression == "none" else level)


@pytest.mark.asyncio
@pytest.mark.parametrize("compression", [":15", "none:15"])
async def test_a_job_level_for_a_codec_that_takes_none_is_refused(env, compression) -> None:
    from app.models import ConversionMode, JobCreateRequest
    from app.routes import convert as convert_routes
    from fastapi import HTTPException

    iso = _gamecube(env["tmp"] / "gc" / "Melee.iso")
    with pytest.raises(HTTPException) as refused:
        await convert_routes.create_job(JobCreateRequest(
            file_path=iso, mode=ConversionMode.DOLPHIN_WIA, compression=compression,
        ))

    # ":15" never reaches the codec checks: the API's token check refuses a
    # level with no codec first. "none:15" is refused for what it is.
    assert refused.value.status_code == 400
    if compression == "none:15":
        assert refused.value.detail == "Dolphin WIA none compression takes no level"


@pytest.mark.parametrize(("compression", "resolved"), [
    (":15", "none"), ("none:15", "none"), ("lzma2:7", "lzma2:7"),
])
def test_a_stored_job_runs_without_a_level_its_codec_refuses(compression, resolved) -> None:
    """A job queued before this fix (the CLI, a Rule's ":15") resolves to what
    dolphin-tool runs, so it still converts instead of failing on refusal."""
    assert registry.for_mode("dolphin_wia").compression_for("dolphin_wia", compression) == resolved
