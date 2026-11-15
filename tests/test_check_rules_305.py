"""Check Rules (#305): a Rule with no target format checks Images instead.

A check Rule picks the stalest Images of a Platform (never checked first, then
the oldest last check) within a nightly byte budget, and checks them through the
tool's own Verify. A failure becomes a "not Intact" Finding, a later pass
clears it, and a sweep with failures sends one "check failed" notification.

Only the verifier is stubbed: it is the one external binary a check runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from services import db as _db
from services import platform_detect
from services.tools import registry

from config import settings as app_settings

GB = 1024 ** 3


# ----------------------------------------------------------------------
# the schema
# ----------------------------------------------------------------------


def test_a_rule_with_no_target_format_is_a_check_rule() -> None:
    from services.rules.rules import is_check_rule, normalize_rule

    rule = normalize_rule({
        "enabled": True, "check_budget_gb": 50,
        "window_start": "01:00", "window_end": "05:00", "days": [0, 2],
    })

    assert rule is not None
    assert is_check_rule(rule)
    assert rule["mode"] == ""
    assert rule["check_budget_gb"] == 50
    assert (rule["window_start"], rule["window_end"], rule["days"]) == ("01:00", "05:00", [0, 2])
    # Nightly: a check Rule runs at most once a day, inside its window.
    assert rule["interval_minutes"] == 24 * 60
    # It never converts, so it never deletes.
    assert rule["delete_on_verify"] is False


def test_a_check_rule_budget_is_bounded() -> None:
    from services.rules.rules import normalize_rule

    assert normalize_rule({"check_budget_gb": 0})["check_budget_gb"] == 1
    assert normalize_rule({"check_budget_gb": "lots"})["check_budget_gb"] == 50
    assert normalize_rule({})["check_budget_gb"] == 50


def test_a_conversion_rule_is_not_a_check_rule() -> None:
    from services.rules.rules import is_check_rule, normalize_rule

    rule = normalize_rule({"mode": "createcd", "enabled": True})
    assert not is_check_rule(rule)
