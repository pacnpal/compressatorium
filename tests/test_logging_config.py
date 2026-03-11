"""Tests for LOGLEVEL/LOG_PATH settings and legacy CHD_DEBUG/CHD_DEBUG_LOG_PATH compat."""

from app.config import Settings


def test_log_level_default_is_info(monkeypatch):
    monkeypatch.delenv("LOGLEVEL", raising=False)
    monkeypatch.delenv("CHD_DEBUG", raising=False)

    assert Settings().log_level == "INFO"


def test_log_level_set_via_loglevel(monkeypatch):
    monkeypatch.delenv("CHD_DEBUG", raising=False)

    assert Settings(LOGLEVEL="DEBUG").log_level == "DEBUG"
    assert Settings(LOGLEVEL="WARNING").log_level == "WARNING"
    assert Settings(LOGLEVEL="ERROR").log_level == "ERROR"
    assert Settings(LOGLEVEL="CRITICAL").log_level == "CRITICAL"


def test_log_level_chd_debug_true_maps_to_debug(monkeypatch):
    """Legacy CHD_DEBUG=true should default log_level to DEBUG when LOGLEVEL is unset."""
    monkeypatch.setenv("CHD_DEBUG", "true")
    monkeypatch.delenv("LOGLEVEL", raising=False)

    assert Settings().log_level == "DEBUG"


def test_log_level_chd_debug_false_keeps_info_default(monkeypatch):
    """CHD_DEBUG=false must not change the log level."""
    monkeypatch.setenv("CHD_DEBUG", "false")
    monkeypatch.delenv("LOGLEVEL", raising=False)

    assert Settings().log_level == "INFO"


def test_loglevel_takes_precedence_over_chd_debug(monkeypatch):
    """Explicit LOGLEVEL wins when both CHD_DEBUG=true and LOGLEVEL are set."""
    monkeypatch.setenv("CHD_DEBUG", "true")
    monkeypatch.setenv("LOGLEVEL", "INFO")

    # LOGLEVEL is explicitly set so CHD_DEBUG compat must not override it
    assert Settings().log_level == "INFO"


def test_explicit_log_level_kwarg_wins_over_chd_debug(monkeypatch):
    """An explicit log_level kwarg (e.g. in tests) must not be overridden by CHD_DEBUG=true."""
    monkeypatch.setenv("CHD_DEBUG", "true")
    monkeypatch.delenv("LOGLEVEL", raising=False)

    # log_level is in __pydantic_fields_set__ so the CHD_DEBUG compat block must skip it
    assert Settings(log_level="WARNING").log_level == "WARNING"


def test_log_path_default_is_none(monkeypatch):
    monkeypatch.delenv("LOG_PATH", raising=False)
    monkeypatch.delenv("CHD_DEBUG_LOG_PATH", raising=False)

    assert Settings().log_path is None


def test_log_path_set_via_env_var():
    assert Settings(LOG_PATH="/var/log/chd.log").log_path == "/var/log/chd.log"


def test_log_path_set_via_legacy_chd_debug_log_path():
    """CHD_DEBUG_LOG_PATH must still populate log_path for existing deployments."""
    assert Settings(CHD_DEBUG_LOG_PATH="/var/log/chd-debug.log").log_path == "/var/log/chd-debug.log"


def test_log_path_takes_precedence_over_legacy(monkeypatch):
    """LOG_PATH wins when both LOG_PATH and CHD_DEBUG_LOG_PATH are provided."""
    monkeypatch.setenv("LOG_PATH", "/preferred/path.log")
    monkeypatch.setenv("CHD_DEBUG_LOG_PATH", "/legacy/path.log")

    assert Settings().log_path == "/preferred/path.log"
