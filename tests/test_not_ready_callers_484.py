"""The Inbox and the rom-converto Info route give the tool's own reason (#484).

Same bug as the sweep's skip line: the Inbox held a file under a Rule whose
tool wasn't ready with ``cso isn't ready, so the Rule can't run``, naming the
tool id and dropping the plugin's reason, and the ZAR Info route built its own
``rom-converto isn't ready: ...``. Both now say it through
:func:`services.tools.not_ready_message`, read after the readiness check.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from services import findings
from services.tools import not_ready_message, registry

from .test_inbox_431 import (
    _db_fixture,
    _drop,
    _inbox,
    _inbox_findings,
    _library,
    _poll,
    _ps2_rule,
    _queue,
    _vol,
)

__all__ = ["_db_fixture", "_queue", "_vol"]


@pytest.mark.asyncio
async def test_an_inbox_rule_whose_tool_is_not_ready_holds_with_its_reason(
    vol, db, queue, monkeypatch,
):
    cso = registry.get("cso")
    probed: list[str] = []

    async def not_ready(tool, **_kwargs):
        # The reason is set by the probe, as rom-converto's is: read after it.
        probed.append(tool.id)
        monkeypatch.setattr(tool, "not_ready_reason", "maxcso said no", raising=False)
        return False

    monkeypatch.setattr(registry, "tool_is_ready", not_ready)
    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=61)

    assert "cso" in probed
    assert queue.submitted == []
    assert game.exists()
    [finding] = _inbox_findings()
    assert finding["kind"] in findings.INBOX_KINDS
    assert finding["reason"].startswith("CSO isn't ready: maxcso said no"), finding["reason"]
    assert not_ready_message(cso) in finding["reason"]
    assert "cso isn't ready" not in finding["reason"]


@pytest.mark.asyncio
async def test_the_zar_info_route_says_rom_convertos_own_reason(vol, monkeypatch):
    from routes import info

    zar = vol / "Game.zar"
    zar.write_bytes(b"\0" * 64)
    tool = registry.get("rom_converto")

    async def not_ready(probed, **_kwargs):
        monkeypatch.setattr(probed, "_not_ready_reason", "the binary did not run")
        return False

    monkeypatch.setattr(registry, "tool_is_ready", not_ready)

    with pytest.raises(HTTPException) as caught:
        await info.get_rom_converto_info(path=str(zar))

    assert caught.value.status_code == 503
    assert caught.value.detail == "rom-converto isn't ready: the binary did not run"
    assert caught.value.detail == not_ready_message(tool)
