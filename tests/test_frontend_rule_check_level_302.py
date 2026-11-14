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
_AUTOMATION = _SRC / "lib" / "components" / "views" / "RuleEditor.svelte"
_NOTICES = _SRC / "lib" / "stores" / "ruleNotices.svelte.js"
_ENDPOINTS = _SRC / "lib" / "api" / "endpoints.js"
_LEVELS = _SRC / "lib" / "util" / "checkLevels.js"


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
    assert "rule.check_level === CHECK_LEVEL.containerOnly" in body, body
    assert (
        "check_level: v ? CHECK_LEVEL.containerOnly : CHECK_LEVEL.roundTrip" in body
    ), body
    # The description follows the box: no Round-trip promise once it is ticked.
    assert (
        "spec?.supportsRoundTrip && rule.check_level !== CHECK_LEVEL.containerOnly" in body
    ), body


def test_the_editor_names_no_level_string_itself():
    """One JS copy of the two levels, held to the backend's by the test below."""
    src = _AUTOMATION.read_text(encoding="utf-8")
    assert "'round_trip'" not in src and "'container_only'" not in src
    assert "from '$lib/util/checkLevels.js'" in src


def test_the_js_levels_match_the_backend():
    from services.rules.rules import CHECK_LEVELS

    js = _LEVELS.read_text(encoding="utf-8")
    found = re.search(
        r"roundTrip: '([a-z_]+)',\s*containerOnly: '([a-z_]+)'", js,
    )
    assert found, js
    assert found.groups() == CHECK_LEVELS


def test_the_notice_says_which_rules_keep_the_container_check():
    src = _AUTOMATION.read_text(encoding="utf-8")
    assert "ruleNotices.checkLevel.container_check" in src


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
