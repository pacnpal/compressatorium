"""A name filter is checked by the server before the console table saves it
(#541, Codacy on PR #547): the same compile ``normalize_rule`` runs, so a
pattern Python's ``re`` refuses is never autosaved and nulled, and the
browser never builds a RegExp from what was typed."""

from __future__ import annotations

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern, ok", [
    (r"\(USA\)", True),
    ("", True),
    ("(", False),
    # Python's grammar, not JS's: a named group `(?P<x>...)` is fine here.
    (r"(?P<region>USA)", True),
    # Too slow to run unattended: refused like normalize_rule refuses it.
    ("(a+)+b", False),
    ("x" * 501, False),
])
async def test_the_server_checks_a_name_filter(pattern, ok) -> None:
    from routes import rules as routes
    from services.rules.rules import normalize_rule

    answer = await routes.check_pattern({"pattern": pattern})

    assert answer["ok"] is ok, answer
    assert (answer["error"] is None) is ok, answer
    # The same verdict a save would get.
    saved = normalize_rule({"mode": "createcd", "include_pattern": pattern})
    assert bool(saved.get("invalid_pattern")) is (not ok)


@pytest.mark.asyncio
async def test_a_pattern_too_long_says_so() -> None:
    from routes import rules as routes

    answer = await routes.check_pattern({"pattern": "x" * 501})

    assert "500" in answer["error"]


def test_the_browser_never_builds_a_regexp_from_what_was_typed() -> None:
    for rel in ("rules/ruleTable.js", "components/library/RuleAdvanced.svelte"):
        src = (_SRC / rel).read_text(encoding="utf-8")
        assert "new RegExp(" not in src, rel
    advanced = (_SRC / "components" / "library" / "RuleAdvanced.svelte").read_text(encoding="utf-8")
    assert "api.checkRulePattern(" in advanced


def test_a_check_answered_after_the_panel_closed_changes_nothing() -> None:
    """A reply landing after the panel is destroyed must not mark the pattern
    unsaved again, which would block Run, Preview and automation (codex
    4653575620)."""
    src = (_SRC / "components" / "library" / "RuleAdvanced.svelte").read_text(encoding="utf-8")
    after_answer = src.split("await api.checkRulePattern(text)", 1)[1]
    guard = after_answer.split("source.markUnsaved", 1)[0]
    assert "if (destroyed || drafts[field] !== text) return;" in guard
    destroy = src.split("onDestroy(() => {", 1)[1].split("});", 1)[0]
    assert "destroyed = true;" in destroy
