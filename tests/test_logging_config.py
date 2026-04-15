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
    assert (
        Settings(CHD_DEBUG_LOG_PATH="/var/log/chd-debug.log").log_path == "/var/log/chd-debug.log"
    )


def test_log_path_takes_precedence_over_legacy(monkeypatch):
    """LOG_PATH wins when both LOG_PATH and CHD_DEBUG_LOG_PATH are provided."""
    monkeypatch.setenv("LOG_PATH", "/preferred/path.log")
    monkeypatch.setenv("CHD_DEBUG_LOG_PATH", "/legacy/path.log")

    assert Settings().log_path == "/preferred/path.log"


# ---------------------------------------------------------------------------
# LOG_COLOR / ColorFormatter
# ---------------------------------------------------------------------------


import io
import logging
import re
from pathlib import Path

import pytest

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def test_log_color_default_is_always(monkeypatch):
    monkeypatch.delenv("LOG_COLOR", raising=False)
    assert Settings().log_color == "always"


@pytest.mark.parametrize("mode", ["auto", "always", "never", "AUTO"])
def test_log_color_accepts_valid_modes(mode):
    assert Settings(LOG_COLOR=mode).log_color == mode


def test_log_color_invalid_value_round_trips_for_warning():
    # Invalid strings are still accepted at the Settings layer — the
    # warning is emitted by configure_logging(), which sees the raw value.
    assert Settings(LOG_COLOR="rainbow").log_color == "rainbow"


class _FakeStream:
    def __init__(self, is_tty: bool):
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_resolve_color_always_forces_true(monkeypatch):
    from main import _resolve_color

    monkeypatch.setenv("NO_COLOR", "1")
    assert _resolve_color("always", _FakeStream(is_tty=False)) is True


def test_resolve_color_never_forces_false(monkeypatch):
    from main import _resolve_color

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _resolve_color("never", _FakeStream(is_tty=True)) is False


def test_resolve_color_auto_follows_tty(monkeypatch):
    from main import _resolve_color

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _resolve_color("auto", _FakeStream(is_tty=True)) is True
    assert _resolve_color("auto", _FakeStream(is_tty=False)) is False


def test_resolve_color_auto_respects_no_color(monkeypatch):
    from main import _resolve_color

    monkeypatch.setenv("NO_COLOR", "1")
    assert _resolve_color("auto", _FakeStream(is_tty=True)) is False


def test_color_formatter_wraps_levelname_only():
    from main import ColorFormatter, _LOG_FORMAT, _RESET

    fmt = ColorFormatter(_LOG_FORMAT)
    record = logging.LogRecord(
        name="chd.test", level=logging.WARNING, pathname="x", lineno=1,
        msg="plain message body", args=(), exc_info=None,
    )
    out = fmt.format(record)
    # Levelname wrapped in SGR, but the message body is not.
    assert "\x1b[33mWARNING" + _RESET in out
    assert "plain message body" in out
    assert "\x1b[" not in out.split("plain message body")[-1]
    # Side-effect free: record.levelname restored.
    assert record.levelname == "WARNING"


@pytest.fixture
def _reset_chd_logger():
    """Detach handlers and reset the 'chd' logger between tests."""
    logger = logging.getLogger("chd")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    for h in saved_handlers:
        logger.removeHandler(h)
    logger.handlers.clear()
    yield logger
    for h in list(logger.handlers):
        logger.removeHandler(h)
    for h in saved_handlers:
        logger.addHandler(h)
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate


def _emit_captured(logger: logging.Logger, level: int, msg: str) -> str:
    """Log one line and return whatever the StreamHandler wrote."""
    buf = io.StringIO()
    # Re-point the already-installed StreamHandler at our buffer so we
    # exercise the exact formatter configure_logging() installed.
    stream_handler = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                          and not isinstance(h, logging.FileHandler))
    stream_handler.stream = buf
    logger.log(level, msg)
    stream_handler.flush()
    return buf.getvalue()


def test_configure_logging_with_color_always_wraps_stream_output(
    _reset_chd_logger, monkeypatch,
):
    monkeypatch.setenv("LOG_COLOR", "always")
    monkeypatch.setenv("LOGLEVEL", "DEBUG")
    monkeypatch.delenv("LOG_PATH", raising=False)

    # Rebuild settings with the new env and reconfigure logging.
    from main import configure_logging
    import config as _config_mod
    monkeypatch.setattr(_config_mod, "settings", _config_mod.Settings())
    import main as _main_mod
    monkeypatch.setattr(_main_mod, "settings", _config_mod.settings)

    configure_logging()
    out = _emit_captured(_reset_chd_logger, logging.ERROR, "boom")
    assert "\x1b[31mERROR\x1b[0m" in out
    assert "boom" in out


def test_configure_logging_never_color_emits_plain_stream(
    _reset_chd_logger, monkeypatch,
):
    monkeypatch.setenv("LOG_COLOR", "never")
    monkeypatch.setenv("LOGLEVEL", "INFO")
    monkeypatch.delenv("LOG_PATH", raising=False)

    from main import configure_logging
    import config as _config_mod
    monkeypatch.setattr(_config_mod, "settings", _config_mod.Settings())
    import main as _main_mod
    monkeypatch.setattr(_main_mod, "settings", _config_mod.settings)

    configure_logging()
    out = _emit_captured(_reset_chd_logger, logging.INFO, "hello")
    assert ANSI_RE.search(out) is None
    assert "hello" in out


def test_file_handler_never_colored_even_with_log_color_always(
    _reset_chd_logger, monkeypatch, tmp_path: Path,
):
    log_file = tmp_path / "chd.log"
    monkeypatch.setenv("LOG_COLOR", "always")
    monkeypatch.setenv("LOGLEVEL", "INFO")
    monkeypatch.setenv("LOG_PATH", str(log_file))

    from main import configure_logging
    import config as _config_mod
    monkeypatch.setattr(_config_mod, "settings", _config_mod.Settings())
    import main as _main_mod
    monkeypatch.setattr(_main_mod, "settings", _config_mod.settings)

    configure_logging()
    _reset_chd_logger.warning("file-safe")

    # Flush FileHandler explicitly so the file is readable.
    for h in _reset_chd_logger.handlers:
        h.flush()

    written = log_file.read_text(encoding="utf-8")
    assert "file-safe" in written
    assert ANSI_RE.search(written) is None, (
        f"log file must not contain ANSI escapes, got: {written!r}"
    )
