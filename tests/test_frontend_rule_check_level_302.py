"""Wiring guards for the Rule check level in the editor (#302).

The backend decides what a check level does (tests/test_rules_check_level_302.py).
These check that the editor offers it next to delete-source, that Preview
shows it, and that the one-time notice reads and dismisses through the
library-agnostic notice routes.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_AUTOMATION = _SRC / "lib" / "components" / "views" / "RommAutomation.svelte"
_NOTICES = _SRC / "lib" / "stores" / "ruleNotices.svelte.js"
_ENDPOINTS = _SRC / "lib" / "api" / "endpoints.js"


def test_the_check_level_sits_next_to_delete_source():
    src = _AUTOMATION.read_text(encoding="utf-8")
    block = re.search(
        r"\{#if spec\?\.supportsDeleteOnVerify\}(.*?)\n                  \{/if\}\n",
        src, re.DOTALL,
    )
    assert block, "the delete-source block is no longer where this guard looks"
    body = block.group(1)
    assert "delete_on_verify: v" in body
    # Offered only where it decides something, as in the convert panel.
    assert "rule.delete_on_verify && spec?.supportsRoundTrip" in body, body
    assert "rule.check_level === 'container_only'" in body, body
    assert "check_level: v ? 'container_only' : 'round_trip'" in body, body


def test_preview_shows_the_check_level():
    src = _AUTOMATION.read_text(encoding="utf-8")
    assert "p.check_level" in src
    assert "CHECK_LEVEL_LABELS" in src


def test_the_notice_reads_and_dismisses_through_the_rules_routes():
    endpoints = _ENDPOINTS.read_text(encoding="utf-8")
    assert "/rules/notices`" in endpoints
    assert "/rules/notices/check-level/dismiss`" in endpoints

    store = _NOTICES.read_text(encoding="utf-8")
    assert "api.getRuleNotices()" in store
    assert "api.dismissCheckLevelNotice()" in store

    src = _AUTOMATION.read_text(encoding="utf-8")
    assert "ruleNotices.load()" in src
    assert re.search(r"\{#if ruleNotices\.checkLevel\?\.show\}", src)
    assert "ruleNotices.dismissCheckLevel()" in src
