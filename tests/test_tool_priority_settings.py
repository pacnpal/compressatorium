"""Tool-neutral process-priority / timeout settings (issue #132).

These knobs (nice level, I/O priority, info/verify timeouts) govern every
conversion tool's subprocess, not just chdman. They are exposed under
tool-neutral ``COMPRESSATORIUM_TOOL_*`` names while the chdman-era
``CHD_*``/``CHDMAN_*`` names remain backwards-compatible aliases, mirroring the
``chd_volumes`` -> ``COMPRESSATORIUM_VOLUMES`` pattern. The per-tool
overrides (``COMPRESSATORIUM_<TOOL>_*``) are retired (#542): each old name now
sets the shared value, and startup names it in one warning.
"""
from __future__ import annotations

import pytest

from app.config import Settings


def test_neutral_defaults_match_chdman_era_values():
    s = Settings()
    assert s.tool_nice == 10
    assert s.tool_ioprio_class == 2
    assert s.tool_ioprio_level == 6
    assert s.tool_info_timeout == 60
    # Verify is bounded out of the box (issue #266): a generous baseline plus a
    # per-GiB allowance, capped. It used to default to 0 -- no bound at all --
    # so a verify that never returned froze the whole queue behind it.
    assert s.tool_verify_timeout == 1800
    assert s.tool_verify_timeout_per_gib == 600
    assert s.tool_verify_timeout_cap == 86400
    assert s.tool_verify_progress_timeout == 600


def test_legacy_chd_env_names_still_populate_shared_settings():
    """The old CHD_*/CHDMAN_* env names keep working unchanged."""
    s = Settings(
        CHD_CHDMAN_NICE=5,
        CHD_CHDMAN_IOPRIO_CLASS=3,
        CHD_CHDMAN_IOPRIO_LEVEL=7,
        CHD_INFO_TIMEOUT=99,
        CHD_VERIFY_TIMEOUT=42,
    )
    assert s.tool_nice == 5
    assert s.tool_ioprio_class == 3
    assert s.tool_ioprio_level == 7
    assert s.tool_info_timeout == 99
    assert s.tool_verify_timeout == 42


def test_neutral_env_names_populate_shared_settings():
    s = Settings(
        COMPRESSATORIUM_TOOL_NICE=4,
        COMPRESSATORIUM_TOOL_IOPRIO_CLASS=1,
        COMPRESSATORIUM_TOOL_IOPRIO_LEVEL=0,
        COMPRESSATORIUM_TOOL_INFO_TIMEOUT=30,
        COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT=15,
    )
    assert s.tool_nice == 4
    assert s.tool_ioprio_class == 1
    assert s.tool_ioprio_level == 0
    assert s.tool_info_timeout == 30
    assert s.tool_verify_timeout == 15


def test_neutral_name_takes_precedence_over_legacy_alias():
    """When both the new and old names are present, the new name wins."""
    s = Settings(COMPRESSATORIUM_TOOL_NICE=1, CHD_CHDMAN_NICE=9)
    assert s.tool_nice == 1

    s = Settings(COMPRESSATORIUM_TOOL_INFO_TIMEOUT=11, CHD_INFO_TIMEOUT=88)
    assert s.tool_info_timeout == 11

    s = Settings(COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT=22, CHD_VERIFY_TIMEOUT=77)
    assert s.tool_verify_timeout == 22


@pytest.mark.parametrize(
    ("env", "field"),
    [
        ("CHD_CHDMAN_NICE", "tool_nice"),
        ("CHD_CHDMAN_IOPRIO_CLASS", "tool_ioprio_class"),
        ("CHD_CHDMAN_IOPRIO_LEVEL", "tool_ioprio_level"),
        ("CHD_INFO_TIMEOUT", "tool_info_timeout"),
        ("CHD_VERIFY_TIMEOUT", "tool_verify_timeout"),
    ],
)
def test_each_legacy_alias_maps_to_its_neutral_field(env, field):
    s = Settings(**{env: 3})
    assert getattr(s, field) == 3


def test_per_tool_settings_are_gone():
    """One value for every tool (#542): no per-tool override fields remain."""
    s = Settings()
    for name in ("chdman_nice", "dolphin_tool_ioprio_class", "nsz_verify_timeout",
                 "rom_converto_info_timeout", "xiso_ioprio_level"):
        assert not hasattr(s, name)


def test_retired_per_tool_name_feeds_the_shared_setting(monkeypatch):
    """An old COMPRESSATORIUM_<TOOL>_* name keeps working: it sets the shared value."""
    monkeypatch.setenv("COMPRESSATORIUM_DOLPHIN_TOOL_NICE", "15")
    monkeypatch.setenv("COMPRESSATORIUM_XISO_VERIFY_TIMEOUT", "77")
    s = Settings()
    assert s.tool_nice == 15
    assert s.tool_verify_timeout == 77
    assert s.retired_env == [
        "COMPRESSATORIUM_DOLPHIN_TOOL_NICE", "COMPRESSATORIUM_XISO_VERIFY_TIMEOUT",
    ]


def test_retired_per_tool_names_that_disagree_take_the_largest(monkeypatch):
    monkeypatch.setenv("COMPRESSATORIUM_CHDMAN_NICE", "5")
    monkeypatch.setenv("COMPRESSATORIUM_NSZ_NICE", "12")
    assert Settings().tool_nice == 12


def test_shared_name_wins_over_a_retired_per_tool_name(monkeypatch):
    monkeypatch.setenv("COMPRESSATORIUM_TOOL_NICE", "3")
    monkeypatch.setenv("COMPRESSATORIUM_CHDMAN_NICE", "15")
    s = Settings()
    assert s.tool_nice == 3
    assert s.retired_env == ["COMPRESSATORIUM_CHDMAN_NICE"]


def test_unreadable_retired_value_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setenv("COMPRESSATORIUM_Z3DS_NICE", "lots")
    s = Settings()
    assert s.tool_nice == 10
    assert s.retired_env == ["COMPRESSATORIUM_Z3DS_NICE"]


def test_retired_names_no_longer_read_are_reported(monkeypatch):
    monkeypatch.setenv("CHD_DEBUG_PROGRESS_TIMEOUT", "1")
    monkeypatch.setenv("COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST", "false")
    s = Settings()
    assert s.retired_env == [
        "CHD_DEBUG_PROGRESS_TIMEOUT", "COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST",
    ]
    assert not hasattr(s, "debug_progress_timeout")
    assert not hasattr(s, "search_auto_return_to_file_list")


def test_no_retired_names_no_warning_list(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith(("COMPRESSATORIUM_", "CHD_DEBUG_", "CHD_SEARCH_")):
            monkeypatch.delenv(name)
    assert Settings().retired_env == []


def test_startup_logs_one_warning_naming_the_retired_names(monkeypatch, capfd):
    import main
    from logging_setup import get_logger

    monkeypatch.setattr(main.settings, "_retired_env", ["COMPRESSATORIUM_CHDMAN_NICE"])
    logger = get_logger()
    # Restored afterwards: configure_logging sets all three.
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    monkeypatch.setattr(logger, "level", logger.level)
    main.configure_logging()
    main.configure_logging()  # a second call (lifespan, then _serve) says nothing
    for handler in logger.handlers:
        handler.flush()
    err = capfd.readouterr().err
    assert err.count("Retired settings") == 1
    assert "COMPRESSATORIUM_CHDMAN_NICE" in err


class _StubSettings:
    """Minimal stand-in for ``config.settings`` used to exercise the resolver."""

    def __init__(self, **values):
        self._values = values

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError as exc:  # pragma: no cover - mirrors getattr default path
            raise AttributeError(name) from exc


def _patch_settings(monkeypatch, **values):
    from app.services import subprocess_runner

    monkeypatch.setattr(subprocess_runner, "settings", _StubSettings(**values))
    return subprocess_runner


def test_resolver_reads_the_shared_values(monkeypatch):
    sr = _patch_settings(
        monkeypatch,
        tool_nice=10,
        tool_info_timeout=60,
        tool_verify_timeout=0,
        tool_ioprio_class=2,
        tool_ioprio_level=6,
    )
    assert sr.nice_value() == 10
    assert sr.info_timeout() == 60
    assert sr.verify_timeout() == 0


def test_timeout_helpers_clamp_negative_and_none(monkeypatch):
    sr = _patch_settings(
        monkeypatch,
        tool_info_timeout=-5,
        tool_verify_timeout=None,
    )
    assert sr.info_timeout() == 0
    assert sr.verify_timeout() == 0
